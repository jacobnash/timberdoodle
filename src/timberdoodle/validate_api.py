"""
POST /validate - checks the live entity graph against Brick's own SHACL
shapes, on demand. Same shape as ingest_api.py/fault_api.py/
derivation_api.py: stdlib http.server, own process/port, OTel span per
request, spec served straight off disk. Always validates *current live*
Oxigraph state, no request body - keeps v1 simple (see
shacl_validate.py / todo/shacl-validation-and-service.md).
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from timberdoodle import docs_ui, gateway_auth, tracing
from timberdoodle.remote_store import RemoteStore
from timberdoodle.shacl_validate import graph_from_remote_store, load_shapes, validate_graph

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "validate-api-openapi.yaml")


def _respond_json(handler, status: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(store):
    class ValidateHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path == "/validate":
                if not gateway_auth.request_came_through_gateway(self):
                    self.send_response(401)
                    self.end_headers()
                    return
                self._post_validate()
                return
            self.send_response(404)
            self.end_headers()

        def _post_validate(self) -> None:
            with tracer.start_as_current_span("validate_api.post_validate") as span:
                data_graph = graph_from_remote_store(store)
                span.set_attribute("triple_count", len(data_graph))
                result = validate_graph(data_graph)
                span.set_attribute("conforms", result["conforms"])
                span.set_attribute("violation_count", len(result["violations"]))
                # Conformance is data in the response, not an error status -
                # matching derivation_api.py's /dry-run: 200 either way.
                _respond_json(self, 200, result)

        def do_GET(self):
            if self.path == "/openapi.yaml":
                self._serve_openapi_spec()
                return
            if self.path == "/docs":
                docs_ui.serve(self)
                return
            self.send_response(404)
            self.end_headers()

        def _serve_openapi_spec(self) -> None:
            with tracer.start_as_current_span("validate_api.get_openapi_spec"):
                with open(OPENAPI_SPEC_PATH, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/yaml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, fmt, *args):
            pass  # quiet by default; tracing carries the real signal

    return ValidateHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8005)
    args = parser.parse_args()

    tracing.init_tracing("timberdoodle-validate-api")

    store = RemoteStore()
    load_shapes()  # parse once at startup, not on the first request

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store))
    print(f"validate API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
