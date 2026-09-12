"""
Phase 7: the field list is walkable (where / what to find / what to
capture / what would resolve), route-ordered, and shrinks as captures
come in - without ever reading what could not be seen or throwing away a
capture nobody asked for.
"""

from datetime import timedelta

from cx_fixtures import NOW, SPEC_MATERIAL, devices

from timberdoodle.commissioning import alignment, punchlist
from timberdoodle.commissioning.spec_model import build_spec_model

SPEC = build_spec_model(SPEC_MATERIAL)


def _aligned():
    res = alignment.align(SPEC, devices(), "new_construction", None, None, ["10.0.0.0/24"])
    return res.entities, res.deviations


def _items():
    ents, devs = _aligned()
    return punchlist.build_punchlist(ents, devs, "new_construction", NOW), ents


def test_every_item_says_where_what_to_find_what_to_capture_and_what_resolves_it():
    items, _ = _items()
    assert items
    for it in items:
        assert it["where"] and it["what_to_find"] and it["what_to_capture"] and it["what_would_resolve"]
        assert it["status"] == "open" and it["reason"]


def test_items_cover_the_open_questions_in_the_fixture():
    items, _ = _items()
    by_tag = {}
    for it in items:
        by_tag.setdefault(it["spec_tag"] or it["field_id"], []).append(it)
    assert "CH-1" in by_tag  # spec device not found
    ch = by_tag["CH-1"][0]
    assert "nameplate of CH-1" in " ".join(ch["what_to_capture"])
    assert "new_construction" in ch["what_would_resolve"][0]  # phase-specific reading of "not there"
    assert "EF-1" in by_tag and "not networked" in by_tag["EF-1"][0]["reason"]
    assert "VAV-1-02" in by_tag  # low-confidence identity
    v = by_tag["VAV-1-02"][0]
    assert "identity is low" in v["reason"]
    assert any("10.0.0.23" in c for c in v["what_to_capture"])  # the label must show this address to confirm
    assert any("alternative to rule out" in r for r in v["what_would_resolve"])


def test_route_order_is_floor_then_room_then_most_unblocking_and_unlocated_last():
    items, _ = _items()
    keys = [(it["location"].get("floor"), it["location"].get("room")) for it in items]
    ranks = [punchlist._floor_rank(f) for f, _ in keys]
    located = [r for r in ranks if r < 1e5]
    assert located == sorted(located)  # basement before floor 1 before roof
    assert punchlist._floor_rank("B") < punchlist._floor_rank("1") < punchlist._floor_rank("roof")
    # nothing with a location comes after something without one
    has_loc = [it["location"].get("floor") is not None or bool(it["location"].get("room")) for it in items]
    assert has_loc == sorted(has_loc, reverse=True)


def test_grouping_by_location_walks_one_room_at_a_time():
    items, _ = _items()
    groups = punchlist.group_by_location(items)
    keys = [g["location_key"] for g in groups]
    assert len(keys) == len(set(keys))  # a room appears once, with all its items together
    assert all(g["where"] for g in groups)


def test_ahu_unblocks_the_boxes_it_feeds():
    ents, _ = _aligned()
    ahu = next(e for e in ents if e["spec_tag"] == "AHU-1")
    vav = next(e for e in ents if e["spec_tag"] == "VAV-1-02")
    assert punchlist._downstream_count(ahu, ents) >= 2
    assert punchlist._downstream_count(vav, ents) == 0


def test_where_is_honest_when_the_spec_gives_no_location():
    assert "not in the spec" in punchlist._where({}, {"spec_tag": "P-1"})
    assert "only the network address" in punchlist._where(None, {})
    assert punchlist._where({"floor": "2", "room": "204", "description": "above ceiling"}, None) == "floor 2, room 204, above ceiling"


