"""
Identity service: login, API-key issuance/revocation, org/site directory
CRUD, and GET /internal/revoked-jtis (consumed by the gateway's
js_periodic poller - container-internal only, no public gateway route).
Same shape as fault_api.py: stdlib http.server, one Postgres connection
per request from a pool, OTel span per request, spec served straight off
disk.

Role/site/rate-limit enforcement happens in the gateway (gateway/njs/),
not here - every handler below just does the one thing it's for. The
exception is gateway_auth.request_came_through_gateway(), checked on
every route except /auth/login (which can't have gone through the
gateway's js_access yet - it's the request that gets you a token in the
first place) and /internal/revoked-jtis (never reachable through the
public gateway at all, so the header is meaningless there).
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from timberdoodle import auth, docs_ui, gateway_auth, tracing
from timberdoodle.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

OPENAPI_SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "auth-api-openapi.yaml")


def _respond_json(handler, status: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _respond_error(handler, status: int, message: str) -> None:
    _respond_json(handler, status, {"error": message})


def make_handler(ts_pool):
    class AuthHandler(BaseHTTPRequestHandler):
        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def _handle(self, span_name: str, fn) -> None:
            with tracer.start_as_current_span(span_name) as span:
                try:
                    fn(span)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    span.set_attribute("error", str(exc))
                    _respond_error(self, 400, str(exc))

        def _require_gateway(self) -> bool:
            if gateway_auth.request_came_through_gateway(self):
                return True
            _respond_error(self, 401, "request did not come through the gateway")
            return False

        def _require_caller(self, span) -> dict | None:
            claims = auth.decode_own_token(self.headers.get("Authorization"))
            if not claims:
                _respond_error(self, 401, "missing or invalid bearer token")
                return None
            span.set_attribute("caller_sub", claims["sub"])
            return claims

        def do_POST(self):
            # Bare paths, matching every other service's convention - the
            # gateway's `location /auth/ { proxy_pass http://auth_api:8006/; }`
            # strips the /auth prefix before it reaches here.
            path = urlparse(self.path).path
            if path == "/login":
                self._handle("auth_api.post_login", self._login)
                return
            if path == "/orgs":
                # Public, like /login - creating a brand-new org has to
                # be reachable with zero prior credentials (its own
                # payload creates the first admin user in the same
                # call), same bootstrap reasoning as create_org() in
                # auth.py. gateway/njs/policy.js marks this route public
                # to match.
                self._handle("auth_api.post_orgs", self._create_org)
                return
            if not self._require_gateway():
                return
            if path == "/logout":
                self._handle("auth_api.post_logout", self._logout)
                return
            if path == "/api-keys":
                self._handle("auth_api.post_api_keys", self._create_api_key)
                return
            if path == "/sites":
                self._handle("auth_api.post_sites", self._create_site)
                return
            if path == "/users":
                self._handle("auth_api.post_users", self._create_user)
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/internal/revoked-jtis":
                # Never routed through the public gateway (no /auth/
                # prefix, and no gateway location proxies this path at
                # all) - reachable only container-to-container, so no
                # gateway check here either.
                self._handle("auth_api.get_revoked_jtis", self._get_revoked_jtis)
                return
            if path == "/openapi.yaml":
                self._serve_openapi_spec()
                return
            if path == "/docs":
                docs_ui.serve(self)
                return
            if not self._require_gateway():
                return
            if path == "/api-keys":
                self._handle("auth_api.get_api_keys", self._list_api_keys)
                return
            if path == "/me":
                self._handle("auth_api.get_me", self._get_me)
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self):
            if not self._require_gateway():
                return
            path = urlparse(self.path).path
            if path.startswith("/api-keys/"):
                key_id = path[len("/api-keys/"):]
                self._handle("auth_api.delete_api_key", lambda span: self._delete_api_key(key_id, span))
                return
            self.send_response(404)
            self.end_headers()

        def _login(self, span) -> None:
            body = self._read_json_body()
            email = body["email"]
            password = body["password"]
            with ts_pool.connection() as conn:
                user = auth.get_user_by_email(conn, email)
                if not user or not auth.verify_password(password, user["password_hash"]):
                    span.set_attribute("outcome", "invalid_credentials")
                    _respond_error(self, 401, "invalid email or password")
                    return
                site_ids = auth.user_site_ids(conn, user["id"])
                token = auth.issue_session_token(user, site_ids)
            span.set_attribute("outcome", "ok")
            span.set_attribute("user_id", user["id"])
            _respond_json(self, 200, {
                "token": token,
                "user": {"id": user["id"], "org_id": user["org_id"], "role": user["role"], "site_ids": site_ids},
            })

        def _logout(self, span) -> None:
            claims = auth.decode_own_token(self.headers.get("Authorization"))
            if claims and claims.get("kind") == "api_key":
                with ts_pool.connection() as conn:
                    conn.execute(
                        "INSERT INTO revoked_tokens (jti, revoked_at) VALUES (%s, now()) ON CONFLICT (jti) DO NOTHING",
                        (claims["jti"],),
                    )
            # Session tokens have nothing to do here - their own short
            # expiry is the guard, matching the gateway's revocation
            # design. Always 204: logout is idempotent either way.
            self.send_response(204)
            self.end_headers()

        def _create_api_key(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self._read_json_body()
            role = body["role"]
            site_ids = body.get("site_ids")
            expires_in_seconds = body.get("expires_in_seconds")
            with ts_pool.connection() as conn:
                key = auth.create_api_key(conn, caller["org_id"], role, site_ids, expires_in_seconds)
                token = auth.issue_api_key_token(key)
            span.set_attribute("api_key_id", key["id"])
            _respond_json(self, 201, {"id": key["id"], "role": key["role"], "site_ids": key["site_ids"], "token": token})

        def _list_api_keys(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            with ts_pool.connection() as conn:
                keys = auth.list_api_keys(conn, caller["org_id"])
            _respond_json(self, 200, keys)

        def _delete_api_key(self, key_id: str, span) -> None:
            span.set_attribute("api_key_id", key_id)
            with ts_pool.connection() as conn:
                found = auth.revoke_api_key(conn, key_id)
            span.set_attribute("found", found)
            self.send_response(204 if found else 404)
            self.end_headers()

        def _get_me(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            _respond_json(self, 200, {
                "sub": caller["sub"], "org_id": caller["org_id"], "role": caller["role"],
                "site_ids": caller.get("site_ids", []), "kind": caller.get("kind"),
            })

        def _create_org(self, span) -> None:
            body = self._read_json_body()
            with ts_pool.connection() as conn:
                org = auth.create_org(conn, body["name"], body["admin_email"], body["admin_password"])
            span.set_attribute("org_id", org["id"])
            _respond_json(self, 201, org)

        def _create_site(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self._read_json_body()
            with ts_pool.connection() as conn:
                site = auth.create_site(conn, caller["org_id"], body["name"])
            span.set_attribute("site_id", site["id"])
            _respond_json(self, 201, site)

        def _create_user(self, span) -> None:
            caller = self._require_caller(span)
            if not caller:
                return
            body = self._read_json_body()
            with ts_pool.connection() as conn:
                user = auth.create_user(conn, caller["org_id"], body["email"], body["password"], body["role"])
            span.set_attribute("new_user_id", user["id"])
            _respond_json(self, 201, user)

        def _get_revoked_jtis(self, span) -> None:
            with ts_pool.connection() as conn:
                jtis = auth.list_revoked_jtis(conn)
            span.set_attribute("count", len(jtis))
            _respond_json(self, 200, jtis)

        def _serve_openapi_spec(self) -> None:
            with tracer.start_as_current_span("auth_api.get_openapi_spec"):
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
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, fmt, *args):
            pass  # quiet by default; tracing carries the real signal

    return AuthHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8006)
    args = parser.parse_args()

    if not os.environ.get("TIMBERDOODLE_JWT_SECRET"):
        raise SystemExit(
            "TIMBERDOODLE_JWT_SECRET is not set - auth_api can't issue tokens without it. "
            "Set it in .env (see .env.example) before starting this service."
        )

    tracing.init_tracing("timberdoodle-auth-api")

    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        auth.ensure_schema(conn)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(ts_pool))
    print(f"auth API listening on {args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
