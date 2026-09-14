"""
Phase 5 + 8: change classification across passes (nothing dropped on the
first miss, a returned device is re-verified by signature), faults and
accepted risks kept strictly apart, and freshness stated honestly.
"""

from datetime import timedelta

from cx_fixtures import (
    AHU_OBJECTS,
    NOW,
    SPEC_MATERIAL,
    VAV_OBJECTS,
    device,
    devices,
    history,
)

from timberdoodle.commissioning import alignment, reconcile
from timberdoodle.commissioning.history import InMemoryHistory
from timberdoodle.commissioning.spec_model import build_spec_model

SPEC = build_spec_model(SPEC_MATERIAL)


def _align(devs, prior=None):
    return alignment.align(SPEC, devs, "operations", None, prior, []).entities


def _by_tag(entities):
    return {e.get("spec_tag") or e["field_identity"]["field_id"]: e for e in entities}


def _confirmed(entities):
    """Rung-1 faults only fire on well-identified entities; the fixture's
    tag evidence lands AHU-1/VAV-1-01 at high already, this pins it."""
    for e in entities:
        if e.get("field_identity"):
            e["confidence"] = "confirmed"
    return entities


def test_first_pass_is_all_new_and_never_seen_is_not_absent():
    hist = history(devices())
    rep = reconcile.reconcile([], _align(devices()), hist, [], now=NOW)
    by = _by_tag(rep.entities)
    assert {c["spec_tag"] for c in rep.changes["new"]} >= {"AHU-1", "VAV-1-01"}
    assert by["CH-1"]["liveness"]["state"] == "never_seen"  # spec-only: never observed, not "absent"
    assert by["EF-1"]["liveness"]["state"] == "not_networked"
    assert rep.changes["absent"] == [] and rep.changes["missing"] == []
    assert by["AHU-1"]["liveness"]["signature_hash"]


def test_unchanged_when_nothing_moves():
    hist = history(devices())
    first = reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities
    second = reconcile.reconcile(first, _align(devices(), first), hist, [], now=NOW + timedelta(minutes=30))
    assert {c["spec_tag"] for c in second.changes["unchanged"]} >= {"AHU-1", "VAV-1-01"}
    assert not second.changes["new"] and not second.changes["drifted"]


def test_missing_is_held_for_a_grace_period_before_it_becomes_absent():
    hist = history(devices())  # last samples at NOW
    prior = reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities
    # 8 h later nothing new has arrived from anyone
    t1 = NOW + timedelta(hours=8)
    r1 = reconcile.reconcile(prior, _align(devices(), prior), hist, [], now=t1)
    ahu = _by_tag(r1.entities)["AHU-1"]
    assert ahu["liveness"]["state"] == "missing"
    assert ahu["liveness"]["misses"] == 1
    assert "reboot" in ahu["reconcile_detail"]["detail"]  # says why it is not yet absent
    assert ahu["liveness"]["last_observed"]  # remembered, not dropped
    # second miss, past the wall-clock grace -> absent
    t2 = NOW + timedelta(hours=16)
    r2 = reconcile.reconcile(r1.entities, _align(devices(), r1.entities), hist, [], now=t2)
    ahu2 = _by_tag(r2.entities)["AHU-1"]
    assert ahu2["liveness"]["state"] == "absent"
    assert ahu2["liveness"]["absent_since"] == t1.isoformat()
    assert "phase" in ahu2["reconcile_detail"]["detail"]  # absence is read per phase, and says so
    # and it stays in the entity list
    assert any(e.get("spec_tag") == "AHU-1" for e in r2.entities)


def test_returned_with_same_signature_vs_new_hardware_at_old_address():
    hist = history(devices())
    prior = reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities
    for e in prior:
        if e.get("spec_tag") == "VAV-1-01":
            e["liveness"].update({"state": "absent", "absent_since": (NOW - timedelta(days=2)).isoformat(), "misses": 3})
    # same device comes back
    same = reconcile.reconcile(prior, _align(devices(), prior), hist, [], now=NOW)
    v = _by_tag(same.entities)["VAV-1-01"]
    assert v["reconcile_state"] == "returned"
    assert "same signature" in v["reconcile_detail"]["detail"]
    # a different object list at the same address comes back
    swapped = devices()
    swapped[1] = device("V1_1", [*VAV_OBJECTS[:2], ("RH-VLV", "analogOutput,2", "%", 10.0)], 2001, "10.0.0.21")
    hist2 = history(swapped)
    ret = reconcile.reconcile(prior, _align(swapped, prior), hist2, [], now=NOW)
    v2 = _by_tag(ret.entities)["VAV-1-01"]
    assert v2["reconcile_state"] == "returned"
    d = v2["reconcile_detail"]
    assert "DIFFERENT object list" in d["detail"]
    assert d["signature"]["added"] and d["signature"]["removed"]
    assert "nameplate" in d["verify"]


