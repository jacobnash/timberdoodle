"""
Phase 7 - the field verification list, and what comes back from it.

Every open question the data cannot settle by itself becomes a punch item
that a person can walk to: *where* (floor, room, what to look for on the
way), *what to find* (the enclosure, the nameplate, the sensor), *what to
capture* (which label, which screen) and *what would resolve it* (which
deviation or confidence gap closes when the capture arrives). Items are
grouped by location and ordered by route - floor, then room - and inside
a room by how many other entities are waiting on the answer, so the walk
settles the AHU before it settles the six VAV boxes hanging off it.

Field captures come in continuously, and the list shrinks as they do:

  * consecutive captures at one location are one device. A photo of an
    enclosure label (the address) followed by a photo of the nameplate
    (the tag) two minutes later at the same room are the same unit, and
    the two legible sets are combined into one piece of evidence.
  * only what is legible is read. A partly readable label keeps its
    unknown characters as '?' and is stored under `partial`; nothing is
    completed by guessing. A capture with nothing legible is kept and
    marked `unreadable` - it still proves someone stood there.
  * unprompted captures (no punch item asked for them) are kept, matched
    to entities by whatever they show, and reported. They are never
    discarded for being unexpected.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from timberdoodle.commissioning.model import (
    Deviation,
    Entity,
    FieldCapture,
    Location,
    PunchItem,
    new_id,
)
from timberdoodle.commissioning.spec_model import normalize_tag

# Two captures at one location this close together are one visit to one device.
GROUP_GAP_SECONDS = 20 * 60

_FLOOR_ORDER = {"basement": -1, "b": -1, "lower": -1, "ground": 0, "g": 0, "roof": 99, "penthouse": 98, "mech": 50}


def location_key(loc: Location | None) -> str:
    loc = loc or {}
    return "/".join(str(loc.get(k) or "?") for k in ("building", "floor", "room"))


def _floor_rank(floor: object) -> float:
    if floor is None:
        return 1e6
    s = str(floor).strip().lower()
    if s in _FLOOR_ORDER:
        return _FLOOR_ORDER[s]
    m = re.match(r"^(?:l|lvl|level|fl|floor)?\s*(-?\d+)", s)
    if m:
        return float(m.group(1))
    return 1e5


def _where(loc: Location | None, entity: Entity | None) -> str:
    loc = loc or {}
    bits = []
    if loc.get("building"):
        bits.append(f"building {loc['building']}")
    if loc.get("floor") is not None:
        bits.append(f"floor {loc['floor']}")
    if loc.get("room"):
        bits.append(f"room {loc['room']}")
    if loc.get("description"):
        bits.append(str(loc["description"]))
    if not bits:
        tag = (entity or {}).get("spec_tag")
        return f"location not in the spec - find {tag or 'the unit'} from the mechanical drawings or ask the installing contractor" if tag else "location unknown - only the network address is known; trace from the switch port"
    return ", ".join(bits)


def _downstream_count(entity: Entity, entities: list[Entity]) -> int:
    """How many other entities list this one as their source or parent -
    settling it settles a question on each of them."""
    tag = normalize_tag(entity.get("spec_tag"))
    if not tag:
        return 0
    n = 0
    for e in entities:
        rel = e.get("relationships") or {}
        if tag in {normalize_tag(t) for t in rel.get("is_fed_by", [])} or tag in {normalize_tag(t) for t in rel.get("is_part_of", [])}:
            n += 1
    return n + len((entity.get("relationships") or {}).get("feeds", [])) + len((entity.get("relationships") or {}).get("serves", []))


def _item(entity: Entity, entities: list[Entity], reason: str, what_to_find: str, capture: list[str], resolves: list[str], deviation_ids: list[str], severity: str, now: str) -> PunchItem:
    loc = (entity.get("relationships") or {}).get("is_located_in")
    return {
        "id": new_id("punch"), "location": loc or {}, "location_key": location_key(loc), "where": _where(loc, entity),
        "what_to_find": what_to_find, "what_to_capture": capture, "what_would_resolve": resolves, "reason": reason,
        "entity_id": entity["id"], "spec_tag": entity.get("spec_tag"), "field_id": (entity.get("field_identity") or {}).get("field_id"),
        "deviation_ids": deviation_ids, "unblocks": _downstream_count(entity, entities), "severity": severity, "status": "open",
    }


def build_punchlist(entities: list[Entity], deviations: list[Deviation], phase: str | None, now: datetime | None = None) -> list[PunchItem]:
    now_s = (now or datetime.now(timezone.utc)).isoformat()
    open_devs = {d["id"]: d for d in deviations if d.get("status") == "open"}
    items: list[PunchItem] = []

    for e in entities:
        tag = e.get("spec_tag") or (e.get("field_identity") or {}).get("name") or e["id"]
        fid = (e.get("field_identity") or {})
        devs = [open_devs[i] for i in e.get("deviations", []) if i in open_devs]
        dev_ids = [d["id"] for d in devs]
        kinds = {d["kind"] for d in devs}
        conf = e.get("confidence")
        live = (e.get("liveness") or {}).get("state")

        if live == "not_networked" and conf != "confirmed":
            items.append(_item(e, entities, f"{tag} is not networked - only a person can verify it exists", f"the {e.get('canonical_type') or 'unit'} tagged {tag}", [f"nameplate of {tag} (model, serial) with the tag label in frame"], [f"confirms {tag} physically exists; nothing else can"], dev_ids, "info", now_s))
            continue

        if "spec_device_not_found" in kinds:
            d = next(x for x in devs if x["kind"] == "spec_device_not_found")
            find = f"the {e.get('canonical_type') or 'unit'} tagged {tag}, or the empty spot where it should be"
            cap = [f"nameplate of {tag} if installed", "enclosure label of its controller (address / MAC / device instance)", "the network drop it is patched to (switch / port label)", "if not installed: a photo of the location as it is"]
            res = [f"deviation {d['id']}: found + labelled -> a confirm_mapping to the address on the label; found + not labelled -> re-scan that segment; not there -> record as unbuilt ({phase or 'phase unknown'})"]
            items.append(_item(e, entities, d["interpretation"], find, cap, res, dev_ids, d["severity"], now_s))
            continue

        if "duplicate_claim" in kinds:
            d = next(x for x in devs if x["kind"] == "duplicate_claim")
            items.append(_item(e, entities, d["interpretation"], f"the controller physically wired to {tag}", [f"enclosure label of the controller at {tag} showing its address", "controller's own name/program screen if it has a display"], [f"deviation {d['id']}: the address on the label is the real {tag}; the other device gets a reject_mapping"], dev_ids, d["severity"], now_s))

        if "type_mismatch" in kinds:
            d = next(x for x in devs if x["kind"] == "type_mismatch")
            items.append(_item(e, entities, d["interpretation"], f"the unit tagged {tag}", [f"nameplate of {tag} (model number shows what it actually is)", "controller program name / application selection screen"], [f"deviation {d['id']}: correct_type to what the nameplate says, or confirm the spec type"], dev_ids, d["severity"], now_s))

        if "topology_mismatch" in kinds:
            d = next(x for x in devs if x["kind"] == "topology_mismatch")
            items.append(_item(e, entities, d["interpretation"], f"the duct/pipe feeding {tag}", ["photo along the supply duct/pipe back to its source unit, with that unit's tag in frame"], [f"deviation {d['id']}: confirm_topology feeds -> {tag} from whichever unit it is"], dev_ids, d["severity"], now_s))

        if "field_device_not_in_spec" in kinds:
            d = next(x for x in devs if x["kind"] == "field_device_not_in_spec")
            addr = fid.get("address") or fid.get("field_id")
            items.append(_item(e, entities, d["interpretation"], f"the controller at {addr} ({fid.get('name') or 'unnamed'}) and whatever it controls", [f"enclosure label at {addr}", "nameplate of the equipment it is wired to", "any tag stencilled on the equipment"], [f"deviation {d['id']}: a tag -> correct_mapping to a spec row (or add it to the spec); no tag -> decide add/ignore"], dev_ids, d["severity"], now_s))
            continue

        if conf in ("medium", "low") and e.get("field_identity"):
            addr = fid.get("address") or fid.get("field_id")
            why = "; ".join(b["detail"] for b in e.get("confidence_basis", []) if b.get("kind") in ("tag", "confidence_note") and b.get("detail"))
            items.append(_item(e, entities, f"identity is {conf}: {why}", f"the {e.get('canonical_type') or 'unit'} tagged {tag} and its controller", [f"enclosure label of the controller at {tag} - must show {addr} to confirm this mapping", f"tag label on {tag} itself"], [f"label shows {addr} -> confirm_mapping; shows another address -> correct_mapping to that one"] + [f"alternative to rule out: {a['interpretation']}" for a in e.get("alternatives", [])[:3]], dev_ids, "warning" if conf == "low" else "info", now_s))

        if "point_mismatch" in kinds:
            d = next(x for x in devs if x["kind"] == "point_mismatch")
            missing = [p for p in e.get("points", []) if p.get("status") == "specified_absent"]
            if missing and conf in ("confirmed", "high", "medium"):
                names = ", ".join(str(p.get("spec_name") or p.get("function")) for p in missing[:6])
                items.append(_item(e, entities, f"specified points not exposed: {names}", f"the sensors/actuators for {names} on {tag}", ["photo of each sensor/actuator in place (or its absence)", "the controller's point list screen if it has one"], [f"deviation {d['id']}: sensor present -> object is unmapped in the program (contractor item); sensor absent -> installation deviation"], dev_ids, "info", now_s))

        for r in (e.get("ladder") or {}).get("rungs", []):
            if r.get("result") == "fail" and r.get("rung") == 2 and conf in ("confirmed", "high"):
                items.append(_item(e, entities, f"liveness: {r.get('symptom')}", f"the flat-lined sensor(s) on {tag}", ["photo of the sensor and its wiring at the controller terminal", "controller's live reading for that input"], [f"distinguishes: {'; '.join(c.get('would_distinguish', '') for c in r.get('candidates', [])[:3])}"], dev_ids, "warning", now_s))

    return order_by_route(items)


def order_by_route(items: list[PunchItem]) -> list[PunchItem]:
    """Floor, then room, then most-unblocking first. Items with no
    location go last - they are the ones a person cannot walk to yet."""
    def key(it: PunchItem):
        loc = it.get("location") or {}
        has_loc = 0 if (loc.get("floor") is not None or loc.get("room")) else 1
        return (has_loc, str(loc.get("building") or ""), _floor_rank(loc.get("floor")), str(loc.get("room") or ""), -int(it.get("unblocks") or 0), it.get("spec_tag") or "")
    return sorted(items, key=key)


def group_by_location(items: list[PunchItem]) -> list[dict]:
    groups: list[dict] = []
    for it in items:
        k = it.get("location_key") or "?/?/?"
        if not groups or groups[-1]["location_key"] != k:
            groups.append({"location_key": k, "location": it.get("location") or {}, "where": it.get("where"), "items": []})
        groups[-1]["items"].append(it)
    return groups


# --- captures --------------------------------------------------------------------

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def assign_groups(captures: list[FieldCapture]) -> list[FieldCapture]:
    """Consecutive captures at the same location within GROUP_GAP_SECONDS
    share a group_id: one visit, one device. Existing group ids are kept."""
    ordered = sorted(captures, key=lambda c: (_parse(c.get("captured_at")) or datetime.min.replace(tzinfo=timezone.utc)))
    prev: FieldCapture | None = None
    for cap in ordered:
        loc_k = location_key(cap.get("location"))
        t = _parse(cap.get("captured_at"))
        if prev is not None and location_key(prev.get("location")) == loc_k and t and _parse(prev.get("captured_at")) and (t - _parse(prev["captured_at"])).total_seconds() <= GROUP_GAP_SECONDS:  # type: ignore[operator]
            cap["group_id"] = prev.get("group_id") or new_id("grp")
            prev["group_id"] = cap["group_id"]
        elif not cap.get("group_id"):
            cap["group_id"] = new_id("grp")
        prev = cap
    return ordered


def merge_groups(captures: list[FieldCapture]) -> list[FieldCapture]:
    """One synthetic capture per group with the union of legible fields -
    what alignment should reason from. A later legible read of the same
    field overrides an earlier one only if the earlier was partial."""
    by_group: dict[str, FieldCapture] = {}
    for cap in sorted(captures, key=lambda c: c.get("captured_at") or ""):
        g = cap.get("group_id") or cap.get("id") or new_id("grp")
        cur = by_group.get(g)
        if cur is None:
            by_group[g] = {**cap, "legible": dict(cap.get("legible") or {}), "partial": dict(cap.get("partial") or {}), "unreadable": list(cap.get("unreadable") or []), "id": g, "corroborates": list(cap.get("corroborates") or [])}
            continue
        for k, v in (cap.get("legible") or {}).items():
            if k == "nameplate" and isinstance(v, dict) and isinstance(cur["legible"].get("nameplate"), dict):
                cur["legible"]["nameplate"] = {**cur["legible"]["nameplate"], **v}
            else:
                cur["legible"][k] = v
        for k, v in (cap.get("partial") or {}).items():
            if k not in cur["legible"]:
                cur["partial"][k] = v
        cur["unreadable"] = sorted(set(cur["unreadable"]) | set(cap.get("unreadable") or []))
        cur["corroborates"] = sorted(set(cur["corroborates"]) | set(cap.get("corroborates") or []))
    return list(by_group.values())


def _normalize_legible(legible: dict) -> tuple[dict, dict]:
    """Split what came in into clean reads and partial reads. Anything
    containing '?' (an unread character) is partial, never legible."""
    clean: dict = {}
    partial: dict = {}
    for k, v in (legible or {}).items():
        if isinstance(v, dict):
            sub_clean, sub_partial = _normalize_legible(v)
            if sub_clean:
                clean[k] = sub_clean
            if sub_partial:
                partial[k] = sub_partial
        elif v is None or (isinstance(v, str) and not v.strip()):
            continue
        elif isinstance(v, str) and "?" in v:
            partial[k] = v
        else:
            clean[k] = v
    return clean, partial


def ingest_capture(raw: dict, items: list[PunchItem], entities: list[Entity], now: datetime | None = None) -> tuple[FieldCapture, list[PunchItem]]:
    """One capture in -> a stored FieldCapture, plus the punch items it
    resolves (marked, not removed - resolution is auditable). Matching
    order: the punch item it was taken for; else any open item at the
    same location whose entity the legible tag/address names; else it is
    unprompted and matched to entities directly."""
    now = now or datetime.now(timezone.utc)
    clean, partial = _normalize_legible(raw.get("legible") or {})
    partial.update(raw.get("partial") or {})
    unreadable = list(raw.get("unreadable") or [])
    cap: FieldCapture = {
        "id": raw.get("id") or new_id("cap"), "captured_at": raw.get("captured_at") or now.isoformat(), "engineer": raw.get("engineer"),
        "location": raw.get("location"), "punch_item_id": raw.get("punch_item_id"), "note": raw.get("note"), "photo_ref": raw.get("photo_ref"),
        "legible": clean, "partial": partial, "unreadable": unreadable, "status": "ingested", "group_id": raw.get("group_id"), "resolved": [], "corroborates": [],
    }
    if not clean and not partial:
        cap["status"] = "unreadable"
        if not unreadable:
            cap["unreadable"] = ["nothing legible was transcribed"]

    tag = clean.get("tag") or (clean.get("nameplate") or {}).get("tag") or clean.get("controller_name")
    addr = clean.get("ip") or clean.get("address")
    norm = normalize_tag(tag) if tag else ""
    ents_by_tag = {normalize_tag(e.get("spec_tag")): e for e in entities if e.get("spec_tag")}
    ents_by_addr = {str((e.get("field_identity") or {}).get("address") or "").split(":")[0]: e for e in entities if (e.get("field_identity") or {}).get("address")}
    hit_entities: list[Entity] = []
    if norm and norm in ents_by_tag:
        hit_entities.append(ents_by_tag[norm])
    if addr and str(addr).split(":")[0] in ents_by_addr:
        e = ents_by_addr[str(addr).split(":")[0]]
        if e not in hit_entities:
            hit_entities.append(e)
    cap["corroborates"] = sorted({x for e in hit_entities for x in (e.get("spec_tag"), (e.get("field_identity") or {}).get("field_id")) if x})

    resolved: list[PunchItem] = []
    target = next((it for it in items if it["id"] == cap.get("punch_item_id") and it.get("status") == "open"), None)
    if target is None and cap.get("location"):
        lk = location_key(cap["location"])
        hit_ids = {e["id"] for e in hit_entities}
        target = next((it for it in items if it.get("status") == "open" and it.get("location_key") == lk and it.get("entity_id") in hit_ids), None)
    if target is None and not cap.get("punch_item_id"):
        cap["status"] = "unprompted" if cap["status"] != "unreadable" else "unreadable"
    if target is not None and cap["status"] != "unreadable":
        # A capture settles an item only if it shows something the item
        # asked for - a photo of the wrong wall does not close a question.
        asked = " ".join(target.get("what_to_capture", [])).lower()
        shows_tag = bool(tag) or bool(clean.get("nameplate"))
        useful = (
            (shows_tag and ("nameplate" in asked or "tag" in asked))
            or (bool(addr) and ("address" in asked or "label" in asked))
            or (bool(clean.get("observation")) and "location as it is" in asked)
        )
        if useful:
            target["status"] = "resolved"
            resolved.append(target)
            cap["resolved"] = [target["id"]]
        else:
            cap["note"] = (cap.get("note") or "") + f" [kept against punch item {target['id']} but it does not show what was asked for: {asked[:120]}]"
    return cap, resolved
