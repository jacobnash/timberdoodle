"""
Fast pure-unit tests for the Haystack JSON scalar decoder, plus live SCRAM
round-trips against a hand-rolled fake Haxall server (same ThreadingHTTPServer
scaffold as test_webhooks.py's _make_receiver()) - the fake server derives
its own SCRAM math independently from the same stdlib primitives, so a
passing round-trip actually proves the client's crypto interops with a
correct implementation, not just that it doesn't crash.

test_about_against_a_real_running_haxall_instance is the one genuine e2e
test: real network calls to a real Haxall/SkySpark server, skipped unless
someone points it at one via the same HAXALL_OSS_URL/HAXALL_SU_USERNAME/
HAXALL_SU_PASSWORD env vars e2e-haxall/ already uses (no Haxall instance is
provisioned by this repo's docker-compose.yml).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from timberdoodle.haystack_client import (
    HaystackAuthError,
    HaystackClient,
    _b64url_decode,
    _b64url_nopad,
    _decode_haystack_scalar,
    _parse_auth_params,
)

TEST_USER = "tduser"
TEST_PASSWORD = "tdpass"
TEST_SALT = b"fixed-test-salt-"
TEST_ITERATIONS = 4096


def test_decode_haystack_scalar_marker():
    assert _decode_haystack_scalar("m:") is True


def test_decode_haystack_scalar_number_with_unit():
    assert _decode_haystack_scalar("n:71.5 °F") == 71.5


def test_decode_haystack_scalar_ref_drops_display_name():
    assert _decode_haystack_scalar("r:abc123 AHU-1 Discharge Temp") == "abc123"


def test_decode_haystack_scalar_datetime_becomes_epoch_seconds():
    from datetime import datetime, timezone

    expected = datetime(2026, 8, 24, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    assert _decode_haystack_scalar("t:2026-08-24T00:00:00Z UTC") == expected


def test_decode_haystack_scalar_passes_through_native_json_types():
    assert _decode_haystack_scalar(True) is True
    assert _decode_haystack_scalar("plain string, no kind prefix") == "plain string, no kind prefix"


def _make_fake_haxall_server(canned_grids: dict = None):
    """Implements just enough of the server side of Haystack's SCRAM
    handshake (https://project-haystack.org/doc/Auth) to authenticate one
    known user/password and hand back canned Haystack-JSON grids - not a
    real Haystack server, just enough to prove the client's handshake and
    request/response parsing actually interop with a correct peer."""
    canned_grids = canned_grids or {}
    handshakes = {}
    bearer_tokens = set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            op = self.path.partition("?")[0].strip("/")
            auth = self.headers.get("Authorization", "")

            if auth.startswith("BEARER "):
                token = _parse_auth_params(auth).get("authToken")
                if token not in bearer_tokens:
                    self.send_response(401)
                    self.end_headers()
                    return
                self._send_json(200, canned_grids.get(op, {"rows": []}))
                return

            if auth.startswith("HELLO "):
                username = _b64url_decode(_parse_auth_params(auth)["username"]).decode()
                token = secrets.token_hex(8)
                handshakes[token] = {"username": username}
                self.send_response(401)
                self.send_header("WWW-Authenticate", f"SCRAM hash=SHA-256, handshakeToken={token}")
                self.end_headers()
                return

            if auth.startswith("SCRAM "):
                self._handle_scram_step(auth, handshakes, bearer_tokens)
                return

            self.send_response(401)
            self.send_header("WWW-Authenticate", "SCRAM hash=SHA-256")
            self.end_headers()

        def _handle_scram_step(self, auth, handshakes, bearer_tokens):
            params = _parse_auth_params(auth)
            state = handshakes.get(params.get("handshakeToken"))
            if state is None:
                self.send_response(401)
                self.end_headers()
                return
            message = _b64url_decode(params["data"]).decode()

            if "p=" not in message:
                # client-first message: "n,,n=<user>,r=<client_nonce>" -
                # split on the double comma, not the first comma, or the
                # gs2 header's own trailing comma leaks into client_first_bare
                client_first_bare = message.split(",,", 1)[1]
                client_nonce = dict(p.split("=", 1) for p in client_first_bare.split(","))["r"]
                combined_nonce = client_nonce + secrets.token_hex(8)
                server_first = f"r={combined_nonce},s={base64.b64encode(TEST_SALT).decode()},i={TEST_ITERATIONS}"
                new_token = secrets.token_hex(8)
                handshakes[new_token] = {
                    "username": state["username"],
                    "client_first_bare": client_first_bare,
                    "server_first": server_first,
                }
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f"SCRAM handshakeToken={new_token}, hash=SHA-256, data={_b64url_nopad(server_first.encode())}",
                )
                self.end_headers()
                return

            # client-final message: "c=biws,r=<combined_nonce>,p=<proof>"
            fields = dict(p.split("=", 1) for p in message.split(","))
            client_final_no_proof = f"c={fields['c']},r={fields['r']}"
            auth_message = f"{state['client_first_bare']},{state['server_first']},{client_final_no_proof}"

            if state["username"] != TEST_USER:
                self.send_response(401)
                self.end_headers()
                return
            salted_password = hashlib.pbkdf2_hmac("sha256", TEST_PASSWORD.encode(), TEST_SALT, TEST_ITERATIONS, dklen=32)
            client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
            stored_key = hashlib.sha256(client_key).digest()
            client_signature = hmac.new(stored_key, auth_message.encode(), hashlib.sha256).digest()
            expected_proof = bytes(a ^ b for a, b in zip(client_key, client_signature))

            if not hmac.compare_digest(expected_proof, base64.b64decode(fields["p"])):
                self.send_response(401)
                self.end_headers()
                return
            bearer_token = secrets.token_hex(8)
            bearer_tokens.add(bearer_token)
            self.send_response(200)
            self.send_header("Authentication-Info", f"authToken={bearer_token}, hash=SHA-256")
            self.end_headers()

        def _send_json(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, bearer_tokens


@pytest.mark.integration
def test_about_succeeds_after_full_scram_handshake():
    server, _ = _make_fake_haxall_server({"about": {"rows": [{"productName": "FakeHaxall", "version": "n:4.0"}]}})
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        result = client.about()
        assert result["productName"] == "FakeHaxall"
        assert result["version"] == 4.0
    finally:
        server.shutdown()


@pytest.mark.integration
def test_read_and_his_read_reuse_the_authenticated_session():
    server, _ = _make_fake_haxall_server(
        {
            "read": {"rows": [{"id": "r:p1 Zone Temp", "curVal": "n:71.0"}]},
            "hisRead": {"rows": [{"ts": "t:2026-08-24T00:00:00Z UTC", "val": "n:71.5"}]},
        }
    )
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        points = client.read("point and temp")
        assert points[0]["id"] == "p1"
        history = client.his_read("@p1", "today")
        assert history[0]["val"] == 71.5
    finally:
        server.shutdown()


@pytest.mark.integration
def test_wrong_password_raises_a_clear_auth_error():
    server, _ = _make_fake_haxall_server()
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, "wrong-password")
        with pytest.raises(HaystackAuthError):
            client.about()
    finally:
        server.shutdown()


@pytest.mark.integration
def test_revoked_bearer_token_triggers_one_reauth_then_succeeds():
    server, bearer_tokens = _make_fake_haxall_server({"about": {"rows": [{"productName": "FakeHaxall"}]}})
    try:
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        client.about()
        bearer_tokens.clear()  # simulate the server expiring/revoking the token
        result = client.about()  # should silently re-auth once and succeed
        assert result["productName"] == "FakeHaxall"
    finally:
        server.shutdown()


HAXALL_OSS_URL = os.environ.get("HAXALL_OSS_URL", "http://localhost:8280")
HAXALL_SU_USERNAME = os.environ.get("HAXALL_SU_USERNAME")
HAXALL_SU_PASSWORD = os.environ.get("HAXALL_SU_PASSWORD", "")


@pytest.mark.integration
@pytest.mark.skipif(
    not HAXALL_SU_USERNAME,
    reason="requires a real running Haxall instance - set HAXALL_SU_USERNAME (and HAXALL_SU_PASSWORD, "
    "HAXALL_OSS_URL if not localhost:8280) to the same env vars e2e-haxall/ uses to run this",
)
def test_about_against_a_real_running_haxall_instance():
    client = HaystackClient(HAXALL_OSS_URL, HAXALL_SU_USERNAME, HAXALL_SU_PASSWORD)
    result = client.about()
    assert "productName" in result
