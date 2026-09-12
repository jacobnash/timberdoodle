"""
Same two layers as test_fault_openapi.py: (1) commissioning-api-openapi.yaml
is well-formed OpenAPI, (2) live requests against the real handler produce
what the spec documents. The handler is spun up with an in-memory repo
(no Postgres) and no graph store, so this runs in the `unit` CI job; the
Postgres/gateway path is covered by test_commissioning_integration.py.
"""

import contextlib
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests
from conftest import assert_matches_schema, load_spec
from cx_fixtures import SPEC_MATERIAL
from openapi_spec_validator import validate

from timberdoodle.commissioning import db
from timberdoodle.commissioning.api import OPENAPI_SPEC_PATH, make_handler

GATEWAY_SECRET = "test-gateway-secret"
H = {"X-Gateway-Secret": GATEWAY_SECRET, "X-User": "tester@example.invalid"}


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


def test_spec_is_well_formed_openapi(spec):
    validate(spec)


def test_every_route_in_the_handler_is_documented_and_vice_versa(spec):
    from timberdoodle.commissioning.api import ROUTES
    documented = set()
    for path, ops in spec["paths"].items():
        for method in ("get", "post", "delete"):
            if method in ops:
                documented.add((method.upper(), path.replace("{projectId}", "{pid}")))
    implemented = set()
    for method, rx, _ in ROUTES:
        p = rx.pattern.strip("^$").replace(r"(?P<pid>[A-Za-z0-9_\-]+)", "{pid}").replace(r"(?P<id>[A-Za-z0-9_\-]+)", "{id}")
        implemented.add((method, p))
    implemented.add(("GET", "/openapi.yaml"))
    assert implemented == documented


class _FakePool:
    """The handler takes one connection per request from a pool; with an
    in-memory repo the connection is never touched."""

    @contextlib.contextmanager
    def connection(self):
        yield None


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("TIMBERDOODLE_GATEWAY_SECRET", GATEWAY_SECRET)
    repo = db.MemoryRepo()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_FakePool(), lambda: None, repo_factory=lambda conn: repo))
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _schema(spec, path, method, status):
    """A $ref *into* the spec document (JSON pointer), not the schema dict
    itself - so nested refs like Report.entities -> Entity resolve against
    the spec rather than against the extracted fragment."""
    parts = ("paths", path, method, "responses", str(status), "content", "application/json", "schema")
    assert spec["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"]
    return {"$ref": "#/" + "/".join(p.replace("~", "~0").replace("/", "~1") for p in parts)}


def _devices_as_fbf_rows():
    """The shape FBF's GET /devices + POST /learn produce - not the
    internal DiscoveredDevice dict - to prove the push path converts."""
    def row(iid, addr, name, learned, prefix):
        return {"id": f"bacnet:{iid}@{addr}", "protocol": "bacnet", "device_instance": iid, "address": addr, "vendor_id": 8, "status": "online", "name": name,
                "first_seen_at": "2026-09-09T12:00:00Z", "last_seen_at": "2026-09-12T12:00:00Z", "topic_prefix": prefix,
                "learned": [{"object_identifier": oid, "label": lbl, "units": u, "present_value": pv} for oid, lbl, u, pv in learned]}
    return [
        row(1001, "10.0.0.11", "AHU_1", [("analogInput,1", "SA-T", "degF", 55.2), ("analogValue,1", "SA-T-SP", "degF", 55.0), ("binaryInput,1", "SF-S", None, 1), ("binaryOutput,1", "SF-C", None, 1), ("analogInput,2", "DSP", "inH2O", 1.2)], "site/AHU_1"),
        row(2001, "10.0.0.21", "V1_1", [("analogInput,1", "ZN-T", "degF", 71.5), ("analogValue,1", "ZN-T-SP", "degF", 72.0), ("analogOutput,1", "DMPR-POS", "%", 40.0), ("analogInput,2", "FLOW", "cfm", 350.0)], "site/V1_1"),
        row(2003, "10.0.0.23", "V1_3", [("analogInput,1", "ZN-T", "degF", 70.5), ("analogValue,1", "ZN-T-SP", "degF", 72.0), ("analogOutput,1", "DMPR-POS", "%", 40.0)], "site/V1_3"),
    ]


def test_public_routes_and_gateway_gate(live_server):
    assert requests.get(f"{live_server}/openapi.yaml").status_code == 200
    assert "text/html" in requests.get(f"{live_server}/docs").headers["Content-Type"]
    assert requests.get(f"{live_server}/projects").status_code == 401
    assert requests.get(f"{live_server}/projects", headers={"X-Gateway-Secret": "wrong"}).status_code == 401
    assert requests.get(f"{live_server}/nope", headers=H).status_code == 404


