"""
Phase 2 + 4 + 6: the evidence ladder, the confidence levels, the named
alternatives, the deviations, and how corrections and captures constrain
the next pass.
"""

from cx_fixtures import AHU_OBJECTS, NOW, SPEC_MATERIAL, VAV_OBJECTS, device, devices

from timberdoodle.commissioning import alignment
from timberdoodle.commissioning.spec_model import build_spec_model


def _align(devs=None, phase="warranty", corrections=(), captures=(), cidr=("10.0.0.0/24",), existing=None):
    spec = build_spec_model(SPEC_MATERIAL)
    devs = devices() if devs is None else devs
    constraints = alignment.build_constraints(list(corrections), list(captures), devs)
    return alignment.align(spec, devs, phase, constraints, existing, list(cidr))


def _mapping(res, tag):
    return next(m for m in res.mappings if m["spec_tag"] == tag)


def _entity(res, tag):
    return next(e for e in res.entities if e["spec_tag"] == tag)


def test_tag_match_with_delimiter_variation_is_high_and_auto_accepted():
    res = _align()
    m = _mapping(res, "AHU-1")
    assert m["field_id"] == "bacnet:1001@10.0.0.11"
    assert m["confidence"] == "high" and m["acceptance"] == "auto-accepted"
    tag_ev = next(b for b in m["basis"] if b["kind"] == "tag")
    assert tag_ev["score"] == 1.0 and "AHU_1" in tag_ev["detail"]
    sig = next(b for b in m["basis"] if b["kind"] == "signature")
    assert sig["score"] >= 0.8


def test_known_abbreviation_with_same_number_is_high():
    res = _align()
    m = _mapping(res, "VAV-1-01")
    assert m["field_id"] == "bacnet:2001@10.0.0.21"
    assert m["confidence"] == "high"
    assert "abbreviation" in next(b for b in m["basis"] if b["kind"] == "tag")["detail"]


def test_conflicting_number_in_the_field_name_is_low_with_a_named_alternative():
    res = _align()
    m = _mapping(res, "VAV-1-02")
    assert m["field_id"] == "bacnet:2003@10.0.0.23"  # only unclaimed VAV, so it is proposed...
    assert m["confidence"] == "low"                  # ...but the name says number 13, not 12
    assert m["acceptance"] is None
    assert any("not VAV-1-02 at all" in a["interpretation"] for a in m["alternatives"])
    assert any("number 13" in b["detail"] for b in m["basis"] if b["kind"] == "tag")


def test_spec_device_not_found_is_interpreted_per_phase_never_bare():
    for phase, word in (("new_construction", "not yet installed"), ("warranty", "contractor is still responsible"), ("operations", "expected to be running"), ("retrofit", "documentation may be stale")):
        res = _align(phase=phase)
        d = next(d for d in res.deviations if d["kind"] == "spec_device_not_found" and d["spec_tag"] == "CH-1")
        assert word in d["interpretation"].lower(), (phase, d["interpretation"])
        assert "10.0.0.0/24" in d["interpretation"]  # the scanned scope is named, absence outside it is not nonexistence
        assert d["to_settle"]
        assert d["severity"] == ("info" if phase == "new_construction" else "warning")
    res = _align(phase="new_construction", cidr=())
    d = next(d for d in res.deviations if d["spec_tag"] == "CH-1")
    assert "never scanned" in d["interpretation"]


def test_not_networked_equipment_is_only_verifiable_by_nameplate():
    res = _align()
    ef = _entity(res, "EF-1")
    assert ef["liveness"]["state"] == "not_networked"
    assert ef["confidence"] == "low"
    cap = {"id": "cap1", "captured_at": NOW.isoformat(), "location": {"floor": "roof"}, "legible": {"nameplate": {"tag": "EF-1", "model": "G-120"}}, "partial": {}, "unreadable": [], "status": "ingested"}
    res2 = _align(captures=[cap])
    ef2 = _entity(res2, "EF-1")
    assert ef2["confidence"] == "confirmed"
    assert ef2["confidence_basis"][0]["kind"] == "field_capture"


def test_extra_field_point_is_an_info_point_mismatch_not_a_dropped_point():
    res = _align()
    ahu = _entity(res, "AHU-1")
    extra = [p for p in ahu["points"] if p["status"] == "unspecified_present"]
    assert [p["name"] for p in extra] == ["RA-T"]
    d = next(d for d in res.deviations if d["kind"] == "point_mismatch" and d["spec_tag"] == "AHU-1")
    assert d["severity"] == "info" and "return air temperature" in d["field_shows"]


def test_missing_specified_point_is_kept_as_specified_absent():
    devs = devices()
    devs[0]["objects"] = [o for o in devs[0]["objects"] if o["name"] != "DSP"]
    res = _align(devs)
    ahu = _entity(res, "AHU-1")
    absent = [p for p in ahu["points"] if p["status"] == "specified_absent"]
    assert [p["spec_name"] for p in absent] == ["Duct Static Pressure"]
    assert absent[0]["point_uri"] is None  # never invented
    d = next(d for d in res.deviations if d["kind"] == "point_mismatch")
    assert "missing Duct Static Pressure" in d["field_shows"]


def test_type_mismatch_when_points_say_heat_pump_under_an_ahu_tag():
    hp_objects = [("SA-T", "analogInput,1", "degF", 55.0), ("SF-C", "binaryOutput,1", None, 1), ("SF-S", "binaryInput,1", None, 1),
                  ("COMP-C", "binaryOutput,2", None, 0), ("RV", "binaryOutput,3", None, 0), ("ZN-T", "analogInput,2", "degF", 71.0)]
    devs = [device("AHU_1", hp_objects, 1001, "10.0.0.11")]
    res = _align(devs)
    m = _mapping(res, "AHU-1")
    assert m["confidence"] in ("medium", "low")
    assert m["contradictions"]
    d = next(d for d in res.deviations if d["kind"] == "type_mismatch")
    assert "replacement" in d["interpretation"]
    assert any("heat pump" in a["interpretation"] for a in m["alternatives"])


