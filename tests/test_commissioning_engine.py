"""
The whole pass, end to end, against an in-memory repo: phase inferred and
declared, report sections present, deviation ids stable across passes,
a correction re-scores the building and reports what moved, captures
shrink the punch list, absence matures into a fault, and an accepted risk
moves that fault out of the fault list without deleting it.
"""

from datetime import timedelta

from cx_fixtures import NOW, SPEC_MATERIAL, devices, history

from timberdoodle.commissioning import db, engine, reconcile
from timberdoodle.commissioning.history import InMemoryHistory

REPORT_SECTIONS = ("spec_model", "mappings", "ladder_results", "deviations", "faults", "accepted_risks", "field_list", "unresolved_questions", "changes", "freshness", "assumptions", "summary")


def _project(phase=None, cidr=("10.0.0.0/24",)):
    repo = db.MemoryRepo()
    proj = engine.new_project("Test Bldg", phase=phase, cidr_scopes=list(cidr))
    repo.put_project(proj)
    repo.upsert("cx_spec", proj["id"], {"id": "spec", "material": SPEC_MATERIAL, "model": None})
    for d in devices():
        repo.upsert("cx_devices", proj["id"], d)
    return repo, proj["id"]


def _by_tag(report):
    return {e.get("spec_tag") or e["field_identity"]["field_id"]: e for e in report["entities"]}


def test_first_pass_infers_the_phase_says_so_and_emits_every_report_section():
    repo, pid = _project()
    hist = history(devices())
    r = engine.run_pass(repo, pid, history=hist, now=NOW, trigger="test")
    for s in REPORT_SECTIONS:
        assert s in r, s
    assert r["pass"]["phase_assumed"] is True
    assert r["pass"]["phase"] == "warranty"  # spec in hand, most units on the network
    assert any(a.startswith("ASSUMED warranty") for a in r["assumptions"])
    assert "no graph store configured" in " ".join(r["pass"]["notes"])
    by = _by_tag(r)
    assert by["AHU-1"]["confidence"] == "high" and by["VAV-1-01"]["confidence"] == "high"
    assert by["VAV-1-02"]["confidence"] == "low" and by["VAV-1-02"]["alternatives"]
    assert by["CH-1"]["confidence"] == "unmatched"
    kinds = {d["kind"] for d in r["deviations"]}
    assert "spec_device_not_found" in kinds
    assert r["summary"]["entities"] == 5
    assert r["freshness"]["statement"].startswith("as of ")
    assert r["fault_sync"]["note"].startswith("no Postgres connection")
    # ladder ran for everything with a field identity
    ahu_ladder = next(item for item in r["ladder_results"] if item["spec_tag"] == "AHU-1")
    assert ahu_ladder["rungs"][1]["result"] == "fail"  # fixture's RA-T is flat
    # questions are one-action, with ready-to-post corrections
    q = next(q for q in r["unresolved_questions"] if q["kind"] == "mapping" and q["spec_tag"] == "VAV-1-02")
    assert q["accept"]["kind"] == "confirm_mapping" and q["reject"]["kind"] == "reject_mapping"
    assert q["alternatives"]
    # spec-only stated phase is honoured verbatim
    repo2, pid2 = _project(phase="retrofit")
    r2 = engine.run_pass(repo2, pid2, history=hist, now=NOW)
    assert r2["pass"]["phase"] == "retrofit" and r2["pass"]["phase_assumed"] is False


def test_deviation_ids_and_open_dates_survive_across_passes_and_close_on_evidence():
    repo, pid = _project()
    hist = history(devices())
    r1 = engine.run_pass(repo, pid, history=hist, now=NOW)
    r2 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(minutes=15))
    ids1 = {(d["kind"], d["spec_tag"], d["field_id"]): d for d in r1["deviations"]}
    ids2 = {(d["kind"], d["spec_tag"], d["field_id"]): d for d in r2["deviations"]}
    assert set(ids1) == set(ids2)
    for k, d in ids1.items():
        assert ids2[k]["id"] == d["id"] and ids2[k]["opened_at"] == d["opened_at"]
    assert r2["deviations_resolved_this_pass"] == []
    # the chiller shows up: its spec_device_not_found closes with evidence, not by hand
    ch = devices()[0] | {"id": "bacnet:3001@10.0.0.31", "field_id": "bacnet:3001@10.0.0.31", "name": "CH-1", "device_instance": 3001, "address": "10.0.0.31", "topic_prefix": "site/CH-1",
                        "objects": [{"name": n, "object_identifier": o, "units": u, "present_value": v, "point_uri": f"urn:point:site/CH-1/{n}"} for n, o, u, v in (("CHWS-T", "analogInput,1", "degF", 44.0), ("CHWR-T", "analogInput,2", "degF", 54.0), ("CH-EN", "binaryOutput,1", None, 1), ("CH-ST", "binaryInput,1", None, 1))]}
    repo.upsert("cx_devices", pid, ch)
    for o in ch["objects"]:
        for i in range(6):
            hist.add(o["point_uri"], NOW + timedelta(minutes=30) - timedelta(hours=5 - i), o["present_value"] + (i % 2) * 0.3 if isinstance(o["present_value"], float) else o["present_value"])
    r3 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(minutes=30))
    closed = [d for d in r3["deviations_resolved_this_pass"] if d["kind"] == "spec_device_not_found" and d["spec_tag"] == "CH-1"]
    assert len(closed) == 1 and closed[0]["resolved_by"] == "evidence"
    assert _by_tag(r3)["CH-1"]["confidence"] in ("confirmed", "high")
    assert any(c["spec_tag"] == "CH-1" for c in r3["changes"]["new"])


