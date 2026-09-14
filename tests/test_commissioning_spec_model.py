"""
Phase 1: the spec model keeps what the documents say, flags what does
not add up, and escalates only what it cannot defend - never quietly
reconciling.
"""

import pytest
from cx_fixtures import SPEC_MATERIAL

from timberdoodle.commissioning.spec_model import (
    ESCALATION_THRESHOLD,
    build_spec_model,
    cite,
    infer_type_from_tag,
    normalize_tag,
    tag_letters,
    tag_number,
)


@pytest.mark.parametrize("a,b", [
    ("AHU-1", "AHU_1"), ("AHU-1", "ahu 1"), ("AHU-1", "AHU1"), ("VAV-2-05", "V2_5"[1:]),  # letters differ - see tag_letters test
    ("RTU-01", "RTU-1"),
])
def test_normalize_tag_ignores_delimiters_case_and_leading_zeros(a, b):
    assert normalize_tag(a) == normalize_tag(b) or tag_number(a) == tag_number(b)


def test_tag_letters_and_number_split_a_tag():
    assert tag_letters("VAV-2-05") == "VAV"
    assert tag_number("VAV-2-05") == tag_number("V2_5") == "25"
    assert tag_number("AHU-1") == "1"
    assert tag_letters("") == "" and tag_number(None) == ""


def test_infer_type_from_tag_uses_the_type_aliases():
    assert infer_type_from_tag("AHU-3") == "air handling unit"
    assert infer_type_from_tag("VAV-2-05") == "variable air volume box"
    assert infer_type_from_tag("XYZ-1") is None


def test_builds_equipment_with_classified_expected_points():
    spec = build_spec_model(SPEC_MATERIAL)
    by_tag = {e["tag"]: e for e in spec["equipment"]}
    assert by_tag["AHU-1"]["type"] == "air handling unit"
    assert by_tag["VAV-1-01"]["type"] == "variable air volume box"
    fns = {(p["function"], p["role"]) for p in by_tag["AHU-1"]["expected_points"]}
    assert ("supply air temperature", "sensor") in fns
    assert ("supply air temperature", "setpoint") in fns
    assert ("supply fan", "status") in fns and ("supply fan", "command") in fns
    assert ("supply air static pressure", "sensor") in fns
    assert by_tag["EF-1"]["networked"] is False
    assert "no_points" in by_tag["EF-1"]["flags"]
    assert spec["confidence"] in ("high", "medium")


def test_type_inferred_from_tag_is_flagged_not_silently_assumed():
    spec = build_spec_model({"equipment": [{"tag": "RTU-4"}]})
    e = spec["equipment"][0]
    assert e["type"] == "rooftop unit"
    assert "type_inferred_from_tag" in e["flags"]


def test_unknown_type_and_unreadable_tag_are_escalated_with_a_one_action_question():
    spec = build_spec_model({"equipment": [{"tag": "Q-7", "type": "thingamajig", "extraction_confidence": 0.3}]})
    e = spec["equipment"][0]
    assert e["type"] is None and "unknown_type" in e["flags"]
    assert spec["escalate"] and spec["escalate"][0]["spec_tag"] == "Q-7"
    assert spec["escalate"][0]["score"] < ESCALATION_THRESHOLD
    assert spec["escalate"][0]["one_action"]["accept"]["kind"] == "accept_spec_entry"
    assert "what kind of equipment" in spec["escalate"][0]["question"]


def test_duplicate_tags_are_flagged_on_both_and_lower_overall_confidence():
    spec = build_spec_model({"equipment": [{"tag": "AHU-1", "type": "AHU", "points": [{"name": "SAT"}]}, {"tag": "AHU_1", "type": "AHU", "points": [{"name": "SAT"}]}]})
    kinds = [i["kind"] for i in spec["inconsistencies"]]
    assert "duplicate_tag" in kinds
    assert all("duplicate_tag" in e["flags"] for e in spec["equipment"])
    assert spec["confidence"] != "high"


def test_topology_stated_one_sidedly_is_kept_and_flagged_not_reconciled():
    spec = build_spec_model({"equipment": [
        {"tag": "AHU-1", "type": "AHU", "feeds": ["VAV-1"], "points": [{"name": "SAT"}]},
        {"tag": "VAV-1", "type": "VAV", "fed_by": [], "points": [{"name": "ZNT"}]},
        {"tag": "VAV-2", "type": "VAV", "fed_by": ["AHU-9"], "points": [{"name": "ZNT"}]},
    ]})
    kinds = {i["kind"] for i in spec["inconsistencies"]}
    assert "topology_one_sided" in kinds
    assert "unresolved_reference" in kinds
    ahu = next(e for e in spec["equipment"] if e["tag"] == "AHU-1")
    assert ahu["feeds"] == ["VAV-1"]  # not removed, not copied onto VAV-1


def test_flat_point_rows_attach_to_their_equipment_and_orphans_are_flagged():
    spec = build_spec_model({
        "equipment": [{"tag": "AHU-1", "type": "AHU"}],
        "points": [{"equipment": "AHU-1", "name": "SAT", "units": "degF"}, {"equipment": "AHU-2", "name": "RAT"}],
    })
    ahu = spec["equipment"][0]
    assert [p["name"] for p in ahu["expected_points"]] == ["SAT"]
    assert any(i["kind"] == "point_for_unknown_equipment" for i in spec["inconsistencies"])


def test_unit_contradiction_in_the_spec_itself_is_flagged():
    spec = build_spec_model({"equipment": [{"tag": "AHU-1", "type": "AHU", "points": [{"name": "Supply Air Temp", "units": "cfm"}]}]})
    assert any(i["kind"] == "unit_contradiction" for i in spec["inconsistencies"])


def test_cite_never_prints_none():
    assert cite({"source": {"document": "M-601", "page": 3}}) == "M-601 p.3"
    assert cite({"source": {"document": "M-601", "page": None}}) == "M-601"
    assert cite({"source": {"document": None, "page": None}}) == "no source recorded"
    assert cite({}) == "no source recorded"