def test_duplicate_claim_when_two_devices_carry_the_same_tag():
    devs = devices() + [device("AHU-1_spare", AHU_OBJECTS[:5], 1099, "10.0.0.99")]
    res = _align(devs)
    d = next((d for d in res.deviations if d["kind"] == "duplicate_claim"), None)
    assert d is not None and d["spec_tag"] == "AHU-1"
    assert "address" in d["to_settle"]
    m = _mapping(res, "AHU-1")
    assert m["confidence"] != "high"  # a contested tag is never auto-accepted


def test_field_device_not_in_spec_gets_a_proposed_type_and_a_phase_aware_reading():
    devs = devices() + [device("V1_7", VAV_OBJECTS, 2007, "10.0.0.27")]
    res = _align(devs, phase="retrofit")
    ds = [d for d in res.deviations if d["kind"] == "field_device_not_in_spec"]
    assert len(ds) == 1 and ds[0]["field_id"] == "bacnet:2007@10.0.0.27"
    assert "variable air volume box" in ds[0]["interpretation"]
    assert "retrofit" in ds[0]["interpretation"]
    ent = next(e for e in res.entities if e["spec_tag"] is None)
    assert ent["canonical_type"] == "variable air volume box" and ent["confidence"] == "medium"


def test_confirm_correction_fixes_the_mapping_and_teaches_an_alias():
    corr = {"id": "c1", "kind": "confirm_mapping", "spec_tag": "VAV-1-02", "field_id": "bacnet:2003@10.0.0.23", "by": "t", "at": NOW.isoformat()}
    res = _align(corrections=[corr])
    m = _mapping(res, "VAV-1-02")
    assert m["confidence"] == "confirmed" and m["acceptance"] == "confirmed"
    assert m["basis"][0]["kind"] == "correction"
    # numbers disagree (02 vs 3) so nothing is learned about letters from this one...
    assert alignment.build_constraints([corr], [], devices()).aliases == {}
    # ...but confirming VAV-1-01 <-> V1_1 (same number) teaches V = VAV
    corr2 = {**corr, "spec_tag": "VAV-1-01", "field_id": "bacnet:2001@10.0.0.21"}
    assert alignment.build_constraints([corr2], [], devices()).aliases.get("V") == "VAV"


def test_reject_correction_frees_the_device_and_the_spec_row_becomes_not_found():
    corr = {"id": "c2", "kind": "reject_mapping", "spec_tag": "VAV-1-02", "field_id": "bacnet:2003@10.0.0.23", "by": "t", "at": NOW.isoformat()}
    res = _align(corrections=[corr])
    assert _mapping(res, "VAV-1-02")["confidence"] == "unmatched"
    assert any(d["kind"] == "spec_device_not_found" and d["spec_tag"] == "VAV-1-02" for d in res.deviations)
    assert any(d["kind"] == "field_device_not_in_spec" and d["field_id"] == "bacnet:2003@10.0.0.23" for d in res.deviations)


def test_capture_showing_address_and_tag_together_lifts_confidence():
    cap = {"id": "cap2", "captured_at": NOW.isoformat(), "location": {"floor": "1", "room": "102"}, "legible": {"tag": "VAV-1-02", "address": "10.0.0.23"}, "partial": {}, "unreadable": [], "status": "ingested"}
    res = _align(captures=[cap])
    m = _mapping(res, "VAV-1-02")
    # a label photo showing the address next to the tag is physical evidence - High -
    # but the controller's own name still disagrees and that stays on the record
    assert m["confidence"] == "high"
    assert m["basis"][0]["kind"] == "field_capture"
    assert any("number 13" in b["detail"] for b in m["basis"] if b["kind"] == "confidence_note")
    ent = _entity(res, "VAV-1-02")
    assert ent["relationships"]["is_located_in"]["room"] == "102"


def test_existing_entities_keep_their_ids_across_passes():
    first = _align()
    second = _align(existing=first.entities)
    assert {e["spec_tag"]: e["id"] for e in first.entities} == {e["spec_tag"]: e["id"] for e in second.entities}


def test_gateway_device_fronting_several_units_is_split_by_object_name_tags():
    objs = [(f"AHU1_{n}", o, u, v) for n, o, u, v in AHU_OBJECTS] + [(f"V1_1_{n}", o.replace(",", ",1"), u, v) for n, o, u, v in VAV_OBJECTS]
    gw = device("JACE-1", objs, 5000, "10.0.0.5", vendor=36)
    res = _align([gw])
    assert any("fronts several" in a for a in res.assumptions)
    assert _mapping(res, "AHU-1")["field_id"].startswith("bacnet:5000@10.0.0.5")
    assert _mapping(res, "VAV-1-01")["field_id"].startswith("bacnet:5000@10.0.0.5")
    assert _mapping(res, "AHU-1")["field_id"] != _mapping(res, "VAV-1-01")["field_id"]


def test_zone_count_disagreement_is_volunteered_as_an_assumption():
    material = {"equipment": [
        {"tag": "AHU-1", "type": "AHU", "serves": ["Z1", "Z2", "Z3"], "points": [{"name": "SAT"}]},
        {"tag": "VAV-1", "type": "VAV", "fed_by": ["AHU-1"], "points": [{"name": "ZNT"}]},
    ]}
    res = alignment.align(build_spec_model(material), devices()[:1], "operations")
    assert any("counts don't agree" in a for a in res.assumptions)