def test_correction_rescoring_reports_what_moved_and_propagates():
    repo, pid = _project()
    hist = history(devices())
    engine.run_pass(repo, pid, history=hist, now=NOW)
    r = engine.apply_correction(repo, pid, {"kind": "confirm_mapping", "spec_tag": "VAV-1-02", "field_id": "bacnet:2003@10.0.0.23", "note": "contractor renumbered the boxes"}, by="cx-agent", history=hist, now=NOW + timedelta(minutes=5))
    assert r["correction"]["kind"] == "confirm_mapping" and r["correction"]["by"] == "cx-agent"
    moved = {m["spec_tag"]: m for m in r["confidence_movement"]}
    assert moved["VAV-1-02"]["from"] == "low" and moved["VAV-1-02"]["to"] == "confirmed"
    assert "confirmed by a person" in moved["VAV-1-02"]["why"]
    assert "VAV-1-02 low -> confirmed" in r["correction_effect"]
    assert r["pass"]["trigger"] == "correction:confirm_mapping"
    # the mapping question is gone, the correction is on record
    assert not any(q["kind"] == "mapping" and q["spec_tag"] == "VAV-1-02" for q in r["unresolved_questions"])
    assert [c["kind"] for c in repo.list_docs("cx_corrections", pid)] == ["confirm_mapping"]
    # a second, identical pass moves nothing
    r2 = engine.apply_correction(repo, pid, {"kind": "resolve_deviation", "deviation_id": next(d["id"] for d in r["deviations"] if d["kind"] == "spec_device_not_found"), "note": "chiller not delivered yet"}, by="owner", history=hist, now=NOW + timedelta(minutes=6))
    assert r2["correction_effect"].startswith("no confidence moved")
    assert not any(d["kind"] == "spec_device_not_found" for d in r2["deviations"])
    resolved = [d for d in repo.list_docs("cx_deviations", pid) if d["kind"] == "spec_device_not_found"]
    assert resolved[0]["resolved_by"] == "owner"  # a human resolution is not reopened by the same evidence
    r3 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(minutes=7))
    assert not any(d["kind"] == "spec_device_not_found" for d in r3["deviations"])


def test_correct_spec_entry_rebuilds_the_spec_model_and_rescores():
    repo, pid = _project()
    hist = history(devices())
    r1 = engine.run_pass(repo, pid, history=hist, now=NOW)
    assert _by_tag(r1)["VAV-1-02"]["confidence"] == "low"
    # the schedule was wrong: the second box is VAV-1-03
    r = engine.apply_correction(repo, pid, {"kind": "correct_spec_entry", "spec_tag": "VAV-1-02", "fields": {"tag": "VAV-1-03"}}, by="designer", history=hist, now=NOW + timedelta(minutes=5))
    tags = {e["tag"] for e in r["spec_model"]["equipment"]}
    assert "VAV-1-03" in tags and "VAV-1-02" not in tags
    assert _by_tag(r)["VAV-1-03"]["field_identity"]["field_id"] == "bacnet:2003@10.0.0.23"
    assert _by_tag(r)["VAV-1-03"]["confidence"] in ("high", "confirmed")
    assert repo.get("cx_spec", pid, "spec")["material"]["equipment"][2]["tag"] == "VAV-1-03"


