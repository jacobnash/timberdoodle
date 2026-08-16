"""
Outbound webhook delivery - HMAC-signed, retried, timed out. Runs off the
MQTT callback thread (see fault_detector.py): a slow/dead URL must never
stall fault evaluation for every other point behind it.
"""

import hashlib
import hmac
import json
import time

import requests

TIMEOUT_SECONDS = 5.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def matches_filter(webhook: dict, rule_id: str) -> bool:
    """No filter (missing/empty/None) means 'every fault' - the safe,
    permissive default for a webhook that didn't ask to be scoped."""
    filt = webhook.get("filter")
    if not filt:
        return True
    filter_rule_id = filt.get("rule_id")
    return filter_rule_id is None or filter_rule_id == rule_id


def deliver(
    url: str,
    secret: str,
    payload: dict,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> tuple[bool, str | None]:
    """Signs the EXACT raw bytes sent, not a re-serialized dict - a real,
    common signing bug: serializing the same dict twice isn't guaranteed
    byte-identical (key order, float formatting), so the signature and
    the body must come from one serialization, sent as raw `data=`, not
    `json=` (which would re-serialize internally). Returns
    (success, last_error) - the caller decides what to do with a
    failure (fault_detector.py records it via faults.record_webhook_failure)."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Timberdoodle-Signature": sign(secret, body)}

    last_error = None
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(backoff_base_seconds * (2 ** (attempt - 1)))
        try:
            resp = requests.post(url, data=body, headers=headers, timeout=timeout_seconds)
            resp.raise_for_status()
            return True, None
        except requests.RequestException as exc:
            last_error = str(exc)

    return False, last_error
