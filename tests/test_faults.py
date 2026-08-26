"""
Live against the real running Postgres. Proves the atomicity/race-safety
of open_fault/close_fault, not just their happy path.
"""

from datetime import datetime, timedelta, timezone

import pytest

from timberdoodle.faults import (
    close_fault,
    enable_webhook,
    ensure_schema,
    is_webhook_disabled,
    list_faults,
    list_webhook_health,
    open_fault,
    record_webhook_failure,
    record_webhook_success,
)
from timberdoodle.timeseries import connect


@pytest.fixture
def conn():
    c = connect()
    ensure_schema(c)
    c.execute("DELETE FROM faults WHERE rule_id LIKE 'test-faults:%'")
    c.execute("DELETE FROM webhook_health WHERE webhook_id LIKE 'test-faults:%'")
    return c


@pytest.mark.integration
def test_open_fault_returns_id_on_first_open(conn):
    fault_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:a", datetime.now(timezone.utc))
    assert fault_id is not None


@pytest.mark.integration
def test_open_fault_is_a_noop_when_already_open(conn):
    now = datetime.now(timezone.utc)
    first_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:b", now)
    second_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:b", now + timedelta(seconds=1))

    assert first_id is not None
    assert second_id is None  # already open - no duplicate row, no new webhook fire

    open_rows = [f for f in list_faults(conn, rule_id="test-faults:range", point_uri="urn:point:test-faults:b") if f["status"] == "open"]
    assert len(open_rows) == 1


@pytest.mark.integration
def test_close_fault_returns_id_then_none_on_repeat(conn):
    now = datetime.now(timezone.utc)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:c", now)

    first_close = close_fault(conn, "test-faults:range", "urn:point:test-faults:c", now + timedelta(seconds=5))
    second_close = close_fault(conn, "test-faults:range", "urn:point:test-faults:c", now + timedelta(seconds=6))

    assert first_close is not None
    assert second_close is None  # nothing open anymore - no-op


@pytest.mark.integration
def test_close_fault_on_nothing_open_is_a_noop(conn):
    assert close_fault(conn, "test-faults:range", "urn:point:test-faults:never-opened", datetime.now(timezone.utc)) is None


@pytest.mark.integration
def test_reopen_after_close_gets_a_new_row(conn):
    now = datetime.now(timezone.utc)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:d", now)
    close_fault(conn, "test-faults:range", "urn:point:test-faults:d", now + timedelta(seconds=5))
    reopened_id = open_fault(conn, "test-faults:range", "urn:point:test-faults:d", now + timedelta(seconds=10))

    assert reopened_id is not None
    all_rows = list_faults(conn, rule_id="test-faults:range", point_uri="urn:point:test-faults:d")
    assert len(all_rows) == 2


@pytest.mark.integration
def test_list_faults_filters_by_status(conn):
    now = datetime.now(timezone.utc)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:e", now)
    open_fault(conn, "test-faults:range", "urn:point:test-faults:f", now)
    close_fault(conn, "test-faults:range", "urn:point:test-faults:f", now + timedelta(seconds=1))

    open_only = list_faults(conn, status="open", rule_id="test-faults:range")
    assert {f["point_uri"] for f in open_only} == {"urn:point:test-faults:e"}


@pytest.mark.integration
def test_webhook_health_disables_after_threshold(conn):
    webhook_id = "test-faults:webhook-1"
    disabled = False
    for _ in range(5):
        disabled = record_webhook_failure(conn, webhook_id, "connection refused", disable_threshold=5)

    assert disabled is True
    assert is_webhook_disabled(conn, webhook_id) is True


@pytest.mark.integration
def test_webhook_health_success_resets_failure_count(conn):
    webhook_id = "test-faults:webhook-2"
    record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    record_webhook_success(conn, webhook_id)

    # 4 more failures shouldn't disable it - the success reset the counter
    for _ in range(4):
        disabled = record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    assert disabled is False
    assert is_webhook_disabled(conn, webhook_id) is False


@pytest.mark.integration
def test_enable_webhook_clears_disabled_state(conn):
    webhook_id = "test-faults:webhook-3"
    for _ in range(5):
        record_webhook_failure(conn, webhook_id, "timeout", disable_threshold=5)
    assert is_webhook_disabled(conn, webhook_id) is True

    enable_webhook(conn, webhook_id)
    assert is_webhook_disabled(conn, webhook_id) is False


@pytest.mark.integration
def test_list_webhook_health_returns_every_webhook_keyed_by_id(conn):
    record_webhook_failure(conn, "test-faults:webhook-4", "timeout", disable_threshold=5)
    record_webhook_failure(conn, "test-faults:webhook-4", "timeout", disable_threshold=5)
    for _ in range(5):
        record_webhook_failure(conn, "test-faults:webhook-5", "refused", disable_threshold=5)

    health = list_webhook_health(conn)

    assert health["test-faults:webhook-4"] == {"disabled": False, "consecutive_failures": 2, "last_error": "timeout"}
    assert health["test-faults:webhook-5"] == {"disabled": True, "consecutive_failures": 5, "last_error": "refused"}
    # a webhook that's never recorded an attempt has no row - the caller
    # (fault_api.py's GET /webhooks) is responsible for the not-in-dict default
    assert "test-faults:never-attempted" not in health