def test_captures_shrink_the_punch_list_and_keep_partial_reads_partial():
    repo, pid = _project()
    hist = history(devices())
    r1 = engine.run_pass(repo, pid, history=hist, now=NOW)
    open_before = r1["summary"]["open_punch_items"]
    assert open_before > 0
    out = engine.ingest_capture(repo, pid, {"captured_at": (NOW + timedelta(minutes=10)).isoformat(), "engineer": "jo", "location": {"floor": "B", "room": "Chiller room"}, "legible": {"nameplate": {"tag": "CH-1", "model": "YVAA0195"}, "address": "10.0.0.4?"}, "unreadable": ["serial number - label scratched"]}, now=NOW + timedelta(minutes=10))
    cap = out["capture"]
    assert cap["status"] == "ingested"
    assert cap["legible"] == {"nameplate": {"tag": "CH-1", "model": "YVAA0195"}}
    assert cap["partial"] == {"address": "10.0.0.4?"}
    assert len(out["resolved_punch_items"]) == 1
    stored = repo.list_docs("cx_captures", pid)
    assert len(stored) == 1 and stored[0]["group_id"]
    # a second photo two minutes later in the same room joins the same group
    out2 = engine.ingest_capture(repo, pid, {"captured_at": (NOW + timedelta(minutes=12)).isoformat(), "location": {"floor": "B", "room": "Chiller room"}, "legible": {"address": "10.0.0.41"}}, now=NOW + timedelta(minutes=12))
    stored = repo.list_docs("cx_captures", pid)
    assert len({c["group_id"] for c in stored}) == 1
    assert out2["capture"]["status"] == "unprompted"  # the item was already resolved; still kept
    r2 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(minutes=15))
    assert r2["summary"]["open_punch_items"] == open_before - 1
    assert all(it["spec_tag"] != "CH-1" for g in r2["field_list"] for it in g["items"])


def test_absence_matures_into_a_fault_and_an_accepted_risk_covers_it():
    repo, pid = _project()
    hist = history(devices())
    engine.run_pass(repo, pid, history=hist, now=NOW)
    engine.apply_correction(repo, pid, {"kind": "confirm_mapping", "spec_tag": "AHU-1", "field_id": "bacnet:1001@10.0.0.11"}, by="me", history=hist, now=NOW + timedelta(minutes=1))
    # every controller goes quiet (no new samples for anyone)
    t1 = NOW + timedelta(hours=8)
    r1 = engine.run_pass(repo, pid, history=hist, now=t1)
    assert _by_tag(r1)["AHU-1"]["liveness"]["state"] == "missing"
    assert r1["faults"] == []
    r2 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(hours=16))
    assert _by_tag(r2)["AHU-1"]["liveness"]["state"] == "absent"
    assert r2["faults"] == []  # absent, but not yet for 48 h
    r3 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(hours=68))
    absent_faults = [f for f in r3["faults"] if f["kind"] == "sustained_absence"]
    # only the well-identified entities fault: AHU-1 (confirmed) and VAV-1-01 (high); VAV-1-02 is low, so its absence is a deviation to settle, not a fault
    assert sorted(f["spec_tag"] for f in absent_faults) == ["AHU-1", "VAV-1-01"]
    assert _by_tag(r3)["VAV-1-02"]["liveness"]["state"] == "absent" and _by_tag(r3)["VAV-1-02"]["faults"] == []
    assert absent_faults[0]["rule_id"] == reconcile.FAULT_RULE_ABSENCE
    assert r3["accepted_risks"]["covered_this_pass"] == []
    # someone signs for it
    risk = reconcile.new_risk(None, None, "AHU-1", "controller offline pending replacement PO", "owner", now=NOW + timedelta(hours=68))
    repo.upsert("cx_risks", pid, risk)
    r4 = engine.run_pass(repo, pid, history=hist, now=NOW + timedelta(hours=69))
    assert [f["spec_tag"] for f in r4["faults"]] == ["VAV-1-01"]  # the risk covers AHU-1 only
    covered = r4["accepted_risks"]["covered_this_pass"]
    assert len(covered) == 1 and covered[0]["risk_id"] == risk["id"] and covered[0]["kind"] == "sustained_absence"
    assert r4["accepted_risks"]["risks"] == [risk]
    assert _by_tag(r4)["AHU-1"]["accepted_risks"] == [risk["id"]]
    assert "0 of 4 networked entities fresh" in r4["freshness"]["statement"] and "3 known absent" in r4["freshness"]["statement"]


def test_pass_with_no_devices_and_a_spec_reads_as_new_construction_and_warns_about_scope():
    repo = db.MemoryRepo()
    proj = engine.new_project("Empty", cidr_scopes=["10.9.0.0/24"])
    repo.put_project(proj)
    repo.upsert("cx_spec", proj["id"], {"id": "spec", "material": SPEC_MATERIAL, "model": None})
    r = engine.run_pass(repo, proj["id"], history=InMemoryHistory(), now=NOW)
    assert r["pass"]["phase"] == "new_construction" and r["pass"]["phase_assumed"]
    assert "absence is not evidence of nonexistence" in r["pass"]["phase_rationale"]
    assert any("no devices at all this pass" in n for n in r["pass"]["notes"])
    assert all(e["confidence"] in ("unmatched", "low") for e in r["entities"])
    assert r["faults"] == []  # nothing is a fault when nothing has been identified
    assert "empty one" in r["freshness"]["statement"] or r["freshness"]["counts"]["fresh"] == 0
