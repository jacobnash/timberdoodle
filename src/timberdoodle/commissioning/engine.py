"""
One commissioning pass, end to end, and the correction loop around it.

    load state -> discover (graph + pushed devices) -> constraints from
    corrections and captures -> align -> climb the ladder -> reconcile
    against the last pass -> punch list -> escalations -> report

Everything below is deterministic given the same inputs, which is what
lets a correction re-run the whole building and report exactly which
confidences moved and why. The pass never writes to the graph unless
asked (`project=True`), and then only the Brick projection of settled
entities.

The report is the agent's whole output for a pass, in the sections the
brief asks for, machine-parseable:

    spec_model, mappings, ladder_results, deviations, faults,
    accepted_risks, field_list, unresolved_questions
    (+ changes, freshness, assumptions, pass metadata)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TypeAlias

from timberdoodle import faults as fault_store
from timberdoodle.commissioning import (
    alignment,
    db,
    discovery,
    ladder,
    projection,
    punchlist,
    reconcile,
    spec_model,
)
from timberdoodle.commissioning.history import History, PostgresHistory
from timberdoodle.commissioning.model import (
    PHASES,
    AcceptedRisk,
    Correction,
    Deviation,
    DiscoveredDevice,
    Entity,
    FieldCapture,
    Phase,
    Project,
    PunchItem,
    SpecModel,
    new_id,
    now_iso,
)
from timberdoodle.commissioning.spec_model import normalize_tag

log = logging.getLogger(__name__)


@dataclass
class State:
    project: Project
    spec_material: dict
    spec: SpecModel | None
    devices: list[DiscoveredDevice]
    entities: list[Entity]
    deviations: list[Deviation]
    corrections: list[Correction]
    captures: list[FieldCapture]
    risks: list[AcceptedRisk]
    punch: list[PunchItem]
    passes: list[dict] = field(default_factory=list)


Repo: TypeAlias = "db.PgRepo | db.MemoryRepo"


def load_state(repo: Repo, project_id: str) -> State:
    project = repo.get_project(project_id)
    if project is None:
        raise KeyError(f"project {project_id!r} not found")
    spec_row = repo.get("cx_spec", project_id, "spec") or {}
    return State(
        project=project,  # type: ignore[arg-type]
        spec_material=spec_row.get("material") or {},
        spec=spec_row.get("model"),
        devices=repo.list_docs("cx_devices", project_id),  # type: ignore[arg-type]
        entities=repo.list_docs("cx_entities", project_id),  # type: ignore[arg-type]
        deviations=repo.list_docs("cx_deviations", project_id),  # type: ignore[arg-type]
        corrections=repo.list_docs("cx_corrections", project_id),  # type: ignore[arg-type]
        captures=repo.list_docs("cx_captures", project_id),  # type: ignore[arg-type]
        risks=repo.list_docs("cx_risks", project_id),  # type: ignore[arg-type]
        punch=repo.list_docs("cx_punch", project_id),  # type: ignore[arg-type]
        passes=repo.list_docs("cx_passes", project_id, limit=20),
    )


def new_project(name: str, phase: str | None = None, cidr_scopes: list[str] | None = None, settings: dict | None = None) -> Project:
    if phase is not None and phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    ts = now_iso()
    return {"id": new_id("proj"), "name": name, "phase": phase, "phase_assumed": False, "phase_rationale": "stated by the user" if phase else None, "cidr_scopes": list(cidr_scopes or []), "settings": dict(settings or {}), "created_at": ts, "updated_at": ts}  # type: ignore[typeddict-item]


def infer_phase(project: Project, spec: SpecModel | None, devices: list[DiscoveredDevice], entities_found: int) -> tuple[Phase | None, bool, str]:
    """When the phase is not stated, infer it from what the data looks
    like - and say so, every time, because absence means opposite things
    in different phases and a wrong phase reads every gap wrong."""
    if project.get("phase"):
        return project["phase"], False, project.get("phase_rationale") or "stated by the user"
    n_spec = len((spec or {}).get("equipment", []))
    docs = " ".join(str(d.get("title") or d.get("document") or "") for d in (spec or {}).get("documents", [])).lower()
    if any(w in docs for w in ("retrofit", "replacement", "upgrade", "renovation")):
        return "retrofit", True, f"ASSUMED retrofit: spec documents are titled as a retrofit/replacement ({docs[:60]}). State the phase on the project to override."
    if n_spec == 0 and devices:
        return "operations", True, "ASSUMED operations: no spec material, only a live network - reading the building as-found. State the phase to override."
    if n_spec and devices:
        found_frac = entities_found / max(1, n_spec)
        if found_frac < 0.5:
            return "new_construction", True, f"ASSUMED new construction: only {entities_found} of {n_spec} scheduled units are on the network yet. If this is an occupied building, that reading is wrong - state the phase."
        return "warranty", True, f"ASSUMED warranty: {entities_found} of {n_spec} scheduled units are on the network with a spec still in hand. State the phase to override (operations if the warranty period is over)."
    if n_spec and not devices:
        return "new_construction", True, "ASSUMED new construction: a spec and nothing on the network. If devices exist, the CIDR scope may not cover them - absence is not evidence of nonexistence."
    return None, True, "phase could not be inferred: no spec and no devices"


def _merge_deviations(prior: list[Deviation], fresh: list[Deviation], now: str) -> list[Deviation]:
    """Deviations keep their id and open-date across passes when the same
    (kind, spec_tag, field_id) recurs; ones the field no longer shows are
    closed with the evidence that closed them; ones a person resolved are
    not reopened by the same evidence."""
    def key(d: Deviation) -> tuple:
        return (d.get("kind"), normalize_tag(d.get("spec_tag")), d.get("field_id"))
    by_key: dict[tuple, Deviation] = {}
    for d in prior:
        by_key.setdefault(key(d), d)
    out: list[Deviation] = []
    seen: set[tuple] = set()
    for d in fresh:
        k = key(d)
        seen.add(k)
        p = by_key.get(k)
        if p is None:
            out.append(d)
        elif p.get("status") == "resolved" and p.get("resolved_by") not in (None, "evidence"):
            out.append({**p, "field_shows": d["field_shows"], "entity_id": d.get("entity_id")})
        else:
            out.append({**d, "id": p["id"], "opened_at": p.get("opened_at") or d["opened_at"], "status": "open", "resolved_at": None, "resolved_by": None, "resolution": None})
    for k, p in by_key.items():
        if k in seen:
            continue
        if p.get("status") == "open":
            out.append({**p, "status": "resolved", "resolved_at": now, "resolved_by": "evidence", "resolution": "no longer observed on this pass - the field evidence that raised it has changed"})
        else:
            out.append(p)
    return out


def _merge_punch(prior: list[PunchItem], fresh: list[PunchItem]) -> list[PunchItem]:
    """Punch items are regenerated each pass from what is still open; an
    item a capture already resolved stays resolved, one for the same
    entity+reason keeps its id."""
    by_key = {(p.get("entity_id"), p.get("reason")): p for p in prior}
    resolved_entities = {(p.get("entity_id"), p.get("reason")) for p in prior if p.get("status") == "resolved"}
    out: list[PunchItem] = []
    for it in fresh:
        k = (it.get("entity_id"), it.get("reason"))
        if k in resolved_entities:
            out.append({**by_key[k]})
            continue
        if k in by_key:
            out.append({**it, "id": by_key[k]["id"]})
        else:
            out.append(it)
    return punchlist.order_by_route(out)


def escalations(entities: list[Entity], deviations: list[Deviation], spec: SpecModel | None) -> list[dict]:
    """Phase 6: one-action questions, batched. Each carries the accept and
    the correct action as ready-to-post corrections."""
    out: list[dict] = []
    open_devs = {d["id"]: d for d in deviations if d.get("status") == "open"}
    for e in entities:
        fid = (e.get("field_identity") or {}).get("field_id")
        if e.get("confidence") in ("medium", "low") and e.get("spec_tag") and fid:
            out.append({
                "kind": "mapping", "entity_id": e["id"], "spec_tag": e["spec_tag"], "field_id": fid, "confidence": e["confidence"],
                "question": f"Is {(e.get('field_identity') or {}).get('name') or fid} the controller for {e['spec_tag']}?",
                "why": "; ".join(b["detail"] for b in e.get("confidence_basis", []) if b.get("kind") in ("tag", "signature", "confidence_note")),
                "alternatives": e.get("alternatives", []),
                "accept": {"kind": "confirm_mapping", "spec_tag": e["spec_tag"], "field_id": fid},
                "correct": {"kind": "correct_mapping", "spec_tag": e["spec_tag"], "field_id": "<the right field_id>"},
                "reject": {"kind": "reject_mapping", "spec_tag": e["spec_tag"], "field_id": fid},
            })
        for did in e.get("deviations", []):
            d = open_devs.get(did)
            if not d:
                continue
            if d["kind"] == "type_mismatch":
                out.append({"kind": "type", "entity_id": e["id"], "spec_tag": e.get("spec_tag"), "field_id": fid, "deviation_id": did, "question": f"{e.get('spec_tag')}: spec says {e.get('canonical_type')}; the points say otherwise. Which is it?", "why": d["field_shows"], "alternatives": e.get("alternatives", []), "accept": {"kind": "resolve_deviation", "deviation_id": did, "note": "spec type is right"}, "correct": {"kind": "correct_type", "spec_tag": e.get("spec_tag"), "field_id": fid, "canonical_type": "<type>"}})
            elif d["kind"] == "duplicate_claim":
                out.append({"kind": "duplicate", "entity_id": e["id"], "spec_tag": e.get("spec_tag"), "field_id": fid, "deviation_id": did, "question": f"Two devices claim {e.get('spec_tag')} - which one is it?", "why": d["field_shows"], "alternatives": [], "accept": {"kind": "confirm_mapping", "spec_tag": e.get("spec_tag"), "field_id": fid}, "correct": {"kind": "correct_mapping", "spec_tag": e.get("spec_tag"), "field_id": "<the other field_id>"}})
            elif d["kind"] == "field_device_not_in_spec" and e.get("confidence") in ("medium", "low", "unmatched"):
                out.append({"kind": "unlisted_device", "entity_id": e["id"], "spec_tag": None, "field_id": fid, "deviation_id": did, "question": f"{(e.get('field_identity') or {}).get('name') or fid} is on the network but not in the spec. Proposed type: {e.get('canonical_type') or 'unknown'}. Keep it?", "why": d["field_shows"], "alternatives": e.get("alternatives", []), "accept": {"kind": "correct_type", "field_id": fid, "canonical_type": e.get("canonical_type") or "<type>"}, "correct": {"kind": "correct_mapping", "spec_tag": "<spec tag it really is>", "field_id": fid}, "ignore": {"kind": "resolve_deviation", "deviation_id": did, "note": "ignore this device"}})
    for esc in (spec or {}).get("escalate", []):
        out.append({"kind": "spec_entry", "spec_tag": esc["spec_tag"], "question": esc["question"], "why": f"spec entry scored {esc['score']:.2f}; flags {', '.join(esc['flags'])}", "alternatives": [], "accept": esc["one_action"]["accept"], "correct": esc["one_action"]["correct"]})
    return out


def _sync_faults(conn, project_id: str, rep: reconcile.ReconcileReport, now: datetime) -> dict:
    """Mirror this pass's faults into the shared `faults` table. The
    point_uri column carries the entity id - it is the stable identity
    here. Returns what was opened/closed."""
    if conn is None:
        return {"opened": [], "closed": [], "note": "no Postgres connection - faults reported here only, not mirrored to the faults table"}
    fault_store.ensure_schema(conn)
    opened: list[str] = []
    closed: list[str] = []
    wanted = {(f["rule_id"], f"cx:{project_id}:{f['entity_id']}") for f in rep.faults}
    for f in rep.faults:
        key = f"cx:{project_id}:{f['entity_id']}"
        since = reconcile._parse(f.get("since")) or now
        fid = fault_store.open_fault(conn, f["rule_id"], key, since, {"kind": f["kind"], "symptom": f["symptom"], "candidates": f["candidates"], "spec_tag": f.get("spec_tag"), "field_id": f.get("field_id"), "project_id": project_id}, f.get("severity", "warning"))
        if fid is not None:
            opened.append(key)
    for rule in (reconcile.FAULT_RULE_ABSENCE, reconcile.FAULT_RULE_LADDER):
        rows = conn.execute("SELECT point_uri FROM faults WHERE rule_id = %s AND ended_at IS NULL AND point_uri LIKE %s", (rule, f"cx:{project_id}:%")).fetchall()
        for (key,) in rows:
            if (rule, key) not in wanted:
                fault_store.close_fault(conn, rule, key, now)
                closed.append(key)
    return {"opened": opened, "closed": closed}


def run_pass(repo: Repo, project_id: str, store=None, history: History | None = None, now: datetime | None = None, trigger: str = "manual", project_to_graph: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    st = load_state(repo, project_id)
    if history is None and repo.conn is not None:
        history = PostgresHistory(repo.conn)

    # Discover: what the graph/history know, plus what was pushed in.
    graph_devices: list[DiscoveredDevice] = []
    discovery_notes: list[str] = []
    if store is not None:
        try:
            graph_devices = discovery.discover_from_store(store, history)
        except Exception as exc:  # noqa: BLE001 - the store being down must not kill the pass; it is reported
            discovery_notes.append(f"graph discovery failed ({exc.__class__.__name__}: {exc}) - this pass sees only pushed device data; absence this pass is NOT evidence")
    else:
        discovery_notes.append("no graph store configured - this pass sees only pushed device data")
    devices = discovery.merge_devices(graph_devices, st.devices)
    if not devices:
        discovery_notes.append("no devices at all this pass - nothing can be verified; if the connector is running, check its CIDR scope covers the controllers")

    spec = st.spec or spec_model.build_spec_model(st.spec_material)
    merged_captures = punchlist.merge_groups(punchlist.assign_groups(list(st.captures)))
    constraints = alignment.build_constraints(st.corrections, merged_captures, devices)
    provisional = alignment.align(spec, devices, st.project.get("phase"), constraints, st.entities, st.project.get("cidr_scopes", []))
    found = sum(1 for e in provisional.entities if e.get("spec_tag") and e.get("field_identity"))
    phase, assumed, rationale = infer_phase(st.project, spec, devices, found)
    if phase != st.project.get("phase") or assumed != st.project.get("phase_assumed"):
        st.project.update({"phase": phase, "phase_assumed": assumed, "phase_rationale": rationale, "updated_at": now.isoformat()})  # type: ignore[typeddict-item]
        provisional = alignment.align(spec, devices, phase, constraints, st.entities, st.project.get("cidr_scopes", []))
    result = provisional

    for e in result.entities:
        if e.get("field_identity") and history is not None:
            e["ladder"] = ladder.climb(e, history, now=now, config=st.project.get("settings", {}).get("ladder"))
        elif e.get("field_identity"):
            e["ladder"] = {"highest_rung_passed": 0, "rungs": [{"rung": 1, "name": ladder.RUNG_NAMES[1], "result": "insufficient_data", "evidence": ["device is bound but no point-history source is configured for this pass - existence cannot be judged from the device record alone"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": now.isoformat()}]}
        elif (e.get("liveness") or {}).get("state") == "not_networked":
            e["ladder"] = {"highest_rung_passed": 0, "rungs": [{"rung": 1, "name": ladder.RUNG_NAMES[1], "result": "not_applicable", "evidence": ["not networked by design - verification is a nameplate photo, not a rung"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": now.isoformat()}]}
        else:
            e["ladder"] = {"highest_rung_passed": 0, "rungs": [{"rung": 1, "name": ladder.RUNG_NAMES[1], "result": "fail", "evidence": ["no field device bound to this entity"], "symptom": "not discovered", "candidates": [], "fell_to_rung": None, "checked_at": now.isoformat()}]}

    rep = reconcile.reconcile(st.entities, result.entities, history, st.risks, now, st.project.get("settings", {}).get("reconcile"))
    deviations = _merge_deviations(st.deviations, result.deviations, now.isoformat())
    for e in rep.entities:
        # deviation ids were minted fresh in align; re-point to the merged ids
        keys = {(d.get("kind"), normalize_tag(d.get("spec_tag")), d.get("field_id")) for d in result.deviations if d.get("id") in e.get("deviations", [])}
        e["deviations"] = [d["id"] for d in deviations if (d.get("kind"), normalize_tag(d.get("spec_tag")), d.get("field_id")) in keys and d.get("status") == "open"]
    punch = _merge_punch(st.punch, punchlist.build_punchlist(rep.entities, deviations, phase, now))
    fault_sync = _sync_faults(repo.conn, project_id, rep, now)

    proj_result = None
    if project_to_graph and store is not None:
        proj_result = projection.project_brick(rep.entities)
        projection.write_brick(store, proj_result)

    pass_id = new_id("pass")
    report = build_report(pass_id, now, trigger, st.project, spec, result, rep, deviations, punch, st.risks, discovery_notes, fault_sync, proj_result, devices)

    repo.put_project(st.project)
    repo.upsert("cx_spec", project_id, {"id": "spec", "material": st.spec_material, "model": spec}, now)
    repo.replace_all("cx_entities", project_id, rep.entities, now)  # type: ignore[arg-type]
    repo.replace_all("cx_deviations", project_id, deviations, now)  # type: ignore[arg-type]
    repo.replace_all("cx_punch", project_id, punch, now)  # type: ignore[arg-type]
    repo.upsert("cx_passes", project_id, report, now)
    return report


def build_report(pass_id, now, trigger, project, spec, result, rep, deviations, punch, risks, notes, fault_sync, proj_result, devices) -> dict:
    ents = rep.entities
    open_devs = [d for d in deviations if d.get("status") == "open"]
    by_conf: dict[str, int] = {}
    for e in ents:
        by_conf[e.get("confidence") or "unmatched"] = by_conf.get(e.get("confidence") or "unmatched", 0) + 1
    questions = escalations(ents, deviations, spec)
    return {
        "id": pass_id,
        "pass": {"id": pass_id, "ran_at": now.isoformat(), "trigger": trigger, "project_id": project["id"], "phase": project.get("phase"), "phase_assumed": project.get("phase_assumed"), "phase_rationale": project.get("phase_rationale"), "cidr_scopes": project.get("cidr_scopes", []), "devices_seen": len(devices), "notes": notes},
        "spec_model": {"confidence": spec.get("confidence"), "equipment_count": len(spec.get("equipment", [])), "equipment": [{"tag": e["tag"], "type": e.get("type"), "type_as_written": e.get("type_as_written"), "points": len(e.get("expected_points", [])), "flags": e.get("flags", []), "confidence": e.get("confidence"), "source": e.get("source")} for e in spec.get("equipment", [])], "inconsistencies": spec.get("inconsistencies", []), "escalate": spec.get("escalate", []), "assumptions": spec.get("assumptions", [])},
        "mappings": result.mappings,
        "entities": ents,
        "ladder_results": [{"entity_id": e["id"], "spec_tag": e.get("spec_tag"), "field_id": (e.get("field_identity") or {}).get("field_id"), "confidence": e.get("confidence"), **(e.get("ladder") or {})} for e in ents],
        "deviations": open_devs,
        "deviations_resolved_this_pass": [d for d in deviations if d.get("status") == "resolved" and d.get("resolved_at") == now.isoformat()],
        "faults": rep.faults,
        "accepted_risks": {"risks": risks, "covered_this_pass": rep.risk_covered},
        "field_list": punchlist.group_by_location([p for p in punch if p.get("status") == "open"]),
        "unresolved_questions": questions,
        "changes": rep.changes,
        "freshness": rep.freshness,
        "assumptions": list(result.assumptions) + ([project["phase_rationale"]] if project.get("phase_assumed") and project.get("phase_rationale") else []),
        "summary": {"entities": len(ents), "by_confidence": by_conf, "open_deviations": len(open_devs), "faults": len(rep.faults), "risk_covered": len(rep.risk_covered), "open_punch_items": sum(1 for p in punch if p.get("status") == "open"), "unresolved_questions": len(questions)},
        "fault_sync": fault_sync,
        "projection": proj_result.as_dict() if proj_result else None,
    }


def apply_correction(repo: Repo, project_id: str, correction: dict, by: str, store=None, history: History | None = None, now: datetime | None = None) -> dict:
    """Record a correction, re-run the whole building, and report what
    moved - a correction to one entity often settles or unsettles others
    (an alias learned here, a duplicate freed there)."""
    now = now or datetime.now(timezone.utc)
    kind = correction.get("kind")
    if kind not in ("confirm_mapping", "correct_mapping", "reject_mapping", "correct_type", "confirm_topology", "naming_alias", "resolve_deviation", "accept_spec_entry", "correct_spec_entry"):
        raise ValueError(f"unknown correction kind {kind!r}")
    before = {e["id"]: e for e in repo.list_docs("cx_entities", project_id)}
    corr: Correction = {"id": new_id("corr"), "kind": kind, "spec_tag": correction.get("spec_tag"), "field_id": correction.get("field_id"), "entity_id": correction.get("entity_id"), "canonical_type": correction.get("canonical_type"), "relationship": correction.get("relationship"), "alias": correction.get("alias"), "by": by, "at": now.isoformat(), "note": correction.get("note")}  # type: ignore[typeddict-item]

    if kind == "resolve_deviation":
        if not correction.get("deviation_id"):
            raise ValueError("resolve_deviation needs deviation_id")
        d = repo.get("cx_deviations", project_id, correction["deviation_id"])
        if d is None:
            raise KeyError(f"deviation {correction['deviation_id']!r} not found")
        d.update({"status": "resolved", "resolved_at": now.isoformat(), "resolved_by": by, "resolution": correction.get("note") or "resolved by a person"})
        repo.upsert("cx_deviations", project_id, d, now)
    repo.upsert("cx_corrections", project_id, corr, now)  # type: ignore[arg-type]
    if kind == "correct_spec_entry" and correction.get("spec_tag"):
        row = repo.get("cx_spec", project_id, "spec") or {"id": "spec", "material": {}, "model": None}
        material = row.get("material") or {}
        for e in material.get("equipment", []):
            if normalize_tag(e.get("tag")) == normalize_tag(correction["spec_tag"]):
                for k in ("type", "points", "tag", "serves", "fed_by", "feeds", "location", "networked"):
                    if k in correction.get("fields", {}):
                        e[k] = correction["fields"][k]
        repo.upsert("cx_spec", project_id, {"id": "spec", "material": material, "model": spec_model.build_spec_model(material)}, now)

    report = run_pass(repo, project_id, store, history, now, trigger=f"correction:{kind}")
    after = {e["id"]: e for e in report["entities"]}
    moved: list[dict] = []
    for eid, e in after.items():
        b = before.get(eid)
        if b is None:
            moved.append({"entity_id": eid, "spec_tag": e.get("spec_tag"), "from": None, "to": e.get("confidence"), "why": "new this pass"})
        elif b.get("confidence") != e.get("confidence") or (b.get("field_identity") or {}).get("field_id") != (e.get("field_identity") or {}).get("field_id"):
            why = "; ".join(dict.fromkeys(x["detail"] for x in e.get("confidence_basis", []) if x.get("kind") in ("correction", "tag", "field_capture", "confidence_note")))[:300]
            moved.append({"entity_id": eid, "spec_tag": e.get("spec_tag"), "from": b.get("confidence"), "to": e.get("confidence"), "field_id_from": (b.get("field_identity") or {}).get("field_id"), "field_id_to": (e.get("field_identity") or {}).get("field_id"), "why": why})
    for eid, b in before.items():
        if eid not in after:
            moved.append({"entity_id": eid, "spec_tag": b.get("spec_tag"), "from": b.get("confidence"), "to": None, "why": "no longer produced"})
    report["correction"] = corr
    report["confidence_movement"] = moved
    report["correction_effect"] = (f"{len(moved)} entit{'y' if len(moved) == 1 else 'ies'} moved after this correction: " + "; ".join(f"{m.get('spec_tag') or m['entity_id']} {m['from']} -> {m['to']}" for m in moved[:12])) if moved else "no confidence moved beyond the corrected entity itself"
    return report


def ingest_capture(repo: Repo, project_id: str, raw: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    items = repo.list_docs("cx_punch", project_id)
    entities = repo.list_docs("cx_entities", project_id)
    cap, resolved = punchlist.ingest_capture(raw, items, entities, now)  # type: ignore[arg-type]
    prior = repo.list_docs("cx_captures", project_id)
    grouped = punchlist.assign_groups([*prior, cap])  # type: ignore[list-item]
    repo.upsert_many("cx_captures", project_id, grouped, now)  # type: ignore[arg-type]
    for it in resolved:
        repo.upsert("cx_punch", project_id, it, now)  # type: ignore[arg-type]
    return {"capture": cap, "resolved_punch_items": [it["id"] for it in resolved], "open_punch_items": sum(1 for it in items if it.get("status") == "open")}