def test_full_journey_matches_the_spec(live_server, spec):
    r = requests.get(f"{live_server}/vocabulary", headers=H)
    assert r.status_code == 200
    vocab = r.json()
    assert set(vocab["phases"]) == {"new_construction", "warranty", "operations", "retrofit"}
    assert "air handling unit" in vocab["equipment_types"]

    r = requests.post(f"{live_server}/projects", headers=H, json={"name": "Contract Test Bldg", "cidr_scopes": ["10.0.0.0/24"]})
    assert r.status_code == 201, r.text
    project = r.json()
    assert_matches_schema(project, _schema(spec, "/projects", "post", 201), spec)
    pid = project["id"]
    assert project["phase"] is None and project["phase_assumed"] is False

    r = requests.get(f"{live_server}/projects", headers=H)
    assert_matches_schema(r.json(), _schema(spec, "/projects", "get", 200), spec)
    assert requests.get(f"{live_server}/projects/proj_nope", headers=H).status_code == 404

    r = requests.post(f"{live_server}/projects/{pid}/spec", headers=H, json={"material": SPEC_MATERIAL})
    assert r.status_code == 200, r.text
    assert r.json()["model"]["equipment"][0]["tag"] == "AHU-1"
    assert requests.post(f"{live_server}/projects/{pid}/spec", headers=H, json={"material": {"equipment": "not a list"}}).status_code == 400

    r = requests.post(f"{live_server}/projects/{pid}/devices", headers=H, json={"devices": _devices_as_fbf_rows()})
    assert r.status_code == 200, r.text
    # a row with a topic_prefix takes the ingest layer's equip URI as its field id, so it merges with graph discovery
    assert sorted(r.json()["stored"]) == ["urn:equip:site/AHU_1", "urn:equip:site/V1_1", "urn:equip:site/V1_3"]
    devs = requests.get(f"{live_server}/projects/{pid}/devices", headers=H).json()
    assert {d["name"] for d in devs} == {"AHU_1", "V1_1", "V1_3"}
    assert devs[0]["pushed_by"] == "tester@example.invalid"
    assert all(o["point_uri"].startswith("urn:point:site/") for d in devs for o in d["objects"])

    r = requests.get(f"{live_server}/projects/{pid}/report", headers=H)
    assert r.json()["report"] is None and "no pass has run yet" in r.json()["note"]

    r = requests.post(f"{live_server}/projects/{pid}/passes", headers=H, json={"use_graph": False})
    assert r.status_code == 200, r.text
    report = r.json()
    assert_matches_schema(report, _schema(spec, "/projects/{projectId}/passes", "post", 200), spec)
    assert report["pass"]["phase_assumed"] is True and report["pass"]["trigger"] == "api:tester@example.invalid"
    assert report["summary"]["entities"] == 5
    by = {e["spec_tag"]: e for e in report["entities"] if e.get("spec_tag")}
    assert by["AHU-1"]["confidence"] == "high" and by["VAV-1-02"]["confidence"] == "low"
    assert by["AHU-1"]["ladder"]["rungs"][0]["result"] == "insufficient_data"  # no history source in this harness - says so
    for d in report["deviations"]:
        assert_matches_schema(d, spec["components"]["schemas"]["Deviation"], spec)

    r = requests.get(f"{live_server}/projects/{pid}", headers=H)
    assert_matches_schema(r.json(), _schema(spec, "/projects/{projectId}", "get", 200), spec)
    assert r.json()["phase"] == report["pass"]["phase"] and r.json()["last_pass"]["id"] == report["id"]

    assert requests.get(f"{live_server}/projects/{pid}/passes/{report['id']}", headers=H).json()["id"] == report["id"]
    assert requests.get(f"{live_server}/projects/{pid}/passes/pass_nope", headers=H).status_code == 404
    passes = requests.get(f"{live_server}/projects/{pid}/passes", headers=H).json()
    assert len(passes) == 1 and passes[0]["id"] == report["id"]
    assert requests.get(f"{live_server}/projects/{pid}/report", headers=H).json()["id"] == report["id"]

    ents = requests.get(f"{live_server}/projects/{pid}/entities?confidence=low", headers=H).json()
    assert_matches_schema(ents, _schema(spec, "/projects/{projectId}/entities", "get", 200), spec)
    assert {e["spec_tag"] for e in ents} >= {"VAV-1-02"}
    one = requests.get(f"{live_server}/projects/{pid}/entities/{by['VAV-1-02']['id']}", headers=H).json()
    assert one["id"] == by["VAV-1-02"]["id"] and isinstance(one["deviation_records"], list)
    assert requests.get(f"{live_server}/projects/{pid}/entities/ent_nope", headers=H).status_code == 404

    devs_open = requests.get(f"{live_server}/projects/{pid}/deviations", headers=H).json()
    assert_matches_schema(devs_open, _schema(spec, "/projects/{projectId}/deviations", "get", 200), spec)
    assert all(d["status"] == "open" for d in devs_open)
    assert {d["kind"] for d in devs_open} >= {"spec_device_not_found"}

    questions = requests.get(f"{live_server}/projects/{pid}/questions", headers=H).json()
    q = next(q for q in questions if q["kind"] == "mapping" and q["spec_tag"] == "VAV-1-02")
    assert q["accept"] == {"kind": "confirm_mapping", "spec_tag": "VAV-1-02", "field_id": "urn:equip:site/V1_3"}

    punch = requests.get(f"{live_server}/projects/{pid}/punchlist", headers=H).json()
    assert punch["items"] and punch["by_location"]
    assert all(it["status"] == "open" for it in punch["items"])
    ch_item = next(it for it in punch["items"] if it["spec_tag"] == "CH-1")

    # post the accept action straight back
    r = requests.post(f"{live_server}/projects/{pid}/corrections", headers=H, json=q["accept"])
    assert r.status_code == 200, r.text
    corr = r.json()
    assert_matches_schema(corr["correction"], spec["components"]["schemas"]["Correction"], spec)
    assert corr["correction"]["by"] == "tester@example.invalid"
    assert any(m["spec_tag"] == "VAV-1-02" and m["to"] == "confirmed" for m in corr["confidence_movement"])
    assert requests.post(f"{live_server}/projects/{pid}/corrections", headers=H, json={"kind": "bogus"}).status_code == 400
    assert requests.post(f"{live_server}/projects/{pid}/corrections", headers=H, json={"kind": "resolve_deviation", "deviation_id": "dev_nope"}).status_code == 404
    assert [c["kind"] for c in requests.get(f"{live_server}/projects/{pid}/corrections", headers=H).json()] == ["confirm_mapping"]  # rejected corrections are not recorded
    assert len(requests.get(f"{live_server}/projects/{pid}/passes", headers=H).json()) == 2

    # a capture: legible tag, partial address, unreadable serial
    r = requests.post(f"{live_server}/projects/{pid}/captures", headers=H, json={"punch_item_id": ch_item["id"], "location": {"floor": "B", "room": "Chiller room"}, "legible": {"nameplate": {"tag": "CH-1", "model": "YVAA0195"}, "address": "10.0.0.4?"}, "unreadable": ["serial - scratched"]})
    assert r.status_code == 201, r.text
    cap = r.json()["capture"]
    assert_matches_schema(cap, spec["components"]["schemas"]["FieldCapture"], spec)
    assert cap["engineer"] == "tester@example.invalid" and cap["partial"] == {"address": "10.0.0.4?"}
    assert r.json()["resolved_punch_items"] == [ch_item["id"]]
    assert requests.post(f"{live_server}/projects/{pid}/captures", headers=H, json={"note": "no fields"}).status_code == 400
    assert len(requests.get(f"{live_server}/projects/{pid}/captures", headers=H).json()) == 1
    assert all(it["spec_tag"] != "CH-1" for it in requests.get(f"{live_server}/projects/{pid}/punchlist", headers=H).json()["items"])
    assert any(it["spec_tag"] == "CH-1" for it in requests.get(f"{live_server}/projects/{pid}/punchlist?status=resolved", headers=H).json()["items"])

    # risks
    r = requests.post(f"{live_server}/projects/{pid}/risks", headers=H, json={"spec_tag": "AHU-1", "description": "RA-T sensor known bad"})
    assert r.status_code == 201, r.text
    risk = r.json()
    assert_matches_schema(risk, _schema(spec, "/projects/{projectId}/risks", "post", 201), spec)
    assert risk["accepted_by"] == "tester@example.invalid" and risk["review_by"]
    assert requests.post(f"{live_server}/projects/{pid}/risks", headers=H, json={"description": "covers nothing"}).status_code == 400
    assert_matches_schema(requests.get(f"{live_server}/projects/{pid}/risks", headers=H).json(), _schema(spec, "/projects/{projectId}/risks", "get", 200), spec)
    assert requests.delete(f"{live_server}/projects/{pid}/risks/{risk['id']}", headers=H).status_code == 204
    assert requests.delete(f"{live_server}/projects/{pid}/risks/{risk['id']}", headers=H).status_code == 404

    fresh = requests.get(f"{live_server}/projects/{pid}/freshness", headers=H).json()
    assert fresh["statement"].startswith("as of ") and fresh["networked_entities"] == 4

    brick = requests.get(f"{live_server}/projects/{pid}/projection", headers=H).json()
    assert brick["triple_count"] == len(brick["triples"]) > 0 and brick["skipped"]
    hay = requests.get(f"{live_server}/projects/{pid}/projection?format=haystack", headers=H).json()
    assert {rec["dis"] for rec in hay["records"]} >= {"AHU-1", "VAV-1-01", "VAV-1-02"}
    assert requests.get(f"{live_server}/projects/{pid}/projection?format=turtle", headers=H).status_code == 400
    assert requests.post(f"{live_server}/projects/{pid}/projection", headers=H, json={}).status_code == 503  # no graph store in this harness

    # phase can be stated after the fact and the inference note is replaced
    r = requests.post(f"{live_server}/projects/{pid}/phase", headers=H, json={"phase": "retrofit", "note": "occupied building, AHU replacement"})
    assert r.status_code == 200 and r.json()["phase"] == "retrofit" and r.json()["phase_assumed"] is False
    assert "stated by tester@example.invalid: occupied building" in r.json()["phase_rationale"]
    assert requests.post(f"{live_server}/projects/{pid}/phase", headers=H, json={"phase": "demolition"}).status_code == 400

    assert requests.delete(f"{live_server}/projects/{pid}", headers=H).status_code == 204
    assert requests.get(f"{live_server}/projects/{pid}", headers=H).status_code == 404
