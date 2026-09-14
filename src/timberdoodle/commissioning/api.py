"""
HTTP surface for the commissioning agent - same shape as fault_api.py /
ops_api.py: stdlib http.server, one Postgres connection per request from
a pool, OTel span per request, spec served off disk, every route behind
the gateway (X-Gateway-Secret) except /openapi.yaml and /docs. Port 8008,
proxied at /commissioning/ (gateway/nginx.conf, rows in
gateway/njs/policy.js).

The API is thin: it stores inputs (spec material, pushed device data,
corrections, captures, accepted risks) and runs `engine.run_pass` /
`engine.apply_correction`. Everything the agent has to say is in the pass
report; the individual GETs are views of the same stored documents.
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from timberdoodle import docs_ui, gateway_auth, tracing
from timberdoodle.commissioning import (
    db,
    discovery,
    engine,
    projection,
    punchlist,
    reconcile,
    spec_model,
)
from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.model import PHASES, now_iso
from timberdoodle.http_handler_base import BaseAPIHandler, respond_error, respond_json
from timberdoodle.remote_store import RemoteStore
from timberdoodle.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "commissioning-api-openapi.yaml")

_PROJECT = r"/projects/(?P<pid>[A-Za-z0-9_\-]+)"
ROUTES: list[tuple[str, re.Pattern, str]] = [
    ("GET", re.compile(r"^/vocabulary$"), "vocabulary"),
    ("GET", re.compile(r"^/projects$"), "list_projects"),
    ("POST", re.compile(r"^/projects$"), "create_project"),
    ("GET", re.compile(rf"^{_PROJECT}$"), "get_project"),
    ("DELETE", re.compile(rf"^{_PROJECT}$"), "delete_project"),
    ("POST", re.compile(rf"^{_PROJECT}/phase$"), "set_phase"),
    ("GET", re.compile(rf"^{_PROJECT}/spec$"), "get_spec"),
    ("POST", re.compile(rf"^{_PROJECT}/spec$"), "put_spec"),
    ("GET", re.compile(rf"^{_PROJECT}/devices$"), "list_devices"),
    ("POST", re.compile(rf"^{_PROJECT}/devices$"), "push_devices"),
    ("GET", re.compile(rf"^{_PROJECT}/passes$"), "list_passes"),
    ("POST", re.compile(rf"^{_PROJECT}/passes$"), "run_pass"),
    ("GET", re.compile(rf"^{_PROJECT}/passes/(?P<id>[A-Za-z0-9_\-]+)$"), "get_pass"),
    ("GET", re.compile(rf"^{_PROJECT}/report$"), "latest_report"),
    ("GET", re.compile(rf"^{_PROJECT}/entities$"), "list_entities"),
    ("GET", re.compile(rf"^{_PROJECT}/entities/(?P<id>[A-Za-z0-9_\-]+)$"), "get_entity"),
    ("GET", re.compile(rf"^{_PROJECT}/deviations$"), "list_deviations"),
    ("GET", re.compile(rf"^{_PROJECT}/questions$"), "list_questions"),
    ("GET", re.compile(rf"^{_PROJECT}/corrections$"), "list_corrections"),
    ("POST", re.compile(rf"^{_PROJECT}/corrections$"), "post_correction"),
    ("GET", re.compile(rf"^{_PROJECT}/punchlist$"), "punchlist"),
    ("GET", re.compile(rf"^{_PROJECT}/captures$"), "list_captures"),
    ("POST", re.compile(rf"^{_PROJECT}/captures$"), "post_capture"),
    ("GET", re.compile(rf"^{_PROJECT}/risks$"), "list_risks"),
    ("POST", re.compile(rf"^{_PROJECT}/risks$"), "post_risk"),
    ("DELETE", re.compile(rf"^{_PROJECT}/risks/(?P<id>[A-Za-z0-9_\-]+)$"), "delete_risk"),
    ("GET", re.compile(rf"^{_PROJECT}/freshness$"), "freshness"),
    ("GET", re.compile(rf"^{_PROJECT}/projection$"), "get_projection"),
    ("POST", re.compile(rf"^{_PROJECT}/projection$"), "write_projection"),
]


def make_handler(ts_pool, store_factory, repo_factory=None):
    """`repo_factory(conn)` defaults to db.PgRepo; tests pass one that
    returns a MemoryRepo so the contract tests need no Postgres."""
    repo_factory = repo_factory or db.PgRepo

    class CommissioningHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS"
        ERROR_TYPES = (*BaseAPIHandler.ERROR_TYPES,)

        # --- dispatch -----------------------------------------------------
        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if method == "GET" and path == "/openapi.yaml":
                self.serve_openapi_spec("api.get_openapi_spec", OPENAPI_SPEC_PATH)
                return
            if method == "GET" and path == "/docs":
                docs_ui.serve(self)
                return
            if not gateway_auth.request_came_through_gateway(self):
                self.send_response(401)
                self.end_headers()
                return
            for m, rx, name in ROUTES:
                hit = rx.match(path)
                if m == method and hit:
                    params = {**hit.groupdict(), **{k: v[0] for k, v in parse_qs(parsed.query).items()}}
                    self.handle_traced(f"api.{name}", lambda span, name=name, params=params: self._run(name, params, span))
                    return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _run(self, name: str, params: dict, span) -> None:
            with ts_pool.connection() as conn:
                repo = repo_factory(conn)
                fn = getattr(self, f"_{name}")
                try:
                    fn(repo, params, span)
                except KeyError as exc:
                    # engine raises KeyError for unknown ids; a missing body
                    # field is also a KeyError - tell them apart by message.
                    msg = str(exc).strip("'\"")
                    if "not found" in msg:
                        respond_error(self, 404, msg)
                    else:
                        respond_error(self, 400, f"missing field {msg}")

        def _who(self) -> str:
            return self.headers.get("X-User") or "unknown"

        def _project(self, repo, params) -> dict:
            p = repo.get_project(params["pid"])
            if p is None:
                raise KeyError(f"project {params['pid']!r} not found")
            return p

        # --- handlers -----------------------------------------------------
        def _vocabulary(self, repo, params, span) -> None:
            respond_json(self, 200, {
                "phases": list(PHASES),
                "roles": list(V.ROLES),
                "equipment_types": {k: {"aliases": v["aliases"], "description": v.get("description"), "brick": v.get("brick"), "haystack": v.get("haystack")} for k, v in V.EQUIPMENT_TYPES.items()},
                "correction_kinds": ["confirm_mapping", "correct_mapping", "reject_mapping", "correct_type", "confirm_topology", "naming_alias", "resolve_deviation", "accept_spec_entry", "correct_spec_entry"],
                "confidence_levels": ["confirmed", "high", "medium", "low", "unmatched"],
                "verification_rungs": {1: "existence", 2: "liveness", 3: "responsiveness", 4: "command response"},
            })

        def _list_projects(self, repo, params, span) -> None:
            respond_json(self, 200, repo.list_projects())

        def _create_project(self, repo, params, span) -> None:
            body = self.read_json_body()
            project = engine.new_project(body["name"], body.get("phase"), body.get("cidr_scopes"), body.get("settings"))
            repo.put_project(project)
            span.set_attribute("project_id", project["id"])
            respond_json(self, 201, project)

        def _get_project(self, repo, params, span) -> None:
            p = self._project(repo, params)
            passes = repo.list_docs("cx_passes", p["id"], limit=1)
            respond_json(self, 200, {**p, "last_pass": passes[0]["pass"] if passes else None, "summary": passes[0].get("summary") if passes else None})

        def _delete_project(self, repo, params, span) -> None:
            if not repo.delete_project(params["pid"]):
                raise KeyError(f"project {params['pid']!r} not found")
            self.send_response(204)
            self.end_headers()

        def _set_phase(self, repo, params, span) -> None:
            p = self._project(repo, params)
            body = self.read_json_body()
            phase = body.get("phase")
            if phase is not None and phase not in PHASES:
                raise ValueError(f"phase must be one of {', '.join(PHASES)} or null to let the agent infer it")
            p.update({"phase": phase, "phase_assumed": phase is None, "phase_rationale": (f"stated by {self._who()}" + (f": {body['note']}" if body.get("note") else "")) if phase else None, "updated_at": now_iso()})
            if body.get("cidr_scopes") is not None:
                p["cidr_scopes"] = list(body["cidr_scopes"])
            repo.put_project(p)
            respond_json(self, 200, p)

        def _get_spec(self, repo, params, span) -> None:
            self._project(repo, params)
            row = repo.get("cx_spec", params["pid"], "spec")
            if row is None:
                respond_json(self, 200, {"material": None, "model": None})
                return
            respond_json(self, 200, {"material": row.get("material"), "model": row.get("model")})

        def _put_spec(self, repo, params, span) -> None:
            self._project(repo, params)
            body = self.read_json_body()
            material = body.get("material", body)
            if not isinstance(material, dict) or not isinstance(material.get("equipment", []), list):
                raise TypeError("spec material must be an object with an 'equipment' list (see the OpenAPI spec for the row shape)")
            model = spec_model.build_spec_model(material)
            repo.upsert("cx_spec", params["pid"], {"id": "spec", "material": material, "model": model})
            span.set_attribute("equipment_count", len(model["equipment"]))
            respond_json(self, 200, {"model": model})

        def _list_devices(self, repo, params, span) -> None:
            self._project(repo, params)
            respond_json(self, 200, repo.list_docs("cx_devices", params["pid"]))

        def _push_devices(self, repo, params, span) -> None:
            """Device rows as FBF's GET /devices returns them, each optionally
            carrying `objects` (its POST /learn result). Anything else with
            the same field names is accepted too - the shape is FBF's, the
            source needn't be."""
            self._project(repo, params)
            body = self.read_json_body()
            rows = body.get("devices", body if isinstance(body, list) else None)
            if not isinstance(rows, list):
                raise TypeError("body must be {'devices': [...]} or a bare list of device rows")
            stored = []
            for row in rows:
                d = discovery.device_from_fbf(row, row.get("learned"))
                doc = {**d, "id": d["field_id"], "pushed_at": now_iso(), "pushed_by": self._who()}
                repo.upsert("cx_devices", params["pid"], doc)
                stored.append(doc["id"])
            span.set_attribute("device_count", len(stored))
            respond_json(self, 200, {"stored": stored})

        def _list_passes(self, repo, params, span) -> None:
            self._project(repo, params)
            passes = repo.list_docs("cx_passes", params["pid"], limit=int(params.get("limit", 20)))
            respond_json(self, 200, [{"id": p["id"], **p["pass"], "summary": p.get("summary")} for p in passes])

        def _run_pass(self, repo, params, span) -> None:
            self._project(repo, params)
            body = self.read_json_body()
            store = store_factory() if body.get("use_graph", True) else None
            report = engine.run_pass(repo, params["pid"], store=store, trigger=f"api:{self._who()}", project_to_graph=bool(body.get("project_to_graph", False)))
            span.set_attribute("entities", report["summary"]["entities"])
            respond_json(self, 200, report, default=str)

        def _get_pass(self, repo, params, span) -> None:
            self._project(repo, params)
            rep = repo.get("cx_passes", params["pid"], params["id"])
            if rep is None:
                raise KeyError(f"pass {params['id']!r} not found")
            respond_json(self, 200, rep, default=str)

        def _latest_report(self, repo, params, span) -> None:
            self._project(repo, params)
            passes = repo.list_docs("cx_passes", params["pid"], limit=1)
            if not passes:
                respond_json(self, 200, {"report": None, "note": "no pass has run yet - POST /projects/{id}/passes"})
                return
            respond_json(self, 200, passes[0], default=str)

        def _list_entities(self, repo, params, span) -> None:
            self._project(repo, params)
            ents = repo.list_docs("cx_entities", params["pid"])
            if params.get("confidence"):
                ents = [e for e in ents if e.get("confidence") == params["confidence"]]
            respond_json(self, 200, ents)

        def _get_entity(self, repo, params, span) -> None:
            self._project(repo, params)
            e = repo.get("cx_entities", params["pid"], params["id"])
            if e is None:
                raise KeyError(f"entity {params['id']!r} not found")
            devs = {d["id"]: d for d in repo.list_docs("cx_deviations", params["pid"])}
            respond_json(self, 200, {**e, "deviation_records": [devs[i] for i in e.get("deviations", []) if i in devs]})

        def _list_deviations(self, repo, params, span) -> None:
            self._project(repo, params)
            devs = repo.list_docs("cx_deviations", params["pid"])
            status = params.get("status", "open")
            if status != "all":
                devs = [d for d in devs if d.get("status") == status]
            respond_json(self, 200, devs)

        def _list_questions(self, repo, params, span) -> None:
            self._project(repo, params)
            ents = repo.list_docs("cx_entities", params["pid"])
            devs = repo.list_docs("cx_deviations", params["pid"])
            spec = (repo.get("cx_spec", params["pid"], "spec") or {}).get("model")
            respond_json(self, 200, engine.escalations(ents, devs, spec))  # type: ignore[arg-type]

        def _list_corrections(self, repo, params, span) -> None:
            self._project(repo, params)
            respond_json(self, 200, repo.list_docs("cx_corrections", params["pid"]))

        def _post_correction(self, repo, params, span) -> None:
            self._project(repo, params)
            body = self.read_json_body()
            store = store_factory() if body.get("use_graph", True) else None
            report = engine.apply_correction(repo, params["pid"], body, by=self._who(), store=store)
            span.set_attribute("moved", len(report["confidence_movement"]))
            respond_json(self, 200, {"correction": report["correction"], "effect": report["correction_effect"], "confidence_movement": report["confidence_movement"], "pass_id": report["id"], "summary": report["summary"]}, default=str)

        def _punchlist(self, repo, params, span) -> None:
            self._project(repo, params)
            items = repo.list_docs("cx_punch", params["pid"])
            status = params.get("status", "open")
            if status != "all":
                items = [i for i in items if i.get("status") == status]
            ordered = punchlist.order_by_route(items)  # type: ignore[arg-type]
            respond_json(self, 200, {"items": ordered, "by_location": punchlist.group_by_location(ordered)})  # type: ignore[arg-type]

        def _list_captures(self, repo, params, span) -> None:
            self._project(repo, params)
            respond_json(self, 200, repo.list_docs("cx_captures", params["pid"]))

        def _post_capture(self, repo, params, span) -> None:
            self._project(repo, params)
            body = self.read_json_body()
            if "legible" not in body and "unreadable" not in body:
                raise ValueError("a capture needs 'legible' (what could be read, '?' for unread characters) and/or 'unreadable' (what could not)")
            body.setdefault("engineer", self._who())
            respond_json(self, 201, engine.ingest_capture(repo, params["pid"], body), default=str)

        def _list_risks(self, repo, params, span) -> None:
            self._project(repo, params)
            respond_json(self, 200, repo.list_docs("cx_risks", params["pid"]))

        def _post_risk(self, repo, params, span) -> None:
            self._project(repo, params)
            body = self.read_json_body()
            if not (body.get("entity_id") or body.get("field_id") or body.get("spec_tag")):
                raise ValueError("a risk must name what it covers: entity_id, field_id or spec_tag")
            risk = reconcile.new_risk(body.get("entity_id"), body.get("field_id"), body.get("spec_tag"), body["description"], body.get("accepted_by") or self._who(), body.get("review_by"))
            repo.upsert("cx_risks", params["pid"], risk)  # type: ignore[arg-type]
            respond_json(self, 201, risk)

        def _delete_risk(self, repo, params, span) -> None:
            self._project(repo, params)
            if not repo.delete("cx_risks", params["pid"], params["id"]):
                raise KeyError(f"risk {params['id']!r} not found")
            self.send_response(204)
            self.end_headers()

        def _freshness(self, repo, params, span) -> None:
            p = self._project(repo, params)
            ents = repo.list_docs("cx_entities", params["pid"])
            cfg = {**reconcile.DEFAULTS, **(p.get("settings", {}).get("reconcile") or {})}
            respond_json(self, 200, reconcile.freshness(ents, datetime.now(timezone.utc), cfg))  # type: ignore[arg-type]

        def _get_projection(self, repo, params, span) -> None:
            self._project(repo, params)
            ents = repo.list_docs("cx_entities", params["pid"])
            include = params.get("include_unsettled", "false").lower() == "true"
            fmt = params.get("format", "brick")
            if fmt == "brick":
                respond_json(self, 200, projection.project_brick(ents, include_unsettled=include).as_dict())  # type: ignore[arg-type]
            elif fmt == "haystack":
                respond_json(self, 200, projection.project_haystack(ents, include_unsettled=include))  # type: ignore[arg-type]
            else:
                raise ValueError("format must be brick or haystack")

        def _write_projection(self, repo, params, span) -> None:
            self._project(repo, params)
            body = self.read_json_body()
            ents = repo.list_docs("cx_entities", params["pid"])
            res = projection.project_brick(ents, include_unsettled=bool(body.get("include_unsettled", False)))  # type: ignore[arg-type]
            store = store_factory()
            if store is None:
                respond_error(self, 503, "no graph store configured (OXIGRAPH_URL) - nothing to write to")
                return
            n = projection.write_brick(store, res)
            span.set_attribute("triples", n)
            respond_json(self, 200, {"written": n, **res.as_dict()})

    return CommissioningHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--no-graph", action="store_true", help="do not read/write Oxigraph; passes see only pushed device data")
    args = parser.parse_args()

    tracing.init_tracing("timberdoodle-commissioning-api")
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        db.ensure_schema(conn)

    def store_factory():
        return None if args.no_graph else RemoteStore()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(ts_pool, store_factory))
    print(f"commissioning_api listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
