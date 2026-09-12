"""
The commissioning agent against the REAL stack: every HTTP call goes
through the nginx gateway at localhost:8080 with a Bearer JWT (njs
verification, policy.js rows for /commissioning/), the pass runs inside
the commissioning_api container against the real Postgres (PgRepo,
PostgresHistory over point_history) and the real Oxigraph (graph
discovery, Brick projection), and faults are mirrored into the shared
`faults` table the fault_api/alarm console read.

Readings arrive the way they do in production - published over the
compose-managed broker and ingested by the running mqtt_listener - so the
history the ladder climbs is the connector's, not something this test
wrote into a table by hand.

Needs `docker compose up -d` with commissioning_api + gateway rebuilt from
this branch (see CLAUDE.md: restart the gateway after rebuilding a
backend).
"""

import json
import time

import pytest
import requests

from timberdoodle.ingest import (
    ingest_equip_tags,
    link_point_to_equip,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from timberdoodle.mqtt_util import make_client
from timberdoodle.remote_store import RemoteStore
from timberdoodle.store import TD
from timberdoodle.timeseries import connect

BASE = "http://localhost:8080"
ORG_NAME_PREFIX = "Commissioning Integration Test Org"
ADMIN_EMAIL = "admin@cx-integration-test.invalid"
ADMIN_PASSWORD = "correct-horse-battery-staple-cx"


def _cleanup_orgs():
    conn = connect()
    for sql in (
        "DELETE FROM user_sites WHERE user_id IN (SELECT id FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE %s))",
        "DELETE FROM revoked_tokens WHERE jti IN (SELECT jti FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE %s))",
        "DELETE FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE %s)",
        "DELETE FROM sites WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE %s)",
        "DELETE FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE %s)",
        "DELETE FROM orgs WHERE name LIKE %s",
    ):
        conn.execute(sql, (f"{ORG_NAME_PREFIX}%",))
    conn.close()


@pytest.fixture
def clean_org():
    _cleanup_orgs()
    yield
    _cleanup_orgs()


def _post_with_retry(url, json):
    """Same 429-tolerant login helper as test_e2e_journey.py - the gateway
    rate-limits /auth/* and the limit is shared across the whole suite."""
    resp = None
    for attempt in range(10):
        resp = requests.post(url, json=json)
        if resp.status_code != 429:
            return resp
        if attempt < 9:
            time.sleep(8)
    return resp


def _publish(client, topic: str, value, ts: float) -> None:
    client.publish(topic, json.dumps({"value": value, "ts": ts})).wait_for_publish(5)


def _wait_for_history(conn, point_uri: str, n: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        (count,) = conn.execute("SELECT count(*) FROM point_history WHERE point_uri = %s", (point_uri,)).fetchone()
        if count >= n:
            return
        time.sleep(0.2)
    raise AssertionError(f"mqtt_listener did not land {n} samples for {point_uri} in {timeout}s")


@pytest.mark.integration
def test_commissioning_pass_through_the_real_gateway_postgres_oxigraph_and_faults_table(clean_org):
    tag = f"cxint{int(time.time() * 1000)}"
    topic_prefix = f"fbf/{tag}/AHU_7"
    equip_uri = topic_prefix_to_equip_uri(topic_prefix)
    store = RemoteStore()
    conn = connect()
    pid = None
    h = {}
    try:
        # --- auth: org + admin, then a real Bearer token ------------------
        org = _post_with_retry(f"{BASE}/auth/orgs", json={"name": f"{ORG_NAME_PREFIX} {tag}", "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD})
        assert org.status_code == 201, org.text
        login = _post_with_retry(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert login.status_code == 200, login.text
        h = {"Authorization": f"Bearer {login.json()['token']}"}

        # no token -> the gateway refuses before the backend sees it
        assert requests.get(f"{BASE}/commissioning/projects").status_code == 401
        assert requests.get(f"{BASE}/commissioning/openapi.yaml").status_code == 200  # spec is public

        # --- the connector's side: readings over MQTT, equipment in the graph
        now = time.time()
        names = {"SA-T": [55.0, 55.4, 55.1, 55.3], "SA-T-SP": [55.0] * 4, "SF-S": [1, 1, 1, 1], "SF-C": [1, 1, 1, 1], "RA-T": [72.0] * 4}  # RA-T frozen
        client = make_client()
        client.connect("localhost", 1883)
        client.loop_start()
        for name, values in names.items():
            for i, v in enumerate(values):
                _publish(client, f"{topic_prefix}/{name}", v, now - (3 - i) * 3600)
        client.loop_stop()
        client.disconnect()
        for name in names:
            _wait_for_history(conn, str(topic_to_point_uri(f"{topic_prefix}/{name}")), 4)
        ingest_equip_tags(store, topic_prefix, {"ahu": True, "dis": f"AHU_7 {tag}"})
        for name in names:
            link_point_to_equip(store, topic_to_point_uri(f"{topic_prefix}/{name}"), equip_uri)

        # --- the agent's side, all through /commissioning/ -----------------
        r = requests.post(f"{BASE}/commissioning/projects", headers=h, json={"name": f"CX integration {tag}", "phase": "warranty", "cidr_scopes": ["10.0.0.0/24"]})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        spec = {"documents": [{"document": "M-601"}], "equipment": [
            {"tag": "AHU-7", "type": "air handling unit", "location": {"floor": "1", "room": "Mech 101"}, "points": [{"name": "Supply Air Temp", "units": "degF"}, {"name": "SA Temp Setpoint", "units": "degF"}, {"name": "Supply Fan Status"}, {"name": "Supply Fan Command"}, {"name": "Return Air Temp", "units": "degF"}]},
            {"tag": "VAV-7-01", "type": "VAV", "fed_by": ["AHU-7"], "location": {"floor": "1", "room": "701"}, "points": [{"name": "Zone Temp"}]},
            {"tag": "EF-7", "type": "exhaust fan", "networked": False, "location": {"floor": "roof"}},
        ]}
        assert requests.post(f"{BASE}/commissioning/projects/{pid}/spec", headers=h, json={"material": spec}).status_code == 200
        r = requests.post(f"{BASE}/commissioning/projects/{pid}/devices", headers=h, json={"devices": [{"id": "bacnet:7001@10.0.0.71", "protocol": "bacnet", "device_instance": 7001, "address": "10.0.0.71", "vendor_id": 8, "name": "AHU_7", "topic_prefix": topic_prefix, "last_seen_at": now}]})
        assert r.status_code == 200, r.text
        assert r.json()["stored"] == [str(equip_uri)]

        r = requests.post(f"{BASE}/commissioning/projects/{pid}/passes", headers=h, json={})
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["pass"]["phase"] == "warranty" and report["pass"]["phase_assumed"] is False
        assert "no graph store configured" not in " ".join(report["pass"]["notes"])
        ahu = next(e for e in report["entities"] if e.get("spec_tag") == "AHU-7")
        assert ahu["confidence"] in ("high", "confirmed"), ahu["confidence_basis"]
        assert ahu["field_identity"]["field_id"] == str(equip_uri)
        assert ahu["field_identity"]["device_instance"] == 7001  # pushed row merged with the graph's equipment
        rungs = ahu["ladder"]["rungs"]
        assert rungs[0]["result"] == "pass", rungs[0]  # PostgresHistory saw the MQTT samples
        assert rungs[1]["result"] == "fail" and "return air temperature" in rungs[1]["symptom"]
        assert ahu["liveness"]["state"] == "present"
        faults = [f for f in report["faults"] if f["entity_id"] == ahu["id"]]
        assert len(faults) == 1 and faults[0]["kind"] == "rung2_failure"
        key = f"cx:{pid}:{ahu['id']}"
        assert key in report["fault_sync"]["opened"]
        rows = conn.execute("SELECT rule_id, ended_at, detail FROM faults WHERE point_uri = %s", (key,)).fetchall()
        assert len(rows) == 1 and rows[0][0] == "commissioning:ladder-failure" and rows[0][1] is None
        assert rows[0][2]["project_id"] == pid and rows[0][2]["spec_tag"] == "AHU-7"
        # the not-found VAV and the non-networked fan are deviations / punch items, never faults
        assert {d["kind"] for d in report["deviations"]} >= {"spec_device_not_found"}
        assert all(f["spec_tag"] == "AHU-7" for f in report["faults"] if f.get("spec_tag") in ("AHU-7", "VAV-7-01", "EF-7"))
        assert any(it["spec_tag"] == "VAV-7-01" for g in report["field_list"] for it in g["items"])

        # --- accept the risk: fault leaves the fault list and closes in the table
        r = requests.post(f"{BASE}/commissioning/projects/{pid}/risks", headers=h, json={"spec_tag": "AHU-7", "description": "RA-T sensor known bad, replacement ordered"})
        assert r.status_code == 201, r.text
        risk_id = r.json()["id"]
        report2 = requests.post(f"{BASE}/commissioning/projects/{pid}/passes", headers=h, json={}).json()
        assert not [f for f in report2["faults"] if f["entity_id"] == ahu["id"]]
        covered = [c for c in report2["accepted_risks"]["covered_this_pass"] if c["entity_id"] == ahu["id"]]
        assert len(covered) == 1 and covered[0]["risk_id"] == risk_id
        assert key in report2["fault_sync"]["closed"]
        (ended,) = conn.execute("SELECT ended_at FROM faults WHERE point_uri = %s", (key,)).fetchone()
        assert ended is not None

        # --- projection into the real graph ----------------------------------
        r = requests.post(f"{BASE}/commissioning/projects/{pid}/projection", headers=h, json={})
        assert r.status_code == 200, r.text
        assert r.json()["written"] > 0 and ahu["id"] in r.json()["projected_entities"]
        rows = store.query(f"SELECT ?t WHERE {{ <{equip_uri}> <{TD.specTag}> ?t }}")
        assert [str(x.t) for x in rows] == ["AHU-7"]
        rows = store.query(f"SELECT ?c WHERE {{ <{topic_to_point_uri(topic_prefix + '/SA-T')}> a ?c . FILTER(STRSTARTS(STR(?c), 'https://brickschema.org/schema/Brick#')) }}")
        assert "https://brickschema.org/schema/Brick#Supply_Air_Temperature_Sensor" in {str(x.c) for x in rows}

        # --- the views are the same stored documents ------------------------
        assert requests.get(f"{BASE}/commissioning/projects/{pid}/report", headers=h).json()["id"] == report2["id"]
        assert requests.get(f"{BASE}/commissioning/projects/{pid}/entities/{ahu['id']}", headers=h).json()["accepted_risks"] == [risk_id]
        fresh = requests.get(f"{BASE}/commissioning/projects/{pid}/freshness", headers=h).json()
        assert "AHU-7" in fresh["entities"]["fresh"]
    finally:
        if pid:
            requests.delete(f"{BASE}/commissioning/projects/{pid}", headers=h)  # admin-only row in policy.js
            conn.execute("DELETE FROM faults WHERE point_uri LIKE %s", (f"cx:{pid}:%",))
        for name in ("SA-T", "SA-T-SP", "SF-S", "SF-C", "RA-T"):
            conn.execute("DELETE FROM point_history WHERE point_uri = %s", (str(topic_to_point_uri(f"{topic_prefix}/{name}")),))
        store._update(f"DELETE WHERE {{ <{equip_uri}> ?p ?o }}")
        store._update(f"DELETE WHERE {{ ?s ?p <{equip_uri}> }}")
        for name in ("SA-T", "SA-T-SP", "SF-S", "SF-C", "RA-T"):
            store._update(f"DELETE WHERE {{ <{topic_to_point_uri(topic_prefix + '/' + name)}> ?p ?o }}")
        conn.close()