def test_drifted_reports_the_object_list_diff_in_full():
    hist = history(devices())
    prior = reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities
    changed = devices()
    # RA-T removed, MA-T added - a re-programmed controller
    changed[0] = device("AHU_1", [*AHU_OBJECTS[:5], ("MA-T", "analogInput,4", "degF", 60.0)], 1001, "10.0.0.11")
    rep = reconcile.reconcile(prior, _align(changed, prior), history(changed, flat=()), [], now=NOW + timedelta(hours=1))
    ahu = _by_tag(rep.entities)["AHU-1"]
    assert ahu["reconcile_state"] == "drifted"
    sig = ahu["reconcile_detail"]["signature"]
    assert any("MA-T" in a for a in sig["added"]) and any("RA-T" in r for r in sig["removed"])
    assert "review before trusting" in ahu["reconcile_detail"]["detail"]


def test_re_addressed_same_signature_new_address():
    hist = history(devices())
    prior = reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities
    moved = devices()
    moved[1] = device("V1_1", VAV_OBJECTS, 2101, "10.0.0.121")
    rep = reconcile.reconcile(prior, _align(moved, prior), history(moved), [], now=NOW + timedelta(hours=1))
    v = _by_tag(rep.entities)["VAV-1-01"]
    assert v["reconcile_state"] == "re_addressed", v["reconcile_detail"]
    assert v["reconcile_detail"]["previous_field_id"] == "bacnet:2001@10.0.0.21"
    assert v["field_identity"]["field_id"] == "bacnet:2101@10.0.0.121"


def test_spec_entity_whose_device_disappears_remembers_the_device_it_had():
    hist = history(devices())
    prior = reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities
    # V1_3 (bound to VAV-1-02 at low confidence) drops off the network
    fewer = devices()[:2]
    rep = reconcile.reconcile(prior, _align(fewer, prior), history(fewer), [], now=NOW + timedelta(hours=1))
    v2 = _by_tag(rep.entities)["VAV-1-02"]
    assert v2["field_identity"] is None
    assert v2["liveness"]["state"] == "missing"
    assert v2["liveness"]["last_field_id"] == "bacnet:2003@10.0.0.23"  # not forgotten on the first miss
    assert v2["reconcile_detail"]["detail"].startswith("not observed this pass (miss 1 of 2")


def test_unlisted_device_that_vanishes_is_carried_forward_not_deleted():
    extra = [*devices(), device("PNL_9", [("KW", "analogInput,1", "kW", 12.0), ("KWH", "analogInput,2", "kWh", 100.0)], 3009, "10.0.0.39")]
    hist = history(extra)
    prior = reconcile.reconcile([], _align(extra), hist, [], now=NOW).entities
    assert any((e.get("field_identity") or {}).get("field_id") == "bacnet:3009@10.0.0.39" for e in prior)
    rep = reconcile.reconcile(prior, _align(devices(), prior), history(devices()), [], now=NOW + timedelta(hours=1))
    gone = [e for e in rep.entities if (e.get("field_identity") or {}).get("field_id") == "bacnet:3009@10.0.0.39"]
    assert gone, "the vanished device must still be in the entity list"
    assert gone[0]["reconcile_state"] == "missing"
    assert "carried forward" in gone[0]["reconcile_detail"]["detail"]
    rep2 = reconcile.reconcile(rep.entities, _align(devices(), rep.entities), history(devices()), [], now=NOW + timedelta(hours=2))
    gone2 = [e for e in rep2.entities if (e.get("field_identity") or {}).get("field_id") == "bacnet:3009@10.0.0.39"]
    assert gone2 and gone2[0]["reconcile_state"] == "absent"


