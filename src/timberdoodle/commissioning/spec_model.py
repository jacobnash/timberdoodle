"""
Phase 1 - build the intended inventory from the specification alone,
before looking at a single network packet.

Input is structured spec material (equipment schedule rows, point
schedule rows, with the document/page each came from). Parsing PDFs or
scans into that shape is upstream of this module; what this does is
normalize it into the canonical vocabulary, score how defensible each
entry is, and flag every internal inconsistency it finds - duplicate
tags, equipment with no points, points for equipment that isn't in the
schedule, units that contradict a point's own name - *without*
reconciling any of them. Those are for a human to see.

A clean parse needs no sign-off: the model is output and alignment
proceeds. Only entries below threshold land in `escalate`.
"""

from __future__ import annotations

import re

from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.model import (
    Inconsistency,
    SpecEquipment,
    SpecModel,
    SpecPoint,
    now_iso,
)
from timberdoodle.commissioning.points import classify_object

# Below this an equipment entry isn't a defensible hypothesis on its own
# and is escalated before alignment leans on it.
ESCALATION_THRESHOLD = 0.5

_TAG_LETTERS = re.compile(r"^[A-Za-z]+")


def normalize_tag(tag: str | None) -> str:
    """`AHU-3` / `AHU_3` / `ahu 3` / `AHU3` -> `AHU3`. Case-folded,
    delimiters dropped - the predictable variation a field name shows."""
    if not tag:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(tag).upper())


def tag_letters(tag: str | None) -> str:
    m = _TAG_LETTERS.match(str(tag or "").strip())
    return m.group(0).upper() if m else ""


def tag_number(tag: str | None) -> str:
    """The identifying remainder after the type letters, with leading zeros
    dropped per digit group so `VAV-2-05` and `V2_5` agree: `AHU-3` -> `3`,
    `VAV-2-05` -> `25`, `RTU3A` -> `3A`."""
    text = str(tag or "").strip()
    letters = tag_letters(tag)
    rest = text[len(letters):]
    groups = re.findall(r"\d+|[A-Za-z]+", rest)
    return "".join((g.lstrip("0") or "0") if g.isdigit() else g.upper() for g in groups)


def infer_type_from_tag(tag: str | None) -> str | None:
    """`AHU-3` -> air handling unit via the alias table. Only the leading
    letters count, and only exact alias matches - `V` for VAV is a real
    convention, `X` is not."""
    letters = tag_letters(tag)
    if not letters:
        return None
    return V.canonical_type(letters)


def _spec_point(row: dict, equipment_tag: str) -> SpecPoint:
    name = str(row.get("name") or row.get("point") or "").strip()
    stated_function = row.get("function") or row.get("description")
    direction = row.get("direction")
    if direction not in ("input", "output", None):
        direction = None
    # The classifier reads the spec's own words the same way it reads a
    # field object name, so spec and field land in one vocabulary.
    pseudo_object = {"input": "analogInput,0", "output": "analogOutput,0"}.get(direction or "", None)
    classified = classify_object(name, pseudo_object, row.get("units"), description=stated_function)
    role = row.get("role") if row.get("role") in V.ROLES else classified.get("role")
    notes = list(classified.get("notes", []))
    if classified.get("function") is None:
        notes.append(f"could not read a function from '{name}'/'{stated_function or ''}' - specified point kept as written, it will only match a field object by name")
    return {
        "name": name,
        "function": classified.get("function"),
        "role": role,
        "units": classified.get("units"),
        "direction": direction or ({"input": "input", "output": "output"}.get(classified.get("direction") or "", None)),
        "required": bool(row.get("required", True)),
        "source": row.get("source") or equipment_tag,
        "notes": notes,
    }


def _score(equipment: SpecEquipment) -> float:
    score = float(equipment.get("extraction_confidence", 1.0))
    flags = set(equipment.get("flags", []))
    if "unknown_type" in flags:
        score *= 0.6
    if "no_points" in flags:
        score *= 0.8
    if "duplicate_tag" in flags:
        score *= 0.7
    if "points_unreadable" in flags:
        score *= 0.85
    return round(max(0.0, min(1.0, score)), 2)


