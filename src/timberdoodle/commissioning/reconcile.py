"""
Phase 5 + Phase 8 - what changed since the last pass, what is alive, and
what of it is a fault as opposed to a risk someone has signed for.

Every entity coming out of alignment is classified against its prior self:

  unchanged     same field identity, same object list
  absent        was observed before, not observed this pass. Not dropped:
                a device stays `missing` for a grace period (passes *and*
                wall-clock) before it is called `absent`, because a
                controller rebooting during a scan is not a removal.
  returned      was absent, is observed again. Its object list is compared
                to the one it had - the same address answering with a
                different signature is *new hardware at an old address*,
                and says so.
  new           never observed before this pass.
  drifted       same identity, different object list. The highest-value
                signal there is: a program change, a point re-mapped, a
                sensor removed. Reported in full, never silently absorbed.
  re_addressed  same spec tag and signature, different BACnet address or
                instance. Common after a controller swap.

Liveness is judged from data, not from the graph's memory: a device whose
equipment node still sits in Oxigraph but whose points have not written a
sample in `absent_after_seconds` is not present, it is remembered.

Faults and accepted risks are kept in different lists on purpose. A
sustained absence or a ladder failure on a well-identified entity is a
fault *unless* an accepted risk covers that entity, in which case it is
reported under the risk, with the risk's owner, and never in the fault
list. Neither list is ever merged into the other.

Freshness is reported the way a person would want to hear it: "as of
<when>, N of M networked entities fresh (< X min), K stale, J known
absent, L never seen" - never "all good" when what is meant is "nothing
new came in".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from timberdoodle.commissioning.history import History
from timberdoodle.commissioning.model import AcceptedRisk, Entity, Liveness, new_id

DEFAULTS = {
    "absent_after_seconds": 6 * 3600,     # no sample/no sighting this long -> counts as a miss
    "absent_grace_passes": 2,             # misses before "missing" becomes "absent"
    "absent_grace_seconds": 12 * 3600,    # ...and at least this long since last observed
    "sustained_absence_seconds": 48 * 3600,  # absent this long -> fault (unless a risk covers it)
    "fresh_seconds": 3600,                # observed within this -> "fresh"
}

FAULT_RULE_ABSENCE = "commissioning:sustained-absence"
FAULT_RULE_LADDER = "commissioning:ladder-failure"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def signature_hash(entity: Entity) -> str | None:
    """Fingerprint of the object list actually observed on the device -
    identifiers, names and what they classify as. Spec-only rows
    (`specified_absent`) are not part of what the *field* shows and are
    left out, so the hash moves only when the device does."""
    parts = sorted(
        f"{p.get('bacnet_object') or ''}|{p.get('name') or ''}|{p.get('function') or ''}|{p.get('role') or ''}|{p.get('units') or ''}"
        for p in entity.get("points", [])
        if p.get("status") in ("matched", "unspecified_present", "unreadable")
    )
    if not parts:
        return None
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


def _signature_diff(prior: Entity, current: Entity) -> dict:
    def keyed(e: Entity) -> dict[str, dict]:
        return {f"{p.get('bacnet_object') or p.get('name')}": p for p in e.get("points", []) if p.get("status") in ("matched", "unspecified_present", "unreadable")}
    a, b = keyed(prior), keyed(current)

    def describe(k: str, p: dict) -> str:
        name = f" '{p['name']}'" if p.get("name") and p["name"] != k else ""
        return f"{k}{name} ({p.get('function') or 'unreadable'} {p.get('role') or ''})".strip()

    added = [describe(k, b[k]) for k in b if k not in a]
    removed = [describe(k, a[k]) for k in a if k not in b]
    changed = []
    for k in a.keys() & b.keys():
        for fld in ("name", "function", "role", "units"):
            if a[k].get(fld) != b[k].get(fld):
                changed.append(f"{k}: {fld} {a[k].get(fld)!r} -> {b[k].get(fld)!r}")
    return {"added": added, "removed": removed, "changed": changed}


def last_observation(entity: Entity, history: History | None) -> datetime | None:
    """The most recent moment anything about this entity was seen: a
    sample on any of its points, or the discovery layer's own last-seen
    stamp. History wins when it is newer - it is the one that proves the
    device is *talking*, not merely remembered."""
    best = _parse((entity.get("provenance") or {}).get("last_observed"))
    if history is not None:
        uris = [p["point_uri"] for p in entity.get("points", []) if p.get("point_uri") and p.get("status") in ("matched", "unspecified_present")]
        if uris:
            for ts, _ in history.latest(uris).values():
                if best is None or ts > best:
                    best = ts
    return best


@dataclass
class ReconcileReport:
    entities: list[Entity]
    changes: dict[str, list[dict]] = field(default_factory=dict)  # state -> [{entity_id, spec_tag, field_id, detail}]
    faults: list[dict] = field(default_factory=list)
    risk_covered: list[dict] = field(default_factory=list)  # would-be faults suppressed by an accepted risk
    freshness: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"changes": self.changes, "faults": self.faults, "risk_covered": self.risk_covered, "freshness": self.freshness}


def reconcile(
    prior: list[Entity],
    current: list[Entity],
    history: History | None,
    risks: list[AcceptedRisk],
    now: datetime | None = None,
    config: dict | None = None,
) -> ReconcileReport:
    now = now or datetime.now(timezone.utc)
    cfg = {**DEFAULTS, **(config or {})}
    prior_by_id = {e["id"]: e for e in prior}
    changes: dict[str, list[dict]] = {k: [] for k in ("unchanged", "absent", "missing", "returned", "new", "drifted", "re_addressed")}
    out: list[Entity] = []

    for ent in current:
        p = prior_by_id.get(ent["id"])
        prior_live: Liveness = dict((p or {}).get("liveness") or {})  # type: ignore[assignment]
        live: Liveness = dict(ent.get("liveness") or {})  # type: ignore[assignment]
        fid = (ent.get("field_identity") or {}).get("field_id")
        observed = last_observation(ent, history) if fid else None
        stale = (now - observed).total_seconds() if observed else None
        seen_now = fid is not None and stale is not None and stale <= cfg["absent_after_seconds"]
        sig = signature_hash(ent) if fid else None
        detail: dict = {"spec_tag": ent.get("spec_tag"), "field_id": fid, "entity_id": ent["id"]}

        if live.get("state") == "not_networked":
            live.update({"last_observed": None, "staleness_seconds": None})
            ent["reconcile_state"], ent["reconcile_detail"] = None, None
            out.append({**ent, "liveness": live})
            continue

        if seen_now:
            was_present = prior_live.get("state") in ("present", "missing")
            was_absent = prior_live.get("state") == "absent"
            prev_sig = prior_live.get("signature_hash")
            prev_fid = prior_live.get("last_field_id") or ((p or {}).get("field_identity") or {}).get("field_id")
            live.update({"state": "present", "absent_since": None, "misses": 0, "last_observed": observed.isoformat() if observed else None, "staleness_seconds": round(stale, 1) if stale is not None else None, "signature_hash": sig, "last_field_id": fid})
            if p is None or prior_live.get("state") in (None, "never_seen"):
                state = "new"
                detail["detail"] = f"first observed this pass ({len([x for x in ent.get('points', []) if x.get('status') != 'specified_absent'])} objects)"
            elif was_absent:
                state = "returned"
                if prev_sig and sig and prev_sig != sig:
                    diff = _signature_diff(p, ent) if p else {}
                    detail["detail"] = "back online, but with a DIFFERENT object list than before it went absent - likely new hardware or a re-programmed controller at the old address, not the same device coming back"
                    detail["signature"] = diff
                    detail["verify"] = "treat as new until the nameplate/controller label is captured at this location"
                else:
                    detail["detail"] = f"back online with the same signature it had before (absent since {prior_live.get('absent_since')})"
            elif was_present and prev_fid and fid and prev_fid != fid and (not prev_sig or prev_sig == sig):
                state = "re_addressed"
                detail["detail"] = f"same equipment ({ent.get('spec_tag')}), same object list, now answering at {fid} instead of {prev_fid} - controller swap or readdress"
                detail["previous_field_id"] = prev_fid
            elif was_present and prev_sig and sig and prev_sig != sig:
                state = "drifted"
                diff = _signature_diff(p, ent) if p else {}
                detail["signature"] = diff
                detail["detail"] = f"object list changed: {len(diff.get('added', []))} added, {len(diff.get('removed', []))} removed, {len(diff.get('changed', []))} renamed/reclassified - a program change, point re-map, or removed sensor; the highest-value signal there is, review before trusting any analytic on this unit"
            else:
                state = "unchanged"
                detail["detail"] = "same identity, same object list"
        else:
            misses = int(prior_live.get("misses") or 0) + 1 if prior_live.get("state") in ("present", "missing", "absent") else 0
            last_obs = prior_live.get("last_observed") or (observed.isoformat() if observed else None)
            absent_since = prior_live.get("absent_since") or (now.isoformat() if prior_live.get("state") in ("present", "missing") else None)
            last_obs_dt = _parse(last_obs)
            long_enough = last_obs_dt is None or (now - last_obs_dt).total_seconds() >= cfg["absent_grace_seconds"]
            if prior_live.get("state") in ("present", "missing", "absent"):
                if prior_live.get("state") == "absent" or (misses >= cfg["absent_grace_passes"] and long_enough):
                    new_state = "absent"
                else:
                    new_state = "missing"
                live.update({"state": new_state, "absent_since": absent_since, "misses": misses, "last_observed": last_obs, "staleness_seconds": round((now - last_obs_dt).total_seconds(), 1) if last_obs_dt else None, "signature_hash": prior_live.get("signature_hash"), "last_field_id": prior_live.get("last_field_id") or ((p or {}).get("field_identity") or {}).get("field_id")})
                state = new_state
                if new_state == "missing":
                    detail["detail"] = f"not observed this pass (miss {misses} of {cfg['absent_grace_passes']}, last seen {last_obs}) - held as missing, not yet absent; a reboot during the scan looks exactly like this"
                else:
                    detail["detail"] = f"not observed since {absent_since} ({misses} consecutive passes) - absent. What absence means depends on the engagement phase; the spec_device_not_found deviation on this entity carries that reading"
                if fid and stale is not None:
                    detail["detail"] += f"; the graph still lists it at {fid} but its points last wrote {stale / 3600:.1f} h ago"
            else:
                live.update({"state": "never_seen", "absent_since": None, "misses": 0, "last_observed": None, "staleness_seconds": None, "signature_hash": None, "last_field_id": None})
                state = None  # type: ignore[assignment]

        ent = {**ent, "liveness": live}
        if state is not None:
            ent["reconcile_state"] = state
            ent["reconcile_detail"] = detail
            changes[state].append(detail)
        else:
            ent["reconcile_state"], ent["reconcile_detail"] = None, None
        out.append(ent)

    # Entities that vanished from the alignment entirely (a spec row deleted,
    # a field device gone AND no spec row to hang it on) are carried forward
    # as absent so nothing is dropped on a miss.
    current_ids = {e["id"] for e in current}
    for p in prior:
        if p["id"] in current_ids or (p.get("liveness") or {}).get("state") in ("never_seen", "not_networked", None):
            continue
        pl: Liveness = dict(p.get("liveness") or {})  # type: ignore[assignment]
        misses = int(pl.get("misses") or 0) + 1
        absent_since = pl.get("absent_since") or now.isoformat()
        st = "absent" if pl.get("state") == "absent" or misses >= cfg["absent_grace_passes"] else "missing"
        pl.update({"state": st, "misses": misses, "absent_since": absent_since})
        carried = {**p, "liveness": pl, "reconcile_state": st, "reconcile_detail": {"entity_id": p["id"], "spec_tag": p.get("spec_tag"), "field_id": (p.get("field_identity") or {}).get("field_id"), "detail": f"no longer produced by alignment (spec row removed or device gone with no spec anchor) - carried forward as {st}, not deleted"}, "updated_at": now.isoformat()}
        changes[st].append(carried["reconcile_detail"])  # type: ignore[arg-type]
        out.append(carried)  # type: ignore[arg-type]

    faults, covered = classify_faults(out, risks, now, cfg)
    for e in out:
        e["faults"] = [f["id"] for f in faults if f["entity_id"] == e["id"]]
        e["accepted_risks"] = [c["risk_id"] for c in covered if c["entity_id"] == e["id"]]
    return ReconcileReport(out, changes, faults, covered, freshness(out, now, cfg))


def _risk_for(entity: Entity, risks: list[AcceptedRisk]) -> AcceptedRisk | None:
    fid = (entity.get("field_identity") or {}).get("field_id")
    for r in risks:
        if r.get("entity_id") and r["entity_id"] == entity["id"]:
            return r
        if r.get("field_id") and fid and r["field_id"] == fid:
            return r
        if r.get("spec_tag") and entity.get("spec_tag") and r["spec_tag"].strip().upper() == entity["spec_tag"].strip().upper():
            return r
    return None


def classify_faults(entities: list[Entity], risks: list[AcceptedRisk], now: datetime, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Faults: things wrong with equipment we are confident about. Risks:
    things someone has decided to live with. A fault covered by a risk is
    reported under the risk, never in the fault list - and the risk list
    never quietly grows a fault."""
    faults: list[dict] = []
    covered: list[dict] = []

    def emit(entity: Entity, kind: str, rule: str, symptom: str, candidates: list[dict], since: str | None, severity: str) -> None:
        risk = _risk_for(entity, risks)
        fault_key = f"{entity['id']}|{kind}"
        rec = {"id": "flt_" + hashlib.sha1(fault_key.encode()).hexdigest()[:12], "kind": kind, "rule_id": rule, "entity_id": entity["id"], "spec_tag": entity.get("spec_tag"), "field_id": (entity.get("field_identity") or {}).get("field_id"), "symptom": symptom, "candidates": candidates, "since": since, "severity": severity, "confidence_of_identity": entity.get("confidence")}
        if risk is not None:
            covered.append({**rec, "risk_id": risk["id"], "risk": risk.get("description"), "accepted_by": risk.get("accepted_by"), "review_by": risk.get("review_by"), "note": "would be a fault; suppressed because an accepted risk covers this entity"})
        else:
            faults.append(rec)

    for e in entities:
        live = e.get("liveness") or {}
        if e.get("confidence") not in ("confirmed", "high"):
            # A symptom on a device we are not sure *is* this equipment is a
            # deviation to settle first, not a fault on the equipment.
            continue
        if live.get("state") == "absent" and live.get("absent_since"):
            since = _parse(live["absent_since"])
            if since and (now - since).total_seconds() >= cfg["sustained_absence_seconds"]:
                emit(e, "sustained_absence", FAULT_RULE_ABSENCE,
                     f"{e.get('spec_tag') or e['id']} has not been observed since {live['absent_since']} ({(now - since).total_seconds() / 3600:.0f} h)",
                     [{"cause": "controller powered off or failed", "would_distinguish": "controller status LED / local display at the enclosure"},
                      {"cause": "network path lost (switch port, MS/TP trunk, router readdress)", "would_distinguish": "ping/Who-Is from the same segment; check whether neighbours on the same trunk also went absent at the same time"},
                      {"cause": "re-addressed and now sitting under a different identity", "would_distinguish": "look for a 'new' entity this pass with the same object signature"}],
                     live["absent_since"], "warning")
        ladder = e.get("ladder") or {}
        for r in ladder.get("rungs", []):
            if r.get("result") == "fail" and r.get("rung") in (2, 3) and live.get("state") == "present":
                emit(e, f"rung{r['rung']}_failure", FAULT_RULE_LADDER, r.get("symptom") or f"rung {r['rung']} ({r.get('name')}) failed", list(r.get("candidates", [])), r.get("checked_at"), "warning" if r["rung"] == 2 else "info")
                break
    return faults, covered


