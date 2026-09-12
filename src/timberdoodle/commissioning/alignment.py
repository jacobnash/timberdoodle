"""
Phase 2 (align discovered devices to the spec), Phase 4 (every place they
disagree is a deviation, surfaced, never auto-resolved) and the
constraint side of Phase 6 (human corrections and field captures are
evidence that re-shapes every subsequent alignment).

Evidence is weighed strongest to weakest, exactly as an integrator does:

  1. tag match          - the name carries the spec tag, allowing for
                          predictable delimiter/abbreviation variation
  2. point signature    - the object list matches what that type of
                          equipment exposes, whatever it calls itself
  3. plausibility       - a 55 degF "supply air temperature" is one; a
                          210 degF one is not
  4. topology           - object names referencing an upstream unit
                          constrain what the device can be
  5. vendor convention  - naming hints only

Confidence is evidence, not optimism: a clean tag match on a device whose
points are wrong for its claimed type is a *type mismatch deviation*, not
a high-confidence mapping. Anything below High names its competing
interpretations specifically and says what would settle them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.model import (
    Alternative,
    Correction,
    Deviation,
    DiscoveredDevice,
    Entity,
    EntityPoint,
    EvidenceItem,
    FieldCapture,
    Mapping,
    Phase,
    SpecEquipment,
    SpecModel,
    new_id,
    now_iso,
)
from timberdoodle.commissioning.points import (
    ClassifiedPoint,
    classify_object,
    label,
    tokenize,
)
from timberdoodle.commissioning.spec_model import (
    infer_type_from_tag,
    normalize_tag,
    tag_letters,
    tag_number,
)

# Mappings at or above this composite score are candidates at all; the
# confidence *word* is decided by rules on the individual evidence, not by
# this number alone.
MIN_CANDIDATE_SCORE = 0.25

AIR_SOURCE_TYPES = {"air handling unit", "rooftop unit", "packaged heat pump"}


# --- constraints from corrections and captures ------------------------------------

@dataclass
class Constraints:
    confirmed: dict[str, str] = field(default_factory=dict)  # norm spec tag -> field_id
    rejected: set[tuple[str, str]] = field(default_factory=set)  # (norm spec tag, field_id)
    type_overrides: dict[str, str] = field(default_factory=dict)  # norm spec tag or field_id -> canonical type
    topology: list[tuple[str, str, str]] = field(default_factory=list)  # (norm from, kind, norm to)
    aliases: dict[str, str] = field(default_factory=dict)  # field letters -> spec letters ("AH" -> "AHU")
    capture_support: dict[str, list[dict]] = field(default_factory=dict)  # field_id -> [{spec_tag, capture_id, why}]
    nameplate_only: dict[str, list[dict]] = field(default_factory=dict)  # norm spec tag -> captures proving physical existence w/o network
    field_locations: dict[str, dict] = field(default_factory=dict)  # field_id -> location from captures

    def effective_type(self, spec: SpecEquipment | None, field_id: str | None) -> str | None:
        if spec is not None and normalize_tag(spec["tag"]) in self.type_overrides:
            return self.type_overrides[normalize_tag(spec["tag"])]
        if field_id and field_id in self.type_overrides:
            return self.type_overrides[field_id]
        return spec.get("type") if spec else None


def _learn_alias(spec_tag: str, field_name: str | None) -> tuple[str, str] | None:
    """A confirmed AHU-3 <-> "AH3" teaches that this building writes AH for
    AHU - only when the numbers agree, so the lesson is about letters."""
    if not field_name:
        return None
    for tok in _name_candidates(field_name):
        if tag_number(tok) and tag_number(tok) == tag_number(spec_tag) and tag_letters(tok) and tag_letters(tok) != tag_letters(spec_tag):
            return tag_letters(tok), tag_letters(spec_tag)
    return None


def build_constraints(corrections: list[Correction], captures: list[FieldCapture], devices: list[DiscoveredDevice]) -> Constraints:
    c = Constraints()
    by_field = {d["field_id"]: d for d in devices}
    for corr in corrections:
        kind = corr.get("kind")
        norm = normalize_tag(corr.get("spec_tag"))
        fid = corr.get("field_id")
        if kind in ("confirm_mapping", "correct_mapping") and norm and fid:
            c.confirmed[norm] = fid
            learned = _learn_alias(corr["spec_tag"] or "", (by_field.get(fid) or {}).get("name"))
            if learned:
                c.aliases[learned[0]] = learned[1]
        elif kind == "reject_mapping" and norm and fid:
            c.rejected.add((norm, fid))
        elif kind == "correct_type" and corr.get("canonical_type"):
            ctype = V.canonical_type(corr["canonical_type"]) or corr["canonical_type"]
            if norm:
                c.type_overrides[norm] = ctype
            if fid:
                c.type_overrides[fid] = ctype
        elif kind == "confirm_topology" and corr.get("relationship"):
            rel = corr["relationship"] or {}
            c.topology.append((normalize_tag(rel.get("from")), str(rel.get("kind") or "feeds"), normalize_tag(rel.get("to"))))
        elif kind == "naming_alias" and corr.get("alias"):
            al = corr["alias"] or {}
            if al.get("field_token") and al.get("spec_token"):
                c.aliases[str(al["field_token"]).upper()] = str(al["spec_token"]).upper()

    for cap in captures:
        legible = cap.get("legible") or {}
        tag = legible.get("tag") or (legible.get("nameplate") or {}).get("tag") or legible.get("controller_name")
        ip = legible.get("ip") or legible.get("address")
        norm = normalize_tag(tag) if tag else ""
        matched_fid = None
        if ip:
            for d in devices:
                if d.get("address") and str(d["address"]).split(":")[0] == str(ip).split(":")[0]:
                    matched_fid = d["field_id"]
                    break
        if matched_fid is None and legible.get("controller_name"):
            cn = normalize_tag(legible["controller_name"])
            for d in devices:
                if cn and normalize_tag(d.get("name")) == cn:
                    matched_fid = d["field_id"]
                    break
        if matched_fid and norm:
            c.capture_support.setdefault(matched_fid, []).append({"spec_tag": tag, "capture_id": cap.get("id"), "why": f"capture {cap.get('id')} shows {'IP ' + str(ip) if ip else 'controller name'} together with tag/name '{tag}'"})
        if matched_fid and cap.get("location"):
            c.field_locations[matched_fid] = cap["location"]  # type: ignore[assignment]
        if norm and (legible.get("nameplate") or legible.get("tag")):
            c.nameplate_only.setdefault(norm, []).append({"capture_id": cap.get("id"), "location": cap.get("location"), "nameplate": legible.get("nameplate") or {"tag": tag}})
    return c


# --- evidence -----------------------------------------------------------------------

def _name_candidates(name: str | None) -> list[str]:
    """Substrings of a device/object name that look like equipment tags:
    `Bldg2_AHU-3_Controller` -> ['AHU-3', 'Bldg2', ...]."""
    if not name:
        return []
    return re.findall(r"[A-Za-z]{1,6}[-_ ]?\d+[A-Za-z]?(?:[-_]\d+)?", str(name))


def tag_evidence(spec: SpecEquipment, device: DiscoveredDevice, classified: list[ClassifiedPoint], aliases: dict[str, str]) -> EvidenceItem:
    spec_norm = normalize_tag(spec["tag"])
    spec_letters, spec_num = tag_letters(spec["tag"]), tag_number(spec["tag"])
    type_aliases = {a.upper().rstrip("-") for a in V.EQUIPMENT_TYPES.get(spec.get("type") or "", {}).get("aliases", [])}
    type_aliases.add(spec_letters)
    for f_tok, s_tok in aliases.items():
        if s_tok == spec_letters:
            type_aliases.add(f_tok)

    def score_name(name: str | None) -> tuple[float, str]:
        if not name:
            return 0.0, ""
        norm = normalize_tag(name)
        if norm == spec_norm:
            return 1.0, f"name '{name}' is the spec tag"
        if spec_norm and spec_norm in norm:
            return 0.9, f"name '{name}' contains the spec tag"
        conflict = ""
        for cand in _name_candidates(name):
            if normalize_tag(cand) == spec_norm:
                return 0.9, f"name '{name}' contains '{cand}' = spec tag"
            letters, num = tag_letters(cand), tag_number(cand)
            if spec_num and num == spec_num:
                if letters in type_aliases:
                    how = "learned alias" if letters in aliases else "known abbreviation"
                    return 0.8, f"name '{name}' has '{cand}' - {letters} is a {how} for {spec_letters}, same number {spec_num}"
                if letters and spec_letters.startswith(letters):
                    return 0.6, f"name '{name}' has '{cand}' - {letters} could abbreviate {spec_letters}, same number {spec_num}"
            elif spec_num and num and letters in type_aliases and not conflict:
                # Same kind of equipment, *different* number: the name says
                # this is some other unit. Evidence against, not merely absent.
                conflict = f"name '{name}' carries '{cand}' (number {num}), not {spec['tag']}'s number {spec_num} - by its own name this is a different {spec_letters}"
        if conflict:
            return -0.2, conflict
        return 0.0, ""

    best_score, best_detail = score_name(device.get("name"))
    if best_score < 0.9 and device.get("description"):
        s, d = score_name(device["description"])
        if s > best_score and s > 0:
            best_score, best_detail = s, d
    # A tag carried by most object names ("AHU3_SAT", "AHU3_RAT", ...) is as
    # good as a device name, common when the device object is named for the
    # controller hardware rather than the equipment.
    if classified:
        hits = [score_name(p.get("name"))[0] for p in classified]
        share = sum(1 for h in hits if h >= 0.8) / len(hits)
        if share >= 0.5 and max(hits) > best_score:
            best_score, best_detail = min(max(hits), 0.9), f"{share:.0%} of object names carry the spec tag"
    return {"rung": 1, "kind": "tag", "score": round(best_score, 2), "detail": best_detail or "no form of the spec tag appears in the device or object names"}


@dataclass
class SignatureFit:
    type: str | None
    required_coverage: float = 0.0
    typical_coverage: float = 0.0
    distinctive_hits: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    score: float = 0.0


def _has(points: list[ClassifiedPoint], function: str, role: str) -> bool:
    return any(p.get("function") == function and (role == "*" or p.get("role") == role) for p in points)


def signature_fit(ctype: str | None, points: list[ClassifiedPoint]) -> SignatureFit:
    """How well a device's classified points fit a canonical type's
    signature. Contradictions (a reversing valve on a claimed AHU) are
    listed by name because they are what tells look-alikes apart."""
    fit = SignatureFit(type=ctype)
    if ctype is None or ctype not in V.EQUIPMENT_TYPES:
        return fit
    t = V.EQUIPMENT_TYPES[ctype]
    required = t.get("required", [])
    typical = t.get("typical", [])
    req_hits = [f"{fn} {'' if role == '*' else role}".strip() for fn, role in required if _has(points, fn, role)]
    fit.missing_required = [f"{fn} {'' if role == '*' else role}".strip() for fn, role in required if not _has(points, fn, role)]
    fit.required_coverage = len(req_hits) / len(required) if required else 1.0
    typ_hits = [fn for fn, role in typical if _has(points, fn, role)]
    fit.typical_coverage = len(typ_hits) / len(typical) if typical else 0.0
    fit.distinctive_hits = [fn for fn, role in t.get("distinctive", []) if _has(points, fn, role)]
    fit.contradictions = [f"{fn} {'' if role == '*' else role}".strip() for fn, role in t.get("contradicting", []) if _has(points, fn, role)]
    score = 0.6 * fit.required_coverage + 0.25 * fit.typical_coverage + (0.15 if fit.distinctive_hits else 0.0)
    score -= 0.3 * len(fit.contradictions)
    fit.score = round(max(0.0, min(1.0, score)), 2)
    return fit


def rank_types(points: list[ClassifiedPoint]) -> list[SignatureFit]:
    fits = [signature_fit(t, points) for t in V.EQUIPMENT_TYPES]
    return sorted(fits, key=lambda f: (-f.score, -f.required_coverage, f.type or ""))


def match_points(spec: SpecEquipment, classified: list[ClassifiedPoint]) -> tuple[list[tuple[dict, ClassifiedPoint]], list[dict], list[ClassifiedPoint]]:
    """Spec expected points <-> field points: by (function, role), then by
    normalized name. Returns (matched pairs, specified-but-absent,
    present-but-unspecified). Never invents a field point."""
    unused = list(classified)
    matched: list[tuple[dict, ClassifiedPoint]] = []
    absent: list[dict] = []
    for sp in spec.get("expected_points", []):
        hit = None
        if sp.get("function"):
            hit = next((p for p in unused if p.get("function") == sp["function"] and (not sp.get("role") or p.get("role") == sp["role"])), None)
            if hit is None:
                hit = next((p for p in unused if p.get("function") == sp["function"]), None)
        if hit is None and sp.get("name"):
            want = normalize_tag(sp["name"])
            hit = next((p for p in unused if want and normalize_tag(p.get("name")) == want), None)
        if hit is None:
            absent.append(dict(sp))
        else:
            unused.remove(hit)
            matched.append((dict(sp), hit))
    return matched, absent, unused


def topology_evidence(spec: SpecEquipment, classified: list[ClassifiedPoint], device: DiscoveredDevice, spec_norms: dict[str, SpecEquipment], constraints: Constraints) -> EvidenceItem:
    """Object names that reference another spec tag (a VAV's "AHU-3 SAT")
    constrain what this device can be: they should name what feeds it."""
    own = normalize_tag(spec["tag"])
    fed_by = {normalize_tag(x) for x in spec.get("fed_by", [])}
    for frm, kind, to in constraints.topology:
        if kind == "feeds" and to == own:
            fed_by.add(frm)
    referenced: dict[str, int] = {}
    for p in classified:
        for cand in _name_candidates(p.get("name")):
            n = normalize_tag(cand)
            if n in spec_norms and n != own:
                referenced[n] = referenced.get(n, 0) + 1
    if not referenced:
        return {"rung": 4, "kind": "topology", "score": 0.0, "detail": "object names reference no other spec equipment"}
    supporting = [n for n in referenced if n in fed_by]
    others = [n for n in referenced if n not in fed_by and (constraints.effective_type(spec_norms[n], None) in AIR_SOURCE_TYPES or not V.EQUIPMENT_TYPES.get(constraints.effective_type(spec_norms[n], None) or "", {}).get("terminal", False))]
    if supporting:
        return {"rung": 4, "kind": "topology", "score": 0.15, "detail": f"object names reference {', '.join(spec_norms[n]['tag'] for n in supporting)}, which the spec says feeds {spec['tag']}"}
    if others:
        return {"rung": 4, "kind": "topology", "score": -0.2, "detail": f"object names reference {', '.join(spec_norms[n]['tag'] for n in others)}, but the spec says {spec['tag']} is fed by {', '.join(spec.get('fed_by', [])) or 'nothing listed'}"}
    return {"rung": 4, "kind": "topology", "score": 0.0, "detail": f"object names reference {', '.join(spec_norms[n]['tag'] for n in referenced)} (terminal units - no constraint)"}


def vendor_evidence(device: DiscoveredDevice) -> EvidenceItem:
    info = V.vendor_info(device.get("vendor_id"))
    if info is None:
        return {"rung": 5, "kind": "vendor", "score": 0.0, "detail": f"vendor id {device.get('vendor_id')} not in the known-vendor table" if device.get("vendor_id") is not None else "no vendor id"}
    return {"rung": 5, "kind": "vendor", "score": 0.0, "detail": f"{info['name']}: {info['note']}"}


# --- gateway devices ----------------------------------------------------------------

def split_gateway_device(device: DiscoveredDevice, spec_norms: dict[str, SpecEquipment]) -> list[tuple[DiscoveredDevice, list[ClassifiedPoint]]]:
    """One BACnet device fronting several pieces of equipment (a JACE, a
    Siemens field panel) shows up as object names prefixed by different
    spec tags. Partition by referenced tag into virtual devices
    `field_id#TAG` so each can align on its own; objects naming no tag
    stay with the parent."""
    classified = [classify_object(o.get("name"), o.get("object_identifier"), o.get("units"), o.get("present_value"), o.get("description"), o.get("point_uri"), o.get("tags")) for o in device.get("objects", [])]
    groups: dict[str, list[ClassifiedPoint]] = {}
    rest: list[ClassifiedPoint] = []
    for p in classified:
        hit = None
        for cand in _name_candidates(p.get("name")):
            n = normalize_tag(cand)
            if n in spec_norms:
                hit = n
                break
        if hit:
            groups.setdefault(hit, []).append(p)
        else:
            rest.append(p)
    big = {n: ps for n, ps in groups.items() if len(ps) >= 2}
    if len(big) < 2:
        return [(device, classified)]
    out: list[tuple[DiscoveredDevice, list[ClassifiedPoint]]] = []
    for n, ps in big.items():
        virtual: DiscoveredDevice = dict(device)  # type: ignore[assignment]
        virtual["field_id"] = f"{device['field_id']}#{spec_norms[n]['tag']}"
        virtual["name"] = f"{device.get('name') or device['field_id']} / {spec_norms[n]['tag']}"
        virtual["description"] = f"objects on {device.get('name') or device['field_id']} whose names carry {spec_norms[n]['tag']} - one of {len(big)} pieces of equipment this device fronts"
        virtual["objects"] = [o for o in device.get("objects", []) if any(o.get("point_uri") == p.get("point_uri") and o.get("name") == p.get("name") for p in ps)]
        out.append((virtual, ps))
    leftovers = rest + [p for n, ps in groups.items() if n not in big for p in ps]
    if leftovers:
        parent: DiscoveredDevice = dict(device)  # type: ignore[assignment]
        parent["objects"] = [o for o in device.get("objects", []) if any(o.get("point_uri") == p.get("point_uri") and o.get("name") == p.get("name") for p in leftovers)]
        parent["description"] = f"{device.get('description') or ''} (objects not attributable to any spec tag; this device also fronts {', '.join(spec_norms[n]['tag'] for n in big)})".strip()
        out.append((parent, leftovers))
    return out


# --- alignment ----------------------------------------------------------------------

@dataclass
class Candidate:
    spec: SpecEquipment
    device: DiscoveredDevice
    classified: list[ClassifiedPoint]
    tag: EvidenceItem
    fit: SignatureFit
    best_fit: SignatureFit
    matched: list[tuple[dict, ClassifiedPoint]]
    absent: list[dict]
    extra: list[ClassifiedPoint]
    plaus: EvidenceItem
    topo: EvidenceItem
    vendor: EvidenceItem
    capture: EvidenceItem | None
    score: float


def _plausibility_evidence(matched: list[tuple[dict, ClassifiedPoint]], classified: list[ClassifiedPoint]) -> EvidenceItem:
    pool = [p for _, p in matched] or classified
    judged = [p for p in pool if p.get("plausibility") in ("plausible", "implausible")]
    bad = [p for p in judged if p["plausibility"] == "implausible"]
    if not judged:
        return {"rung": 3, "kind": "plausibility", "score": 0.0, "detail": "no numeric present values to judge"}
    if bad:
        return {"rung": 3, "kind": "plausibility", "score": -0.1 * len(bad), "detail": "; ".join(n for p in bad for n in p.get("notes", []) if "outside the plausible" in n)}
    return {"rung": 3, "kind": "plausibility", "score": 0.05, "detail": f"{len(judged)} present value(s) within plausible bands"}


def _candidate(spec: SpecEquipment, device: DiscoveredDevice, classified: list[ClassifiedPoint], spec_norms: dict[str, SpecEquipment], constraints: Constraints, ranked: list[SignatureFit]) -> Candidate:
    ctype = constraints.effective_type(spec, device["field_id"])
    tag = tag_evidence(spec, device, classified, constraints.aliases)
    fit = signature_fit(ctype, classified)
    matched, absent, extra = match_points(spec, classified)
    # The spec's own point list is a second, building-specific signature -
    # count it alongside the type's generic one.
    if spec.get("expected_points"):
        own_cov = len(matched) / len(spec["expected_points"])
        fit.score = round(min(1.0, 0.5 * fit.score + 0.5 * own_cov) if ctype else own_cov, 2)
    plaus = _plausibility_evidence(matched, classified)
    topo = topology_evidence(spec, classified, device, spec_norms, constraints)
    vendor = vendor_evidence(device)
    capture: EvidenceItem | None = None
    for sup in constraints.capture_support.get(device["field_id"], []):
        if normalize_tag(sup["spec_tag"]) == normalize_tag(spec["tag"]):
            capture = {"rung": 0, "kind": "field_capture", "score": 0.3, "detail": sup["why"]}
    score = 0.5 * tag["score"] + 0.35 * fit.score + topo["score"] + plaus["score"] + (capture["score"] if capture else 0.0)
    return Candidate(spec, device, classified, tag, fit, ranked[0] if ranked else fit, matched, absent, extra, plaus, topo, vendor, capture, round(max(0.0, score), 3))


def _confidence(c: Candidate, duplicate: bool) -> tuple[str, list[str]]:
    reasons: list[str] = []
    tag, sig = c.tag["score"], c.fit.score
    contradictions = list(c.fit.contradictions)
    implausible = c.plaus["score"] < 0
    topo_bad = c.topo["score"] < 0
    if contradictions:
        reasons.append(f"points contradict the spec type: {', '.join(contradictions)}")
    if tag < 0:
        reasons.append(c.tag["detail"])
    if implausible:
        reasons.append(c.plaus["detail"])
    if topo_bad:
        reasons.append(c.topo["detail"])
    if duplicate:
        reasons.append("another device also claims this tag")
    if tag >= 0.8 and sig >= 0.5 and not contradictions and not implausible and not topo_bad and not duplicate:
        return "high", reasons
    if c.capture and sig >= 0.3 and not contradictions and not duplicate:
        return "high", reasons
    if tag >= 0.8 or (tag == 0 and sig >= 0.8) or (tag >= 0.6 and sig >= 0.3) or (c.capture and tag >= 0):
        return "medium", reasons
    if c.score >= MIN_CANDIDATE_SCORE:
        return "low", reasons
    return "unmatched", reasons


def _alternatives(c: Candidate, ranked: list[SignatureFit], spec_norms: dict[str, SpecEquipment]) -> list[Alternative]:
    out: list[Alternative] = []
    spec_type = c.fit.type
    for alt in ranked[:3]:
        if alt.type == spec_type or alt.score < 0.3:
            continue
        why = f"points fit '{alt.type}' at {alt.score:.2f}"
        if alt.distinctive_hits:
            why += f" - it has {', '.join(alt.distinctive_hits)}"
        if c.fit.contradictions:
            why += f", which rules out a standard {spec_type}"
        if not any(normalize_tag(s["tag"]) != normalize_tag(c.spec["tag"]) and s.get("type") == alt.type for s in spec_norms.values()):
            why += f"; the spec has no {alt.type} here"
        out.append({"interpretation": f"{c.spec['tag']} is actually a {alt.type}" if spec_type else f"this device is a {alt.type}", "why": why, "would_resolve": f"nameplate on the unit ({c.spec['tag']}) or the controller's program name - a photo of either settles it"})
    if c.tag["score"] < 0.8:
        out.append({"interpretation": f"this device is not {c.spec['tag']} at all", "why": c.tag["detail"], "would_resolve": f"the enclosure label showing the address {c.device.get('address') or ''} next to the controller name, at the {c.spec['tag']} location"})
    return out


def _entity_points(matched, absent, extra) -> list[EntityPoint]:
    pts: list[EntityPoint] = []
    for sp, fp in matched:
        pts.append({"function": fp.get("function"), "role": fp.get("role"), "direction": fp.get("direction"), "units": fp.get("units"), "bacnet_object": fp.get("object_identifier"), "name": fp.get("name"), "point_uri": fp.get("point_uri"), "status": "matched", "spec_name": sp.get("name"), "plausibility": fp.get("plausibility"), "notes": list(fp.get("notes", [])) + ([f"spec lists it as {sp.get('role')}, field object reads as {fp.get('role')}"] if sp.get("role") and fp.get("role") and sp["role"] != fp["role"] else [])})
    for sp in absent:
        pts.append({"function": sp.get("function"), "role": sp.get("role"), "direction": sp.get("direction"), "units": sp.get("units"), "bacnet_object": None, "name": None, "point_uri": None, "status": "specified_absent", "spec_name": sp.get("name"), "plausibility": None, "notes": ["specified but not among the discovered objects - a deviation, not a point"]})
    for fp in extra:
        pts.append({"function": fp.get("function"), "role": fp.get("role"), "direction": fp.get("direction"), "units": fp.get("units"), "bacnet_object": fp.get("object_identifier"), "name": fp.get("name"), "point_uri": fp.get("point_uri"), "status": "unspecified_present" if fp.get("function") else "unreadable", "spec_name": None, "plausibility": fp.get("plausibility"), "notes": list(fp.get("notes", []))})
    return pts


def interpret_absence(phase: Phase | None, spec: SpecEquipment, cidr_scopes: list[str]) -> tuple[str, str, str]:
    """(interpretation, suspected, to_settle) for a spec device not found -
    absence means something different in every phase and is never
    reported bare."""
    scope = f" or on a segment outside the scanned ranges ({', '.join(cidr_scopes)})" if cidr_scopes else " or on a network segment that was never scanned (no CIDR scope recorded)"
    where = (spec.get("location") or {}).get("description") or (spec.get("location") or {}).get("room") or "its scheduled location"
    if phase == "new_construction":
        return (f"not yet installed - expected in new construction; progress data, not a fault. Could also be installed and not yet connected{scope}.", "unbuilt", f"ask the mechanical/controls contractor for the install and network-connection date for {spec['tag']}; no site visit needed yet")
    if phase == "warranty":
        return (f"never installed, or installed and never connected to the network{scope}. Someone owes an answer for this - the contractor is still responsible.", "unconnected", f"walk to {where}: photograph the unit's nameplate and the controller enclosure label (address) - if there is no controller, this is a punch item for the controls contractor")
    if phase == "retrofit":
        return (f"documentation may be stale: could be removed, replaced under a new name, never networked (a pneumatic or stand-alone unit), or on an isolated controller network{scope}.", "stale_documentation", f"walk to {where}: photograph what is actually there (nameplate + controller label if any); check for a serial trunk or a vendor gateway that this sweep can't see through")
    return (f"was expected to be running: failed, removed, replaced, re-addressed, or never networked{scope}. Investigate.", "offline_or_removed", f"check {where} - power to the controller, network cable, and whether the enclosure label's address is inside the scanned ranges; if the unit was replaced, capture the new controller's label")


@dataclass
class AlignmentResult:
    entities: list[Entity]
    mappings: list[Mapping]
    deviations: list[Deviation]
    assumptions: list[str]
    unmatched_devices: list[DiscoveredDevice]


def align(
    spec: SpecModel,
    devices: list[DiscoveredDevice],
    phase: Phase | None,
    constraints: Constraints | None = None,
    existing: list[Entity] | None = None,
    cidr_scopes: list[str] | None = None,
) -> AlignmentResult:
    """The whole of Phase 2 + 4 for one pass. `existing` entities lend
    their stable ids (by spec tag, else by field id) so a re-run after a
    correction moves confidence on the *same* entity rather than minting a
    new one."""
    constraints = constraints or Constraints()
    cidr_scopes = cidr_scopes or []
    assumptions: list[str] = []
    spec_norms: dict[str, SpecEquipment] = {}
    for e in spec.get("equipment", []):
        spec_norms.setdefault(normalize_tag(e["tag"]), e)

    prior_by_tag = {normalize_tag(e.get("spec_tag")): e for e in (existing or []) if e.get("spec_tag")}
    prior_by_field = {(e.get("field_identity") or {}).get("field_id"): e for e in (existing or []) if e.get("field_identity")}

    units: list[tuple[DiscoveredDevice, list[ClassifiedPoint]]] = []
    for d in devices:
        parts = split_gateway_device(d, spec_norms)
        if len(parts) > 1:
            assumptions.append(f"{d.get('name') or d['field_id']} fronts several pieces of equipment ({len(parts)} groups of object names carry different spec tags) - treated as {len(parts)} logical devices, not one")
        units.extend(parts)
    ranked_by_field = {u["field_id"]: rank_types(cls) for u, cls in units}

    candidates: list[Candidate] = []
    for e in spec_norms.values():
        if not e.get("networked", True):
            continue
        for u, cls in units:
            if (normalize_tag(e["tag"]), u["field_id"]) in constraints.rejected:
                continue
            candidates.append(_candidate(e, u, cls, spec_norms, constraints, ranked_by_field[u["field_id"]]))

    # Confirmed pairs first - fixed, never re-litigated automatically.
    chosen: dict[str, Candidate] = {}
    used_fields: set[str] = set()
    for norm, fid in constraints.confirmed.items():
        c = next((c for c in candidates if normalize_tag(c.spec["tag"]) == norm and c.device["field_id"] == fid), None)
        if c is not None:
            chosen[norm] = c
            used_fields.add(fid)
    for c in sorted(candidates, key=lambda c: -c.score):
        norm = normalize_tag(c.spec["tag"])
        if norm in chosen or c.device["field_id"] in used_fields or c.score < MIN_CANDIDATE_SCORE:
            continue
        chosen[norm] = c
        used_fields.add(c.device["field_id"])

    # Duplicate claims: any other device that also fits the tag at Medium+.
    duplicates: dict[str, list[Candidate]] = {}
    for c in candidates:
        norm = normalize_tag(c.spec["tag"])
        picked = chosen.get(norm)
        if picked is None or c is picked or c.device["field_id"] in constraints.confirmed.values():
            continue
        if _confidence(c, False)[0] in ("high", "medium") and c.tag["score"] >= 0.6:
            duplicates.setdefault(norm, []).append(c)

    entities: list[Entity] = []
    mappings: list[Mapping] = []
    deviations: list[Deviation] = []
    ts = now_iso()

    for norm, e in spec_norms.items():
        c = chosen.get(norm)
        prior = prior_by_tag.get(norm)
        eid = prior["id"] if prior else new_id("ent")
        etype = constraints.effective_type(e, c.device["field_id"] if c else None)
        rel = {"feeds": list(e.get("feeds", [])), "is_fed_by": list(e.get("fed_by", [])), "serves": list(e.get("serves", [])), "is_part_of": [], "is_located_in": e.get("location")}
        for frm, kind, to in constraints.topology:
            if kind == "feeds" and frm == norm and to in spec_norms and spec_norms[to]["tag"] not in rel["feeds"]:
                rel["feeds"].append(spec_norms[to]["tag"])
            if kind == "feeds" and to == norm and frm in spec_norms and spec_norms[frm]["tag"] not in rel["is_fed_by"]:
                rel["is_fed_by"].append(spec_norms[frm]["tag"])
        base: Entity = {
            "id": eid, "canonical_type": etype, "description": e.get("description", ""), "spec_tag": e["tag"], "field_identity": None,
            "relationships": rel, "points": [], "ladder": (prior or {}).get("ladder") or {"highest_rung_passed": 0, "rungs": []},
            "confidence": "unmatched", "confidence_basis": [], "alternatives": [], "acceptance": None,
            "provenance": {"spec_source": e.get("source"), "first_discovered": (prior or {}).get("provenance", {}).get("first_discovered"), "last_observed": (prior or {}).get("provenance", {}).get("last_observed"), "field_captures": [n for n in constraints.nameplate_only.get(norm, [])], "corrections": (prior or {}).get("provenance", {}).get("corrections", [])},
            "liveness": (prior or {}).get("liveness") or {"state": "never_seen", "absent_since": None, "last_observed": None, "staleness_seconds": None, "signature_hash": None},
            "deviations": [], "faults": (prior or {}).get("faults", []), "accepted_risks": (prior or {}).get("accepted_risks", []), "reconcile_state": None, "reconcile_detail": None, "updated_at": ts,
        }
        if not e.get("networked", True):
            base["liveness"] = {**base["liveness"], "state": "not_networked"}
            base["points"] = _entity_points([], list(e.get("expected_points", [])), [])
            if constraints.nameplate_only.get(norm):
                base["confidence"] = "confirmed"
                base["acceptance"] = "confirmed"
                base["confidence_basis"] = [{"rung": 0, "kind": "field_capture", "score": 1.0, "detail": f"nameplate captured ({', '.join(str(n['capture_id']) for n in constraints.nameplate_only[norm])}) - physically present, no BACnet objects by design"}]
            else:
                base["confidence"] = "low"
                base["confidence_basis"] = [{"rung": 0, "kind": "spec", "score": 0.0, "detail": "spec says this equipment is not networked; only a nameplate photo can verify it exists"}]
            entities.append(base)
            continue

        if c is None:
            interp, suspected, settle = interpret_absence(phase, e, cidr_scopes)
            dev_id = new_id("dev")
            deviations.append({"id": dev_id, "kind": "spec_device_not_found", "spec_tag": e["tag"], "field_id": None, "entity_id": eid, "spec_says": f"{e['tag']} ({etype or e.get('type_as_written') or 'unknown type'}) with {len(e.get('expected_points', []))} points, {e['source'].get('document')} p.{e['source'].get('page')}", "field_shows": "no discovered device carries the tag, and no device's point signature fits it well enough to propose", "interpretation": interp, "suspected": suspected, "to_settle": settle, "severity": "info" if phase == "new_construction" else "warning", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})
            base["deviations"] = [dev_id]
            base["points"] = _entity_points([], list(e.get("expected_points", [])), [])
            base["confidence_basis"] = [{"rung": 1, "kind": "tag", "score": 0.0, "detail": "not found"}]
            if constraints.nameplate_only.get(norm):
                base["confidence_basis"].append({"rung": 0, "kind": "field_capture", "score": 0.5, "detail": "a nameplate capture proves the unit physically exists - the gap is network, not installation"})
                deviations[-1]["field_shows"] += "; a field capture shows the unit's nameplate, so it exists physically"
                deviations[-1]["suspected"] = "unconnected"
            entities.append(base)
            mappings.append({"spec_tag": e["tag"], "field_id": None, "canonical_type": etype, "confidence": "unmatched", "score": 0.0, "basis": base["confidence_basis"], "alternatives": [], "acceptance": None, "contradictions": []})
            continue

        is_dup = norm in duplicates
        confidence, reasons = _confidence(c, is_dup)
        human_confirmed = norm in constraints.confirmed and constraints.confirmed[norm] == c.device["field_id"]
        if human_confirmed:
            confidence = "confirmed"
        acceptance = "confirmed" if confidence == "confirmed" else ("auto-accepted" if confidence == "high" else None)
        basis: list[EvidenceItem] = [c.tag, {"rung": 2, "kind": "signature", "score": c.fit.score, "detail": f"fits '{etype}' with required coverage {c.fit.required_coverage:.0%}" + (f", missing {', '.join(c.fit.missing_required)}" if c.fit.missing_required else "") + (f", contradicted by {', '.join(c.fit.contradictions)}" if c.fit.contradictions else "")}, c.plaus, c.topo, c.vendor]
        if c.capture:
            basis.insert(0, c.capture)
        if human_confirmed:
            basis.insert(0, {"rung": 0, "kind": "correction", "score": 1.0, "detail": "mapping confirmed by a person - fixed until a later correction says otherwise; the automatic evidence below is kept for the record"})
        alternatives = _alternatives(c, ranked_by_field[c.device["field_id"]], spec_norms) if confidence not in ("high", "confirmed") else []
        dev_ids: list[str] = []

        if c.fit.contradictions or (c.best_fit.type and c.best_fit.type != etype and c.best_fit.score >= c.fit.score + 0.3 and c.tag["score"] >= 0.6):
            dev_ids.append(new_id("dev"))
            deviations.append({"id": dev_ids[-1], "kind": "type_mismatch", "spec_tag": e["tag"], "field_id": c.device["field_id"], "entity_id": eid, "spec_says": f"{e['tag']} is a {etype}", "field_shows": f"points fit '{c.best_fit.type}' ({c.best_fit.score:.2f}) better than '{etype}' ({c.fit.score:.2f})" + (f"; has {', '.join(c.fit.contradictions)}" if c.fit.contradictions else ""), "interpretation": "tag matches but the point signature says otherwise - often a replacement-in-place that kept the old tag, or a controller re-used from another unit", "suspected": "replacement_in_place", "to_settle": f"photograph the nameplate on {e['tag']} and the controller's program/name screen; confirm or correct the type", "severity": "warning", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})
        if c.absent or c.extra:
            missing = [sp.get("name") or sp.get("function") for sp in c.absent]
            extra = [label(p) for p in c.extra if p.get("function")]
            unreadable = [p.get("name") for p in c.extra if not p.get("function")]
            if missing or extra:
                dev_ids.append(new_id("dev"))
                deviations.append({"id": dev_ids[-1], "kind": "point_mismatch", "spec_tag": e["tag"], "field_id": c.device["field_id"], "entity_id": eid, "spec_says": f"{len(e.get('expected_points', []))} points: {', '.join(str(sp.get('name')) for sp in e.get('expected_points', []))}", "field_shows": (f"missing {', '.join(str(m) for m in missing)}" if missing else "") + ("; " if missing and extra else "") + (f"additionally exposes {', '.join(extra)}" if extra else "") + (f"; {len(unreadable)} object(s) whose names could not be read: {', '.join(str(u) for u in unreadable)}" if unreadable else ""), "interpretation": "equipment matches, point list does not - points may be unmapped in the controller, named beyond recognition, or the spec's point schedule may be for a different submittal revision", "suspected": "point_schedule_drift", "to_settle": "compare against the controller's actual program point list (vendor tool or the controller's local display); a missing sensor is a physical check", "severity": "info", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})
        if is_dup:
            dev_ids.append(new_id("dev"))
            others = ", ".join(f"{d.device.get('name') or d.device['field_id']} ({d.score:.2f})" for d in duplicates[norm])
            deviations.append({"id": dev_ids[-1], "kind": "duplicate_claim", "spec_tag": e["tag"], "field_id": c.device["field_id"], "entity_id": eid, "spec_says": f"one {e['tag']}", "field_shows": f"chosen {c.device.get('name') or c.device['field_id']} ({c.score:.2f}); also plausible: {others}", "interpretation": "two devices plausibly map to one spec tag - a spare/replaced controller left online, a supervisory gateway re-exposing the same unit, or a mis-tagged neighbour", "suspected": "duplicate_or_reexposed", "to_settle": f"at the {e['tag']} location, capture the enclosure label (address) of the controller actually wired to it - that address wins; check whether the other is a gateway re-exposing the same points", "severity": "warning", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})
        if c.topo["score"] < 0:
            dev_ids.append(new_id("dev"))
            deviations.append({"id": dev_ids[-1], "kind": "topology_mismatch", "spec_tag": e["tag"], "field_id": c.device["field_id"], "entity_id": eid, "spec_says": f"{e['tag']} fed by {', '.join(e.get('fed_by', [])) or '(none listed)'}", "field_shows": c.topo["detail"], "interpretation": "the controller's own naming points at a different upstream unit than the drawings do - either the drawings are wrong or the program was copied from another unit", "suspected": "topology_or_copied_program", "to_settle": "trace the duct/pipe from the unit to its source, or confirm the upstream reference in the controller program", "severity": "warning", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})
        for p in c.classified:
            for note in p.get("notes", []):
                if "contradict the name" in note:
                    dev_ids.append(new_id("dev"))
                    deviations.append({"id": dev_ids[-1], "kind": "unit_contradiction", "spec_tag": e["tag"], "field_id": c.device["field_id"], "entity_id": eid, "spec_says": f"object '{p.get('name')}' reads as {p.get('function')}", "field_shows": note, "interpretation": "the object's engineering units disagree with its own name - one of them is wrong, and any analytic trusting either will be too", "suspected": "misconfigured_units_or_name", "to_settle": "open the object in the vendor tool and check which is right against the physical sensor", "severity": "warning", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})

        last_seen = c.device.get("last_seen")
        first = (prior or {}).get("provenance", {}).get("first_discovered") or last_seen or ts
        base.update({
            "field_identity": {"field_id": c.device["field_id"], "device_instance": c.device.get("device_instance"), "address": c.device.get("address"), "name": c.device.get("name"), "vendor": c.device.get("vendor_name") or c.device.get("vendor_id"), "model": c.device.get("model"), "topic_prefix": c.device.get("topic_prefix"), "source": c.device.get("source")},
            "points": _entity_points(c.matched, c.absent, c.extra),
            "confidence": confidence, "confidence_basis": basis, "alternatives": alternatives, "acceptance": acceptance,
            "deviations": dev_ids,
        })
        base["provenance"] = {**base["provenance"], "first_discovered": first, "last_observed": last_seen or base["provenance"].get("last_observed")}
        if constraints.field_locations.get(c.device["field_id"]) and not base["relationships"].get("is_located_in"):
            base["relationships"]["is_located_in"] = constraints.field_locations[c.device["field_id"]]  # type: ignore[typeddict-item]
        if reasons:
            basis.append({"rung": 0, "kind": "confidence_note", "score": 0.0, "detail": "; ".join(reasons)})
        entities.append(base)
        mappings.append({"spec_tag": e["tag"], "field_id": c.device["field_id"], "canonical_type": etype, "confidence": confidence, "score": c.score, "basis": basis, "alternatives": alternatives, "acceptance": acceptance, "contradictions": list(c.fit.contradictions)})  # type: ignore[typeddict-item]

    # Field devices no spec entry claimed - identify from the signature.
    unmatched_devices: list[DiscoveredDevice] = []
    for u, cls in units:
        if u["field_id"] in used_fields:
            continue
        unmatched_devices.append(u)
        ranked = ranked_by_field[u["field_id"]]
        best = ranked[0] if ranked and ranked[0].score >= 0.3 else None
        prior = prior_by_field.get(u["field_id"])
        eid = prior["id"] if prior else new_id("ent")
        etype = constraints.type_overrides.get(u["field_id"]) or (best.type if best else None)
        alternatives = [{"interpretation": f"this device is a {f.type}", "why": f"points fit at {f.score:.2f}" + (f" - has {', '.join(f.distinctive_hits)}" if f.distinctive_hits else ""), "would_resolve": "nameplate or controller program name at the device's location"} for f in ranked[:3] if f.score >= 0.3 and f.type != etype]
        if u["field_id"] in constraints.type_overrides:
            confidence, basis_detail = "confirmed", "type set by human correction"
        elif best and best.score >= 0.7 and not best.contradictions:
            confidence, basis_detail = "medium", f"signature fits '{best.type}' ({best.score:.2f}); no spec tag to anchor it"
        elif best:
            confidence, basis_detail = "low", f"signature weakly fits '{best.type}' ({best.score:.2f})"
        else:
            confidence, basis_detail = "unmatched", "no type signature fits these points"
        referenced = sorted({spec_norms[normalize_tag(cand)]["tag"] for p in cls for cand in _name_candidates(p.get("name")) if normalize_tag(cand) in spec_norms})
        dev_id = new_id("dev")
        phase_note = "common in retrofits and takeovers - the drawings never knew about it" if phase in ("retrofit", "operations") else "in a new build this is usually a contractor connecting equipment under a different tag than the schedule, or test/temporary equipment"
        deviations.append({"id": dev_id, "kind": "field_device_not_in_spec", "spec_tag": None, "field_id": u["field_id"], "entity_id": eid, "spec_says": "nothing - no schedule entry claims this device", "field_shows": f"{u.get('name') or u['field_id']} ({u.get('vendor_name') or 'unknown vendor'}) with {len(cls)} objects: {', '.join(sorted({label(p) for p in cls if p.get('function')})[:8])}" + (f"; names reference {', '.join(referenced)}" if referenced else ""), "interpretation": f"proposed type: {etype or 'unknown'} ({confidence}); {phase_note}", "suspected": "undocumented_equipment", "to_settle": "find the controller by its address label; photograph the nameplate of what it controls; decide whether to add it to the model or ignore it", "severity": "info", "status": "open", "opened_at": ts, "resolved_at": None, "resolved_by": None, "resolution": None})
        entities.append({
            "id": eid, "canonical_type": etype, "description": u.get("description") or (V.EQUIPMENT_TYPES.get(etype or "", {}).get("description", "") if etype else "unidentified device"), "spec_tag": None,
            "field_identity": {"field_id": u["field_id"], "device_instance": u.get("device_instance"), "address": u.get("address"), "name": u.get("name"), "vendor": u.get("vendor_name") or u.get("vendor_id"), "model": u.get("model"), "topic_prefix": u.get("topic_prefix"), "source": u.get("source")},
            "relationships": {"feeds": [], "is_fed_by": referenced if best and V.EQUIPMENT_TYPES.get(best.type or "", {}).get("terminal") else [], "serves": [], "is_part_of": [], "is_located_in": constraints.field_locations.get(u["field_id"])},  # type: ignore[typeddict-item]
            "points": _entity_points([], [], cls), "ladder": (prior or {}).get("ladder") or {"highest_rung_passed": 0, "rungs": []},
            "confidence": confidence, "confidence_basis": [{"rung": 2, "kind": "signature", "score": best.score if best else 0.0, "detail": basis_detail}, vendor_evidence(u)], "alternatives": alternatives, "acceptance": "confirmed" if confidence == "confirmed" else None,  # type: ignore[typeddict-item]
            "provenance": {"spec_source": None, "first_discovered": (prior or {}).get("provenance", {}).get("first_discovered") or u.get("last_seen") or ts, "last_observed": u.get("last_seen"), "field_captures": [], "corrections": (prior or {}).get("provenance", {}).get("corrections", [])},
            "liveness": (prior or {}).get("liveness") or {"state": "present" if u.get("last_seen") else "never_seen", "absent_since": None, "last_observed": u.get("last_seen"), "staleness_seconds": None, "signature_hash": None},
            "deviations": [dev_id], "faults": (prior or {}).get("faults", []), "accepted_risks": (prior or {}).get("accepted_risks", []), "reconcile_state": None, "reconcile_detail": None, "updated_at": ts,
        })
        mappings.append({"spec_tag": None, "field_id": u["field_id"], "canonical_type": etype, "confidence": confidence, "score": best.score if best else 0.0, "basis": [], "alternatives": alternatives, "acceptance": None, "contradictions": []})  # type: ignore[typeddict-item]

    # Volunteer what we notice about the building as a whole.
    for norm, e in spec_norms.items():
        etype = constraints.effective_type(e, None)
        if etype in AIR_SOURCE_TYPES:
            terminals = [s for s in spec_norms.values() if norm in {normalize_tag(x) for x in s.get("fed_by", [])}]
            zones = len(e.get("serves", []))
            if zones and terminals and zones != len(terminals):
                assumptions.append(f"{e['tag']} serves {zones} zone(s) per the schedule but {len(terminals)} terminal unit(s) list it as their source - counts don't agree")
    return AlignmentResult(entities, mappings, deviations, assumptions, unmatched_devices)


def infer_type_for_tag(tag: str) -> str | None:
    return infer_type_from_tag(tag)


__all__ = ["AlignmentResult", "Constraints", "align", "build_constraints", "infer_type_for_tag", "interpret_absence", "match_points", "rank_types", "signature_fit", "split_gateway_device", "tag_evidence", "tokenize", "topology_evidence"]
