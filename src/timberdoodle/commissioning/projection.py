"""
Projections of the canonical model into Brick and Haystack.

The canonical model (`model.Entity`) is the storage form. Brick and
Haystack are *views* of it - generated on demand, written to the existing
graph through the same `store`/`ingest` helpers every other writer uses,
and never read back as the source of truth. Anything either vocabulary
cannot say is reported as lossy rather than approximated:

  * confidence, alternatives, ladder results, deviations and provenance
    have no home in Brick; they stay in the canonical model and are
    referenced from the graph by entity id (TD namespace) only.
  * an equipment type Brick lacks (unit heater) gets no rdf:type; a point
    function Brick lacks gets the generic role class (Sensor/Setpoint/
    Command/Status) and a lossy note naming what was dropped.
  * only entities whose identity is settled (`confirmed`/`high`) are
    projected by default - Brick has no way to say "probably this AHU",
    and a graph that says it flatly is worse than one that says nothing.

Equipment URIs reuse the identity the ingest layer already minted
(`urn:equip:<topic_prefix>`) when the entity is bound to a discovered
device, so the projection enriches the existing node rather than creating
a parallel one. Spec-only entities (not yet found in the field) get
`urn:equip:spec:<tag>` so they can appear in the graph as expected-but-
unobserved, and are merged when a device is later confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rdflib import RDF, RDFS, Literal, URIRef

from timberdoodle import brick_vocab
from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.model import Entity
from timberdoodle.commissioning.spec_model import normalize_tag
from timberdoodle.store import BRICK, TD

PROJECTED_CONFIDENCE = ("confirmed", "high")


def equip_uri(entity: Entity) -> URIRef:
    fid = entity.get("field_identity") or {}
    if fid.get("equip_uri"):
        return URIRef(str(fid["equip_uri"]))
    if fid.get("topic_prefix"):
        return URIRef(f"urn:equip:{fid['topic_prefix']}")
    if entity.get("spec_tag"):
        return URIRef(f"urn:equip:spec:{normalize_tag(entity['spec_tag']).lower()}")
    return URIRef(f"urn:equip:cx:{entity['id']}")


def location_uri(loc: dict) -> URIRef | None:
    parts = [str(loc.get(k)).strip() for k in ("building", "floor", "room") if loc.get(k) is not None and str(loc.get(k)).strip()]
    if not parts:
        return None
    return URIRef("urn:location:" + "/".join(p.replace(" ", "_") for p in parts))


@dataclass
class ProjectionResult:
    triples: list[tuple[URIRef, URIRef, object]] = field(default_factory=list)
    lossy: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    projected_entities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "triples": [(str(s), str(p), str(o)) for s, p, o in self.triples],
            "triple_count": len(self.triples),
            "projected_entities": self.projected_entities,
            "lossy": self.lossy,
            "skipped": self.skipped,
        }


def _brick_class(vocab: brick_vocab.Vocab | None, name: str | None) -> str | None:
    if not name:
        return None
    if vocab is None:
        return name
    return vocab.canonical_class(name)


def project_brick(entities: list[Entity], vocab: brick_vocab.Vocab | None = None, include_unsettled: bool = False) -> ProjectionResult:
    if vocab is None:
        try:
            vocab = brick_vocab.load()
        except (OSError, ValueError):  # ontology file not vendored in this checkout
            vocab = None
    res = ProjectionResult()
    by_tag = {normalize_tag(e.get("spec_tag")): e for e in entities if e.get("spec_tag")}
    T = res.triples

    for e in entities:
        label = e.get("spec_tag") or (e.get("field_identity") or {}).get("name") or e["id"]
        if e.get("confidence") not in PROJECTED_CONFIDENCE and not include_unsettled:
            res.skipped.append(f"{label}: identity is {e.get('confidence')} - not projected; Brick cannot carry 'probably'. Confirm or correct the mapping first")
            continue
        if not e.get("field_identity") and (e.get("liveness") or {}).get("state") != "not_networked":
            res.skipped.append(f"{label}: not observed in the field - kept as an expected entity in the canonical model only")
            continue
        uri = equip_uri(e)
        res.projected_entities.append(e["id"])
        T.append((uri, TD.commissioningEntity, Literal(e["id"])))
        T.append((uri, TD.identityConfidence, Literal(e.get("confidence") or "unmatched")))
        if e.get("spec_tag"):
            T.append((uri, TD.specTag, Literal(e["spec_tag"])))
            T.append((uri, RDFS.label, Literal(e["spec_tag"])))
        if e.get("description"):
            T.append((uri, RDFS.comment, Literal(e["description"])))

        etype = e.get("canonical_type")
        brick_name = V.EQUIPMENT_TYPES.get(etype or "", {}).get("brick") if etype else None
        cls = _brick_class(vocab, brick_name)
        if cls:
            T.append((uri, RDF.type, BRICK[cls]))
        elif etype:
            res.lossy.append(f"{label}: canonical type '{etype}' has no Brick class - typed only as brick:Equipment")
            T.append((uri, RDF.type, BRICK.Equipment))
        else:
            res.lossy.append(f"{label}: type unknown - typed only as brick:Equipment")
            T.append((uri, RDF.type, BRICK.Equipment))

        rel = e.get("relationships") or {}
        for tag in rel.get("feeds", []):
            other = by_tag.get(normalize_tag(tag))
            if other and (other.get("confidence") in PROJECTED_CONFIDENCE or include_unsettled):
                T.append((uri, BRICK.feeds, equip_uri(other)))
                T.append((equip_uri(other), BRICK.isFedBy, uri))
            else:
                res.lossy.append(f"{label} feeds {tag}: {tag} is not projected (unsettled or unknown) - edge dropped")
        for tag in rel.get("is_fed_by", []):
            other = by_tag.get(normalize_tag(tag))
            if other and (other.get("confidence") in PROJECTED_CONFIDENCE or include_unsettled):
                T.append((equip_uri(other), BRICK.feeds, uri))
                T.append((uri, BRICK.isFedBy, equip_uri(other)))
            else:
                res.lossy.append(f"{label} is fed by {tag}: {tag} is not projected - edge dropped")
        if rel.get("serves"):
            res.lossy.append(f"{label} serves {', '.join(map(str, rel['serves']))}: zones are not modelled as entities here - brick:feeds to a Zone would need zone URIs the spec did not provide")
        loc = rel.get("is_located_in") or {}
        luri = location_uri(loc)
        if luri:
            T.append((uri, BRICK.hasLocation, luri))
            T.append((luri, BRICK.isLocationOf, uri))
            T.append((luri, RDF.type, BRICK.Room if loc.get("room") else BRICK.Floor if loc.get("floor") is not None else BRICK.Building))
            T.append((luri, RDFS.label, Literal(", ".join(str(loc.get(k)) for k in ("building", "floor", "room") if loc.get(k) is not None))))

        for p in e.get("points", []):
            if p.get("status") == "specified_absent":
                res.lossy.append(f"{label}: specified point '{p.get('spec_name')}' is absent in the field - Brick has no 'expected but missing' point; it stays a deviation")
                continue
            if not p.get("point_uri"):
                continue
            puri = URIRef(p["point_uri"])
            T.append((uri, BRICK.hasPoint, puri))
            T.append((puri, BRICK.isPointOf, uri))
            proj = V.POINT_PROJECTIONS.get((p.get("function") or "", p.get("role") or ""))
            cls = _brick_class(vocab, proj["brick"] if proj else None)
            if cls:
                T.append((puri, RDF.type, BRICK[cls]))
            else:
                generic = V.GENERIC_ROLE_BRICK.get(p.get("role") or "")
                if generic:
                    T.append((puri, RDF.type, BRICK[generic]))
                if p.get("function"):
                    res.lossy.append(f"{label}/{p.get('name')}: '{p['function']} {p.get('role')}' has no Brick class - typed as brick:{generic or 'Point'}")
                else:
                    res.lossy.append(f"{label}/{p.get('name')}: function unreadable - typed as brick:{generic or 'Point'} only")
            if p.get("status") == "unspecified_present":
                T.append((puri, TD.unspecifiedBySpec, Literal(True)))

        if e.get("ladder", {}).get("rungs") or e.get("deviations") or e.get("alternatives"):
            res.lossy.append(f"{label}: ladder results, open deviations and alternatives are canonical-model only (entity {e['id']}) - not expressible in Brick")
    return res


def write_brick(store, result: ProjectionResult) -> int:
    """Write the projection into the existing graph via the same
    `add_many` every other writer uses. Returns triples written."""
    if result.triples:
        store.add_many(result.triples)
    return len(result.triples)


def project_haystack(entities: list[Entity], include_unsettled: bool = False) -> dict:
    """Haystack 4 style records: marker tags as `True`, valued tags as
    values, refs as `@id` strings. Same skip/lossy discipline as Brick."""
    recs: list[dict] = []
    lossy: list[str] = []
    skipped: list[str] = []
    ids = {e["id"]: f"@cx-{e['id']}" for e in entities}
    by_tag = {normalize_tag(e.get("spec_tag")): e for e in entities if e.get("spec_tag")}
    for e in entities:
        label = e.get("spec_tag") or (e.get("field_identity") or {}).get("name") or e["id"]
        if e.get("confidence") not in PROJECTED_CONFIDENCE and not include_unsettled:
            skipped.append(f"{label}: identity is {e.get('confidence')} - not projected")
            continue
        rec: dict = {"id": ids[e["id"]], "dis": label, "equip": True}
        etype = e.get("canonical_type")
        for m in (V.EQUIPMENT_TYPES.get(etype or "", {}).get("haystack") or []):
            rec[m] = True
        if etype and not V.EQUIPMENT_TYPES.get(etype, {}).get("haystack"):
            lossy.append(f"{label}: type '{etype}' has no Haystack marker set")
        fid = e.get("field_identity") or {}
        if fid.get("address"):
            rec["bacnetAddr"] = fid["address"]
        if fid.get("device_instance") is not None:
            rec["bacnetDevice"] = fid["device_instance"]
        if e.get("spec_tag"):
            rec["specTag"] = e["spec_tag"]
        rel = e.get("relationships") or {}
        fed_by: list[Entity] = [x for x in (by_tag.get(normalize_tag(t)) for t in rel.get("is_fed_by", [])) if x is not None]
        if fed_by:
            if len(fed_by) > 1:
                lossy.append(f"{label}: fed by {len(fed_by)} units - Haystack's single equipRef keeps only {fed_by[0].get('spec_tag')}")
            rec["equipRef"] = ids[fed_by[0]["id"]]
        loc = rel.get("is_located_in") or {}
        if loc.get("room") or loc.get("floor") is not None:
            rec["space"] = ", ".join(str(loc.get(k)) for k in ("floor", "room") if loc.get(k) is not None)
        rec["points"] = []
        for p in e.get("points", []):
            if p.get("status") == "specified_absent":
                continue
            prec: dict = {"dis": p.get("name"), "point": True, "equipRef": rec["id"]}
            proj = V.POINT_PROJECTIONS.get((p.get("function") or "", p.get("role") or ""))
            if proj:
                for m in proj["haystack"]:
                    prec[m] = True
            else:
                role_marker = {"sensor": "sensor", "setpoint": "sp", "command": "cmd", "status": "sensor"}.get(p.get("role") or "")
                if role_marker:
                    prec[role_marker] = True
                lossy.append(f"{label}/{p.get('name')}: '{p.get('function')} {p.get('role')}' has no Haystack tag set beyond the role marker")
            if p.get("units"):
                prec["unit"] = p["units"]
            if p.get("bacnet_object"):
                prec["bacnetCur"] = p["bacnet_object"]
            if p.get("point_uri"):
                prec["sourceUri"] = p["point_uri"]
            rec["points"].append(prec)
        recs.append(rec)
        lossy.append(f"{label}: confidence/ladder/deviations not expressible in Haystack - see entity {e['id']}")
    return {"records": recs, "lossy": lossy, "skipped": skipped}