def test_sustained_absence_is_a_fault_only_on_a_well_identified_entity_and_only_after_the_threshold():
    hist = history(devices())
    ents = _confirmed(reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities)
    for e in ents:
        if e.get("spec_tag") == "AHU-1":
            e["liveness"].update({"state": "absent", "absent_since": (NOW - timedelta(hours=10)).isoformat(), "misses": 3})
    faults, _ = reconcile.classify_faults(ents, [], NOW, reconcile.DEFAULTS)
    assert not [f for f in faults if f["kind"] == "sustained_absence"]  # 10 h < 48 h
    faults, _ = reconcile.classify_faults(ents, [], NOW + timedelta(hours=40), reconcile.DEFAULTS)
    absent = [f for f in faults if f["kind"] == "sustained_absence"]
    assert len(absent) == 1 and absent[0]["spec_tag"] == "AHU-1"
    assert absent[0]["rule_id"] == reconcile.FAULT_RULE_ABSENCE
    assert absent[0]["candidates"] and all(c["would_distinguish"] for c in absent[0]["candidates"])
    # the same symptom on a low-confidence entity is not a fault - identity first
    for e in ents:
        if e.get("spec_tag") == "AHU-1":
            e["confidence"] = "low"
    faults, _ = reconcile.classify_faults(ents, [], NOW + timedelta(hours=40), reconcile.DEFAULTS)
    assert not faults


def test_ladder_failure_becomes_a_fault_and_an_accepted_risk_moves_it_out_of_the_fault_list():
    hist = history(devices(), flat=("RA-T",))
    ents = _confirmed(reconcile.reconcile([], _align(devices()), hist, [], now=NOW).entities)
    ahu = _by_tag(ents)["AHU-1"]
    ahu["ladder"] = {"highest_rung_passed": 1, "rungs": [{"rung": 1, "result": "pass"}, {"rung": 2, "name": "liveness", "result": "fail", "symptom": "1 sensor(s) reading a constant value: return air temperature", "candidates": [{"cause": "x", "would_distinguish": "y"}], "checked_at": NOW.isoformat()}]}
    faults, covered = reconcile.classify_faults(ents, [], NOW, reconcile.DEFAULTS)
    assert [f["kind"] for f in faults] == ["rung2_failure"]
    assert faults[0]["rule_id"] == reconcile.FAULT_RULE_LADDER and faults[0]["severity"] == "warning"
    assert covered == []

    risk = reconcile.new_risk(None, None, "ahu-1", "RA-T sensor known bad, replacement on order", "owner", now=NOW)
    assert risk["review_by"]  # a risk always has a review date
    faults2, covered2 = reconcile.classify_faults(ents, [risk], NOW, reconcile.DEFAULTS)
    assert faults2 == []  # never in both lists
    assert len(covered2) == 1
    assert covered2[0]["risk_id"] == risk["id"] and covered2[0]["accepted_by"] == "owner"
    assert covered2[0]["kind"] == "rung2_failure"
    assert "suppressed because an accepted risk" in covered2[0]["note"]


def test_freshness_statement_counts_every_bucket_and_never_says_all_good_for_nothing():
    hist = history(devices())
    rep = reconcile.reconcile([], _align(devices()), hist, [], now=NOW)
    f = rep.freshness
    assert f["counts"]["fresh"] == 3 and f["counts"]["never_seen"] == 1 and f["counts"]["not_networked"] == 1
    assert f["networked_entities"] == 4  # EF-1 is excluded from the denominator
    assert "3 of 4 networked entities fresh" in f["statement"]
    assert "1 never seen" in f["statement"] and "not networked by design" in f["statement"]
    # stale: same entities, an hour and a half later with no new samples
    later = reconcile.reconcile(rep.entities, _align(devices(), rep.entities), hist, [], now=NOW + timedelta(minutes=90))
    assert later.freshness["counts"]["stale"] == 3 and later.freshness["counts"]["fresh"] == 0
    empty = reconcile.freshness([], NOW, reconcile.DEFAULTS)
    assert "empty one" in empty["statement"]


def test_last_observation_prefers_history_over_the_graphs_memory():
    ents = _align(devices())
    v = _by_tag(ents)["VAV-1-01"]
    v["provenance"]["last_observed"] = (NOW - timedelta(days=5)).isoformat()
    hist = InMemoryHistory()
    hist.add(v["points"][0]["point_uri"], NOW - timedelta(minutes=3), 71.0)
    obs = reconcile.last_observation(v, hist)
    assert obs == NOW - timedelta(minutes=3)
    assert reconcile.last_observation(v, None) == NOW - timedelta(days=5)
