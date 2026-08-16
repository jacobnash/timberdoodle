"""
CRUD for derivations, plus test/dry-run validation and per-target health
management - mirrors fault_api.py's exact shape (stdlib http.server, one
Postgres connection per request from a pool, OTel span per request, spec
served straight off disk). Own process, own port - same reasoning as
fault_api.py getting its own OpenAPI spec rather than extending
ingest_api.py's or fault_api.py's.

POST /derivations and POST /derivations/test both execute submitted Python
(via sandbox.compile_fn/run_test_cases) as part of validation - see
derivation-api-openapi.yaml's security note. This must never be exposed
past a fully trusted, localhost-only boundary.
"""

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from timberdoodle import derivation_engine, derivation_health, docs_ui, json_store, sandbox, tracing
from timberdoodle.remote_store import RemoteStore
from timberdoodle.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "derivation-api-openapi.yaml")

# Guards read-modify-write on derivations.json against concurrent requests
# within this one process - same documented, accepted gap as fault_api.py's
# rules.json/webhooks.json (json_store.py's own docstring): not a
# cross-process concurrent-writer solution.
_file_lock = threading.Lock()

_TARGETS_RE = re.compile(r"^/derivations/([^/]+)/targets$")
_ENABLE_TARGET_RE = re.compile(r"^/derivations/([^/]+)/targets/([^/]+)/enable$")


def _respond_json(handler, status: int, payload) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _respond_error(handler, status: int, message: str) -> None:
    _respond_json(handler, status, {"error": message})


def _validate_fn_and_test_cases(fn_source: str, test_cases: list[dict]) -> None:
    if not test_cases:
        raise ValueError("test_cases must be a non-empty list - submitted code that will run unattended needs test coverage")
    fn = sandbox.compile_fn(fn_source)
    results = sandbox.run_test_cases(fn, test_cases)
    failed = [r for r in results if not r["passed"]]
    if failed:
        raise ValueError(f"test_cases failed: {json.dumps(failed, default=str)}")


def make_handler(derivations_path: str, store, ts_pool):
    class DerivationHandler(BaseHTTPRequestHandler):
        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def do_POST(self):
            if self.path == "/derivations":
                self._handle("api.post_derivations", self._create_derivation)
                return
            if self.path == "/derivations/test":
                self._handle("api.test_derivation", self._test_derivation)
                return
            if self.path == "/derivations/dry-run":
                self._handle("api.dry_run_derivation", self._dry_run)
                return
            match = _ENABLE_TARGET_RE.match(self.path)
            if match:
                derivation_id, target_uri = match.group(1), unquote(match.group(2))
                self._handle("api.enable_target", lambda span: self._enable_target(derivation_id, target_uri, span))
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path == "/derivations":
                self._handle("api.get_derivations", lambda span: _respond_json(self, 200, json_store.load(derivations_path)))
                return
            match = _TARGETS_RE.match(self.path)
            if match:
                self._handle("api.get_targets", lambda span: self._list_targets(match.group(1), span))
                return
            if self.path == "/openapi.yaml":
                self._serve_openapi_spec()
                return
            if self.path == "/docs":
                docs_ui.serve(self)
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if self.path.startswith("/derivations/"):
                derivation_id = self.path[len("/derivations/"):]
                self._handle("api.delete_derivation", lambda span: self._delete(derivation_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def _handle(self, span_name: str, fn) -> None:
            with tracer.start_as_current_span(span_name) as span:
                try:
                    fn(span)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError, sandbox.SandboxError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, str(exc))

        def _create_derivation(self, span) -> None:
            body = self._read_json_body()
            fn_source = body["fn_source"]
            test_cases = body["test_cases"]
            _validate_fn_and_test_cases(fn_source, test_cases)

            reserved = {"name", "kind", "fn_source", "test_cases", "window_seconds", "output", "depends_on"}
            derivation = {
                "id": json_store.new_id(),
                "name": body["name"],
                "kind": body.get("kind", "formula"),
                "fn_source": fn_source,
                "test_cases": test_cases,
                "window_seconds": body.get("window_seconds", 3600),
                "output": body.get("output", {}),
                "depends_on": body.get("depends_on", []),
                **{k: v for k, v in body.items() if k not in reserved},
            }
            with _file_lock:
                derivations = json_store.load(derivations_path)
                derivations.append(derivation)
                json_store.save(derivations_path, derivations)
            span.set_attribute("derivation_id", derivation["id"])
            _respond_json(self, 201, derivation)

        def _test_derivation(self, span) -> None:
            body = self._read_json_body()
            fn = sandbox.compile_fn(body["fn_source"])
            results = sandbox.run_test_cases(fn, body.get("test_cases", []))
            span.set_attribute("passed", all(r["passed"] for r in results))
            _respond_json(self, 200, results)

        def _dry_run(self, span) -> None:
            body = self._read_json_body()
            if "id" in body and set(body.keys()) <= {"id"}:
                derivations = json_store.load(derivations_path)
                derivation = next((d for d in derivations if d["id"] == body["id"]), None)
                if derivation is None:
                    raise ValueError(f"no derivation with id {body['id']!r}")
            else:
                derivation = {**body, "id": body.get("id", "draft")}
                sandbox.compile_fn(derivation["fn_source"])  # fail fast with a clear 400 instead of a silently-skipped trace

            span.set_attribute("derivation_id", derivation["id"])
            with ts_pool.connection() as conn:
                trace = derivation_engine.evaluate_derivations(store, conn, conn, conn, [derivation], dry_run=True)
            span.set_attribute("target_count", len(trace))
            _respond_json(self, 200, trace)

        def _list_targets(self, derivation_id: str, span) -> None:
            with ts_pool.connection() as conn:
                targets = derivation_health.list_targets(conn, derivation_id)
            span.set_attribute("count", len(targets))
            _respond_json(self, 200, targets)

        def _enable_target(self, derivation_id: str, target_uri: str, span) -> None:
            span.set_attribute("derivation_id", derivation_id)
            span.set_attribute("target", target_uri)
            with ts_pool.connection() as conn:
                derivation_health.enable_target(conn, derivation_id, target_uri)
            _respond_json(self, 200, {"derivation_id": derivation_id, "target_uri": target_uri, "disabled": False})

        def _delete(self, derivation_id: str, span) -> None:
            span.set_attribute("id", derivation_id)
            with _file_lock:
                derivations = json_store.load(derivations_path)
                remaining = [d for d in derivations if d["id"] != derivation_id]
                found = len(remaining) != len(derivations)
                if found:
                    json_store.save(derivations_path, remaining)
            span.set_attribute("found", found)
            self.send_response(204 if found else 404)
            self.end_headers()

        def _serve_openapi_spec(self) -> None:
            with tracer.start_as_current_span("api.get_openapi_spec"):
                with open(OPENAPI_SPEC_PATH, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/yaml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, fmt, *args):
            pass  # quiet by default; tracing carries the real signal

    return DerivationHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--derivations-file", default="derivations.json")
    parser.add_argument("--oxigraph-url", default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"))
    args = parser.parse_args()

    tracing.init_tracing("timberdoodle-derivation-api")

    store = RemoteStore(args.oxigraph_url)
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        derivation_health.ensure_schema(conn)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.derivations_file, store, ts_pool))
    print(f"derivation API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