def _confidence_word(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= ESCALATION_THRESHOLD:
        return "medium"
    return "low"


def build_spec_model(material: dict) -> SpecModel:
    """Structured spec material -> SpecModel. Never raises on a bad row;
    the row is kept with flags so a human sees exactly what was wrong."""
    rows: list[dict] = list(material.get("equipment") or [])
    flat_points: list[dict] = list(material.get("points") or [])
    assumptions: list[str] = list(material.get("assumptions") or [])
    inconsistencies: list[Inconsistency] = []

    by_norm: dict[str, list[SpecEquipment]] = {}
    equipment: list[SpecEquipment] = []
    for row in rows:
        tag = str(row.get("tag") or "").strip()
        written_type = row.get("type")
        ctype = V.canonical_type(written_type) or infer_type_from_tag(tag)
        flags: list[str] = []
        if ctype is None:
            flags.append("unknown_type")
        elif not written_type or V.canonical_type(written_type) is None:
            flags.append("type_inferred_from_tag")
        vintage = row.get("vintage")
        networked = row.get("networked")
        if networked is None:
            networked = not bool(vintage and "pneumatic" in str(vintage).lower())
            if not networked:
                assumptions.append(f"{tag}: vintage '{vintage}' mentions pneumatics - assumed NOT networked (no BACnet objects expected; nameplate capture is the only way to verify it)")
        location = row.get("location")
        if isinstance(location, str):
            location = {"description": location}
        entry: SpecEquipment = {
            "tag": tag,
            "type": ctype,
            "type_as_written": written_type,
            "description": str(row.get("description") or (V.EQUIPMENT_TYPES.get(ctype or "", {}).get("description", "") if ctype else "")),
            "serves": [str(s) for s in (row.get("serves") or [])],
            "fed_by": [str(s) for s in (row.get("fed_by") or [])],
            "feeds": [str(s) for s in (row.get("feeds") or [])],
            "location": location,
            "expected_points": [_spec_point(p, tag) for p in (row.get("points") or [])],
            "source": {"document": (row.get("source") or {}).get("document"), "page": (row.get("source") or {}).get("page")},
            "vintage": vintage,
            "networked": bool(networked),
            "extraction_confidence": float(row.get("extraction_confidence", 1.0)),
            "flags": flags,
            "confidence": "high",
        }
        equipment.append(entry)
        by_norm.setdefault(normalize_tag(tag), []).append(entry)

    # Flat point-schedule rows attach to their equipment; rows naming
    # equipment that isn't in the schedule are an inconsistency, not a
    # reason to invent equipment.
    for row in flat_points:
        owner = normalize_tag(row.get("equipment") or row.get("equip") or row.get("tag"))
        targets = by_norm.get(owner)
        if not targets:
            inconsistencies.append({
                "kind": "point_for_unknown_equipment",
                "detail": f"point schedule lists '{row.get('name')}' for '{row.get('equipment')}', which is not in the equipment schedule",
                "tags": [str(row.get("equipment"))],
            })
            continue
        for target in targets:
            target["expected_points"].append(_spec_point(row, target["tag"]))

    for entries in by_norm.values():
        if len(entries) > 1:
            inconsistencies.append({
                "kind": "duplicate_tag",
                "detail": f"tag appears {len(entries)} times in the schedule ({', '.join(e['tag'] for e in entries)}) - alignment cannot tell them apart until one is renamed or removed",
                "tags": [e["tag"] for e in entries],
            })
            for e in entries:
                e["flags"].append("duplicate_tag")

    known = set(by_norm)
    for e in equipment:
        if not e["expected_points"]:
            e["flags"].append("no_points")
            inconsistencies.append({"kind": "no_points", "detail": f"{e['tag']} is in the equipment schedule but has no points anywhere in the point list", "tags": [e["tag"]]})
        if e["expected_points"] and all(p.get("function") is None for p in e["expected_points"]):
            e["flags"].append("points_unreadable")
        for p in e["expected_points"]:
            for note in p.get("notes", []):
                if "contradict" in note:
                    inconsistencies.append({"kind": "unit_contradiction", "detail": f"{e['tag']} point '{p['name']}': {note}", "tags": [e["tag"]]})
        for ref in e["fed_by"] + e["feeds"]:
            if normalize_tag(ref) not in known:
                inconsistencies.append({"kind": "unresolved_reference", "detail": f"{e['tag']} references '{ref}' (fed_by/feeds) which is not in the equipment schedule", "tags": [e["tag"], ref]})
        # feeds/fed_by are two statements of one fact; when they disagree
        # both are kept and the disagreement is flagged.
        for downstream in e["feeds"]:
            for d in by_norm.get(normalize_tag(downstream), []):
                if e["tag"] not in d["fed_by"] and normalize_tag(e["tag"]) not in {normalize_tag(x) for x in d["fed_by"]}:
                    inconsistencies.append({"kind": "topology_one_sided", "detail": f"{e['tag']} says it feeds {d['tag']}, but {d['tag']} does not list {e['tag']} in fed_by", "tags": [e["tag"], d["tag"]]})
        expected_count = next((r.get("expected_terminal_count") for r in rows if str(r.get("tag") or "").strip() == e["tag"]), None)
        if expected_count is not None:
            actual = sum(1 for d in equipment if normalize_tag(e["tag"]) in {normalize_tag(x) for x in d["fed_by"]})
            if int(expected_count) != actual:
                inconsistencies.append({"kind": "count_mismatch", "detail": f"{e['tag']} schedule says it serves {expected_count} terminal units; {actual} in the schedule list it as fed_by", "tags": [e["tag"]]})

    escalate: list[dict] = []
    for e in equipment:
        score = _score(e)
        e["confidence"] = _confidence_word(score)  # type: ignore[typeddict-item]
        if score < ESCALATION_THRESHOLD:
            escalate.append({
                "spec_tag": e["tag"],
                "score": score,
                "flags": e["flags"],
                "question": _question_for(e),
                "one_action": {"accept": {"kind": "accept_spec_entry", "spec_tag": e["tag"]}, "correct": {"kind": "correct_spec_entry", "spec_tag": e["tag"], "fields": ["type", "points", "tag"]}},
            })

    if any(i["kind"] == "duplicate_tag" for i in inconsistencies):
        overall = "medium"
    elif not escalate:
        overall = "high"
    elif len(escalate) * 4 < max(1, len(equipment)):
        overall = "medium"
    else:
        overall = "low"

    return {
        "equipment": equipment,
        "inconsistencies": inconsistencies,
        "escalate": escalate,
        "assumptions": assumptions,
        "documents": list(material.get("documents") or []),
        "built_at": now_iso(),
        "confidence": overall,  # type: ignore[typeddict-item]
    }


def _question_for(e: SpecEquipment) -> str:
    flags = set(e.get("flags", []))
    parts = []
    if "unknown_type" in flags:
        parts.append(f"what kind of equipment is '{e.get('type_as_written') or e['tag']}'? The vocabulary knows: {', '.join(sorted(V.EQUIPMENT_TYPES))}")
    if "no_points" in flags:
        parts.append("which points does it have? Without any, alignment can only match on the tag")
    if "duplicate_tag" in flags:
        parts.append("the tag is duplicated in the schedule - which entry is real?")
    if e.get("extraction_confidence", 1.0) < ESCALATION_THRESHOLD:
        parts.append(f"the source ({cite(e)}) was hard to read - confirm the tag and type")
    return "; ".join(parts) or "confirm this entry"


def cite(e: SpecEquipment) -> str:
    """'M-601 p.3', 'M-601', or 'no source recorded' - never 'None p.None'."""
    src = e.get("source") or {}
    doc, page = src.get("document"), src.get("page")
    if doc and page is not None:
        return f"{doc} p.{page}"
    if doc:
        return str(doc)
    if page is not None:
        return f"p.{page} (document not recorded)"
    return "no source recorded"


def spec_by_norm(spec: SpecModel) -> dict[str, SpecEquipment]:
    """Normalized tag -> entry, first wins (duplicates are already flagged)."""
    out: dict[str, SpecEquipment] = {}
    for e in spec.get("equipment", []):
        out.setdefault(normalize_tag(e["tag"]), e)
    return out
