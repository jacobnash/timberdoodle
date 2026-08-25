"""
CRUD for fault-detection rules and webhook subscriptions, plus GET /faults
- mirrors ingest_api.py's exact shape: stdlib http.server, one Postgres
connection per request from a pool, OTel span per request, spec served
straight off disk. Own process, own port - a second, independently-run
API, which is exactly why it gets its own OpenAPI spec file rather than
extending openapi.yaml (see fault-api-openapi.yaml's own note on this).
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from timberdoodle import docs_ui, json_store, tracing
from timberdoodle.faults import enable_webhook, ensure_schema, list_faults, list_webhook_health
from timberdoodle.timeseries import connect_pool
from timberdoodle.webhooks import validate_url

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "fault-api-openapi.yaml")

# Guards read-modify-write on rules.json/webhooks.json against concurrent
# requests within this one process (ThreadingHTTPServer = one thread per
# request). Does NOT solve cross-process concurrent writers - that's a
# documented, accepted gap (json_store.py's own docstring), unchanged here.
_file_lock = threading.Lock()


def _respond_json(handler, status: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _respond_error(handler, status: int, message: str) -> None:
    _respond_json(handler, status, {"error": message})


def _redact_webhook(webhook: dict) -> dict:
    return {**webhook, "secret": None}


def make_handler(rules_path: str, webhooks_path: str, ts_pool):
    class FaultHandler(BaseHTTPRequestHandler):
        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def do_POST(self):
            if self.path == "/rules":
                self._handle("api.post_rules", self._create_rule)
                return
            if self.path == "/webhooks":
                self._handle("api.post_webhooks", self._create_webhook)
                return
            if self.path.startswith("/webhooks/") and self.path.endswith("/enable"):
                webhook_id = self.path[len("/webhooks/"):-len("/enable")]
                self._handle("api.post_webhook_enable", lambda span: self._enable_webhook(webhook_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path == "/rules":
                self._handle("api.get_rules", lambda span: _respond_json(self, 200, json_store.load(rules_path)))
                return
            if self.path == "/webhooks":
                def handle(span):
                    with ts_pool.connection() as conn:
                        health = list_webhook_health(conn)
                    default_health = {"disabled": False, "consecutive_failures": 0, "last_error": None}
                    webhooks = [
                        {**_redact_webhook(w), **health.get(w["id"], default_health)}
                        for w in json_store.load(webhooks_path)
                    ]
                    _respond_json(self, 200, webhooks)

                self._handle("api.get_webhooks", handle)
                return
            if self.path.startswith("/faults"):
                self._handle("api.get_faults", self._list_faults)
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
            if self.path.startswith("/rules/"):
                rule_id = self.path[len("/rules/"):]
                self._handle("api.delete_rule", lambda span: self._delete(rules_path, rule_id, span))
                return
            if self.path.startswith("/webhooks/"):
                webhook_id = self.path[len("/webhooks/"):]
                self._handle("api.delete_webhook", lambda span: self._delete(webhooks_path, webhook_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def _handle(self, span_name: str, fn) -> None:
            with tracer.start_as_current_span(span_name) as span:
                try:
                    fn(span)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, str(exc))

        def _create_rule(self, span) -> None:
            body = self._read_json_body()
            rule = {
                "id": json_store.new_id(),
                "name": body["name"],
                "mode": body["mode"],  # "cur" | "his"
                "type": body["type"],  # "range" | "stuck" | "stale"
                "applies_to": body["applies_to"],
                **{k: v for k, v in body.items() if k not in ("name", "mode", "type", "applies_to")},
            }
            with _file_lock:
                rules = json_store.load(rules_path)
                rules.append(rule)
                json_store.save(rules_path, rules)
            span.set_attribute("rule_id", rule["id"])
            _respond_json(self, 201, rule)

        def _create_webhook(self, span) -> None:
            body = self._read_json_body()
            # Same deployment-level escape hatch as fault_detector.py's
            # delivery-time check - never a per-request/per-webhook toggle.
            allow_private = os.environ.get("TIMBERDOODLE_ALLOW_PRIVATE_WEBHOOKS") == "1"
            validate_url(body["url"], allow_private=allow_private)  # raises ValueError -> 400, via _handle's except clause
            webhook = {
                "id": json_store.new_id(),
                "url": body["url"],
                "secret": body["secret"],
                "filter": body.get("filter"),
            }
            with _file_lock:
                webhooks = json_store.load(webhooks_path)
                webhooks.append(webhook)
                json_store.save(webhooks_path, webhooks)
            span.set_attribute("webhook_id", webhook["id"])
            _respond_json(self, 201, webhook)  # secret shown once, at creation - redacted on every later GET

        def _delete(self, path: str, record_id: str, span) -> None:
            span.set_attribute("id", record_id)
            with _file_lock:
                records = json_store.load(path)
                remaining = [r for r in records if r["id"] != record_id]
                found = len(remaining) != len(records)
                if found:
                    json_store.save(path, remaining)
            span.set_attribute("found", found)
            self.send_response(204 if found else 404)
            self.end_headers()

        def _enable_webhook(self, webhook_id: str, span) -> None:
            span.set_attribute("webhook_id", webhook_id)
            webhooks = json_store.load(webhooks_path)
            if not any(w["id"] == webhook_id for w in webhooks):
                self.send_response(404)
                self.end_headers()
                return
            with ts_pool.connection() as conn:
                enable_webhook(conn, webhook_id)
            self.send_response(204)
            self.end_headers()

        def _list_faults(self, span) -> None:
            query = parse_qs(urlparse(self.path).query)
            status = query.get("status", [None])[0]
            point_uri = query.get("point_uri", [None])[0]
            rule_id = query.get("rule_id", [None])[0]
            with ts_pool.connection() as conn:
                faults = list_faults(conn, status=status, point_uri=point_uri, rule_id=rule_id)
            span.set_attribute("count", len(faults))
            _respond_json(self, 200, faults)

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
            # Lets the docs site's live try-it playground (served from a
            # different origin/port) call this API directly from the browser.
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, fmt, *args):
            pass  # quiet by default; tracing carries the real signal

    return FaultHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--rules-file", default="rules.json")
    parser.add_argument("--webhooks-file", default="webhooks.json")
    args = parser.parse_args()

    tracing.init_tracing("timberdoodle-fault-api")

    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        ensure_schema(conn)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.rules_file, args.webhooks_file, ts_pool))
    print(f"fault API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