def freshness(entities: list[Entity], now: datetime, cfg: dict) -> dict:
    buckets: dict[str, list[str]] = {"fresh": [], "stale": [], "missing": [], "known_absent": [], "never_seen": [], "not_networked": []}
    oldest_fresh: datetime | None = None
    for e in entities:
        live = e.get("liveness") or {}
        label = e.get("spec_tag") or (e.get("field_identity") or {}).get("field_id") or e["id"]
        st = live.get("state")
        if st == "not_networked":
            buckets["not_networked"].append(label)
        elif st == "never_seen" or st is None:
            buckets["never_seen"].append(label)
        elif st == "absent":
            buckets["known_absent"].append(label)
        elif st == "missing":
            buckets["missing"].append(label)
        else:
            obs = _parse(live.get("last_observed"))
            if obs and (now - obs).total_seconds() <= cfg["fresh_seconds"]:
                buckets["fresh"].append(label)
                oldest_fresh = obs if oldest_fresh is None or obs < oldest_fresh else oldest_fresh
            else:
                buckets["stale"].append(label)
    networked = sum(len(v) for k, v in buckets.items() if k != "not_networked")
    pct = (100.0 * len(buckets["fresh"]) / networked) if networked else 0.0
    return {
        "as_of": now.isoformat(),
        "fresh_window_seconds": cfg["fresh_seconds"],
        "networked_entities": networked,
        "counts": {k: len(v) for k, v in buckets.items()},
        "percent_fresh": round(pct, 1),
        "entities": buckets,
        "statement": (
            f"as of {now.strftime('%Y-%m-%d %H:%M UTC')}: {len(buckets['fresh'])} of {networked} networked entities fresh (a sample within {cfg['fresh_seconds'] // 60} min), "
            f"{len(buckets['stale'])} stale, {len(buckets['missing'])} missing (grace period), {len(buckets['known_absent'])} known absent, {len(buckets['never_seen'])} never seen"
            + (f"; {len(buckets['not_networked'])} not networked by design" if buckets["not_networked"] else "")
            + ("" if networked else " - nothing has been observed at all; this is not a clean bill of health, it is an empty one")
        ),
    }


def new_risk(entity_id: str | None, field_id: str | None, spec_tag: str | None, description: str, accepted_by: str, review_by: str | None = None, now: datetime | None = None) -> AcceptedRisk:
    now = now or datetime.now(timezone.utc)
    return {"id": new_id("risk"), "entity_id": entity_id, "field_id": field_id, "spec_tag": spec_tag, "description": description, "accepted_by": accepted_by, "accepted_at": now.isoformat(), "review_by": review_by or (now + timedelta(days=90)).date().isoformat()}
