"""
Phase 3: the four rungs climb in order, a failure falls to the rung
below when re-checked, and every failure names the symptom with
candidate causes - never a diagnosis.
"""

from datetime import timedelta

from cx_fixtures import NOW, SPEC_MATERIAL, devices, history

from timberdoodle.commissioning import alignment, ladder
from timberdoodle.commissioning.history import InMemoryHistory
from timberdoodle.commissioning.spec_model import build_spec_model


def _entities(devs=None):
    res = alignment.align(build_spec_model(SPEC_MATERIAL), devs or devices(), "operations", None, None, [])
    return {e["spec_tag"]: e for e in res.entities}


def _uri(entity, function, role):
    return next(p["point_uri"] for p in entity["points"] if p["function"] == function and p["role"] == role)


def test_all_rungs_pass_for_a_healthy_vav_with_tracking_and_a_setpoint_step():
    ents = _entities()
    vav = ents["VAV-1-01"]
    hist = InMemoryHistory()
    zt, zsp = _uri(vav, "zone air temperature", "sensor"), _uri(vav, "zone air temperature", "setpoint")
    for i in range(24):
        t = NOW - timedelta(hours=23 - i)
        hist.add(zsp, t, 72.0 if i < 22 else 70.0)             # setpoint stepped down 2 h ago
        hist.add(zt, t, 72.0 + (i % 2) * 0.3 if i <= 22 else 71.0)  # zone moved toward it an hour later
        hist.add(_uri(vav, "damper position", "command"), t, 40.0 + i)
        hist.add(_uri(vav, "flow", "sensor"), t, 350.0 + i)
    out = ladder.climb(vav, hist, now=NOW)
    assert [r["result"] for r in out["rungs"]] == ["pass", "pass", "pass", "pass"]
    assert out["highest_rung_passed"] == 4
    assert "passive" in " ".join(out["rungs"][3]["evidence"]).lower()


def test_rung1_with_no_samples_at_all_is_insufficient_data_and_nothing_above_is_attempted():
    # the device is in the graph (the connector saw it once) but nothing has
    # ever reported - that is "I don't know", not "it isn't there"
    vav = _entities()["VAV-1-01"]
    out = ladder.climb(vav, InMemoryHistory(), now=NOW)
    assert out["rungs"][0]["result"] == "insufficient_data"
    assert "no point history" in out["rungs"][0]["evidence"][0]
    assert out["highest_rung_passed"] == 0
    assert [r["result"] for r in out["rungs"][1:]] == ["not_attempted"] * 3


def test_rung1_fails_when_a_device_that_used_to_report_has_gone_quiet():
    vav = _entities()["VAV-1-01"]
    hist = InMemoryHistory()
    zt = _uri(vav, "zone air temperature", "sensor")
    for i in range(6):
        hist.add(zt, NOW - timedelta(days=3, hours=i), 72.0 + i * 0.1)
    out = ladder.climb(vav, hist, now=NOW)
    r1 = out["rungs"][0]
    assert r1["result"] == "fail"
    assert "nothing has reported since" in r1["evidence"][0]
    assert r1["candidates"] and all(c["would_distinguish"] for c in r1["candidates"])  # never a bare failure
    assert out["highest_rung_passed"] == 0
    assert [r["result"] for r in out["rungs"][1:]] == ["not_attempted"] * 3


def test_flat_sensor_fails_liveness_with_symptom_and_candidates():
    ahu = _entities()["AHU-1"]
    hist = history(devices(), flat=("RA-T",))
    out = ladder.climb(ahu, hist, now=NOW)
    r2 = out["rungs"][1]
    assert r2["result"] == "fail"
    assert "return air temperature" in r2["symptom"] and "constant" in r2["symptom"]
    causes = " ".join(c["cause"] for c in r2["candidates"]).lower()
    assert "sensor" in causes and "overrid" in causes
    assert all(c.get("would_distinguish") for c in r2["candidates"])
    assert out["highest_rung_passed"] == 1
    assert out["rungs"][2]["result"] == "not_attempted"


def test_rung3_failure_that_is_really_stale_data_falls_to_rung2():
    ahu = _entities()["AHU-1"]
    hist = InMemoryHistory()
    sat, sp = _uri(ahu, "supply air temperature", "sensor"), _uri(ahu, "supply air temperature", "setpoint")
    # lively for the first 12 hours, then the sensor freezes at a 10 degF error
    for i in range(24):
        t = NOW - timedelta(hours=23 - i)
        live = i < 12
        hist.add(sp, t, 55.0)
        hist.add(sat, t, 55.0 + (i % 3) * 0.4 if live else 65.0)
        hist.add(_uri(ahu, "supply fan", "status"), t, 1)
        hist.add(_uri(ahu, "supply fan", "command"), t, 1)
        hist.add(_uri(ahu, "supply air static pressure", "sensor"), t, 1.2 + (i % 2) * 0.05 if live else 1.2)
        hist.add(_uri(ahu, "return air temperature", "sensor"), t, 72.0 + (i % 2) * 0.3 if live else 72.0)
    out = ladder.climb(ahu, hist, now=NOW, config={"flat_epsilon": 0.01, "response_window_minutes": 60})
    r3 = out["rungs"][2]
    assert r3["result"] == "fail"
    # the response check failed, but the sensor also stopped moving in the
    # response window - that is a liveness problem masquerading as control
    assert r3["fell_to_rung"] == 2, r3
    assert out["highest_rung_passed"] == 1


def test_command_status_disagreement_is_a_responsiveness_failure():
    ahu = _entities()["AHU-1"]
    hist = history(devices(), flat=())
    st, cmd = _uri(ahu, "supply fan", "status"), _uri(ahu, "supply fan", "command")
    hist.series.pop(st, None)
    hist.series.pop(cmd, None)
    for i in range(12):
        t = NOW - timedelta(hours=11 - i)
        hist.add(cmd, t, 1)
        hist.add(st, t, 0)  # commanded on, never proves
    out = ladder.climb(ahu, hist, now=NOW)
    r3 = out["rungs"][2]
    assert r3["result"] == "fail"
    assert "supply fan" in r3["symptom"] and "command" in r3["symptom"].lower()
    assert any("belt" in c["cause"].lower() or "starter" in c["cause"].lower() or "overload" in c["cause"].lower() for c in r3["candidates"])


def test_rung4_is_insufficient_data_without_a_setpoint_change_and_a_plan_is_offered_not_executed():
    vav = _entities()["VAV-1-01"]
    hist = history(devices(), flat=())
    out = ladder.climb(vav, hist, now=NOW)
    assert out["rungs"][3]["result"] == "insufficient_data"
    plans = ladder.plan_command_tests(vav, hist, now=NOW)
    assert plans and plans[0]["write_point"]["function"] == "zone air temperature"
    assert plans[0]["revert_value"] == plans[0]["before_state"]["setpoint"]
    assert abs(plans[0]["write_value"] - plans[0]["revert_value"]) == 2.0
    assert "not executed" not in plans[0]["log"]  # the plan describes the test; nothing here writes to a controller


def test_not_networked_entity_gets_not_applicable_not_a_failure():
    ef = _entities()["EF-1"]
    out = ladder.climb(ef, InMemoryHistory(), now=NOW)
    assert out["rungs"][0]["result"] == "not_applicable"