def test_consecutive_captures_at_one_location_are_one_device():
    caps = [
        {"id": "c1", "captured_at": NOW.isoformat(), "location": {"floor": "1", "room": "101"}, "legible": {"address": "10.0.0.21"}},
        {"id": "c2", "captured_at": (NOW + timedelta(minutes=2)).isoformat(), "location": {"floor": "1", "room": "101"}, "legible": {"nameplate": {"tag": "VAV-1-01", "model": "TU-7"}}},
        {"id": "c3", "captured_at": (NOW + timedelta(minutes=4)).isoformat(), "location": {"floor": "1", "room": "102"}, "legible": {"nameplate": {"tag": "VAV-1-02"}}},
        {"id": "c4", "captured_at": (NOW + timedelta(hours=3)).isoformat(), "location": {"floor": "1", "room": "101"}, "legible": {"tag": "VAV-1-01"}},
    ]
    grouped = punchlist.assign_groups(caps)
    g = {c["id"]: c["group_id"] for c in grouped}
    assert g["c1"] == g["c2"]          # same room, two minutes apart
    assert g["c3"] != g["c1"]          # different room
    assert g["c4"] != g["c1"]          # same room, hours later - a second visit
    merged = {m["legible"].get("address") or m["legible"].get("nameplate", {}).get("tag") or m["legible"].get("tag"): m for m in punchlist.merge_groups(grouped)}
    one = merged["10.0.0.21"]
    assert one["legible"]["nameplate"]["tag"] == "VAV-1-01" and one["legible"]["address"] == "10.0.0.21"


def test_only_legible_characters_are_read_and_nothing_is_completed_by_guessing():
    items, ents = _items()
    cap, resolved = punchlist.ingest_capture({"location": {"floor": "B", "room": "Chiller room"}, "legible": {"nameplate": {"tag": "CH-1", "serial": "4?7A-??"}, "address": "10.0.0.4?"}, "unreadable": ["model - label scratched"]}, items, ents, NOW)
    assert cap["legible"] == {"nameplate": {"tag": "CH-1"}}
    assert cap["partial"] == {"nameplate": {"serial": "4?7A-??"}, "address": "10.0.0.4?"}
    assert cap["unreadable"] == ["model - label scratched"]
    assert cap["status"] == "ingested"
    assert "CH-1" in cap["corroborates"]
    # it was taken at the CH-1 location and shows the nameplate the item asked for
    assert len(resolved) == 1 and resolved[0]["spec_tag"] == "CH-1" and resolved[0]["status"] == "resolved"
    assert cap["resolved"] == [resolved[0]["id"]]


def test_a_capture_with_nothing_legible_is_kept_and_marked_unreadable_not_dropped():
    items, ents = _items()
    cap, resolved = punchlist.ingest_capture({"location": {"floor": "1", "room": "102"}, "legible": {}, "note": "label faded"}, items, ents, NOW)
    assert cap["status"] == "unreadable"
    assert cap["unreadable"] == ["nothing legible was transcribed"]
    assert resolved == []


def test_unprompted_capture_is_kept_and_matched_by_what_it_shows():
    items, ents = _items()
    # nobody asked for anything at AHU-1 (it is high confidence) - a photo turns up anyway
    cap, resolved = punchlist.ingest_capture({"location": {"floor": "1", "room": "Mech 101"}, "legible": {"tag": "AHU-1", "address": "10.0.0.11"}}, items, ents, NOW)
    assert cap["status"] == "unprompted"
    assert set(cap["corroborates"]) >= {"AHU-1", "bacnet:1001@10.0.0.11"}
    assert resolved == []


def test_a_capture_that_does_not_show_what_was_asked_for_does_not_close_the_item():
    items, ents = _items()
    target = next(it for it in items if it["spec_tag"] == "VAV-1-02")
    cap, resolved = punchlist.ingest_capture({"punch_item_id": target["id"], "legible": {"observation": "ceiling tile removed, box visible"}}, items, ents, NOW)
    assert resolved == []
    assert target["status"] == "open"
    assert "does not show what was asked for" in cap["note"]
