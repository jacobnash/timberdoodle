"""
Brick/Haystack are projections of the canonical model, never its storage:
only settled identities are projected, equipment URIs reuse the ingest
layer's `urn:equip:<topic_prefix>`, and anything the vocabulary cannot
say is reported as lossy - not approximated.
"""

from cx_fixtures import SPEC_MATERIAL, devices
from rdflib import RDF, URIRef

from timberdoodle import brick_vocab
from timberdoodle.commissioning import alignment, projection
from timberdoodle.commissioning.spec_model import build_spec_model
from timberdoodle.store import BRICK, TD, Store

SPEC = build_spec_model(SPEC_MATERIAL)


def _entities():
    return alignment.align(SPEC, devices(), "operations", None, None, []).entities


def test_only_settled_identities_are_projected_and_the_rest_are_named_in_skipped():
    ents = _entities()
    res = projection.project_brick(ents)
    tags = {e["id"]: e.get("spec_tag") for e in ents}
    projected = {tags[i] for i in res.projected_entities}
    assert projected == {"AHU-1", "VAV-1-01"}
    assert any(s.startswith("VAV-1-02: identity is low") for s in res.skipped)
    assert any(s.startswith("CH-1: not observed in the field") for s in res.skipped)
    # opting in projects the unsettled ones too, still flagged by confidence
    res2 = projection.project_brick(ents, include_unsettled=True)
    assert len(res2.projected_entities) > len(res.projected_entities)
    assert (URIRef("urn:equip:site/V1_3"), TD.identityConfidence, None) in _index(res2.triples)


def _index(triples):
    class Idx:
        def __init__(self, t):
            self.t = t

        def __contains__(self, q):
            s, p, o = q
            return any((s is None or ts == s) and (p is None or tp == p) and (o is None or to == o) for ts, tp, to in self.t)
    return Idx(triples)


def test_equipment_uri_reuses_the_ingest_layers_identity():
    ents = _entities()
    ahu = next(e for e in ents if e["spec_tag"] == "AHU-1")
    assert projection.equip_uri(ahu) == URIRef("urn:equip:site/AHU_1")
    ch = next(e for e in ents if e["spec_tag"] == "CH-1")
    assert projection.equip_uri(ch) == URIRef("urn:equip:spec:ch1")  # normalized tag - merges when the device is confirmed


def test_brick_classes_are_verified_against_the_vocabulary_and_points_get_role_classes():
    ents = _entities()
    vocab = brick_vocab.load()
    res = projection.project_brick(ents, vocab=vocab)
    idx = _index(res.triples)
    ahu = URIRef("urn:equip:site/AHU_1")
    assert (ahu, RDF.type, BRICK.Air_Handling_Unit) in idx
    assert (URIRef("urn:equip:site/V1_1"), RDF.type, BRICK.Variable_Air_Volume_Box) in idx
    assert (URIRef("urn:equip:site/V1_1"), BRICK.isFedBy, ahu) in idx
    assert (ahu, BRICK.feeds, URIRef("urn:equip:site/V1_1")) in idx
    sat = URIRef("urn:point:site/AHU_1/SA-T")
    assert (ahu, BRICK.hasPoint, sat) in idx
    assert (sat, RDF.type, BRICK.Supply_Air_Temperature_Sensor) in idx
    # every projected class is a real Brick class
    for s, p, o in res.triples:
        if p == RDF.type and str(o).startswith(str(BRICK)):
            assert vocab.canonical_class(str(o).split("#")[-1]) is not None, o
    # location and canonical-model back-reference
    assert (ahu, BRICK.hasLocation, URIRef("urn:location:1/Mech_101")) in idx
    assert (ahu, TD.commissioningEntity, None) in idx


def test_lossy_notes_name_what_brick_cannot_say_instead_of_approximating():
    ents = _entities()
    res = projection.project_brick(ents)
    lossy = " ".join(res.lossy)
    assert "AHU-1 serves VAV-1-01, VAV-1-02: zones are not modelled" in lossy  # no zone URIs to point feeds at
    assert "VAV-1-01/FLOW: 'flow sensor' has no Brick class - typed as brick:Sensor" in lossy
    assert "canonical-model only" in lossy                              # ladder/deviations/alternatives
    # an edge to an unsettled neighbour is dropped and said so
    ents2 = _entities()
    for e in ents2:
        if e["spec_tag"] == "AHU-1":
            e["relationships"]["feeds"] = ["VAV-1-02"]
            e["points"].append({"spec_name": "Mixed Air Temp", "function": "mixed air temperature", "role": "sensor", "status": "specified_absent", "point_uri": None})
    lossy2 = projection.project_brick(ents2).lossy
    assert any(s.startswith("AHU-1 feeds VAV-1-02: VAV-1-02 is not projected") for s in lossy2)
    assert any("specified point 'Mixed Air Temp' is absent in the field" in s for s in lossy2)  # no 'expected but missing' point in Brick
    assert not any("Brick cannot carry" in s for s in res.lossy)  # that wording belongs to skipped, not lossy


def test_write_brick_goes_through_the_stores_add_many():
    ents = _entities()
    res = projection.project_brick(ents)
    store = Store()
    n = projection.write_brick(store, res)
    assert n == len(res.triples) > 0
    rows = store.query("SELECT ?e WHERE { ?e <" + str(TD.commissioningEntity) + "> ?id }")
    assert len(list(rows)) == 2


def test_haystack_projection_uses_marker_tags_and_the_same_skip_discipline():
    ents = _entities()
    out = projection.project_haystack(ents)
    recs = {r["dis"]: r for r in out["records"]}
    assert set(recs) == {"AHU-1", "VAV-1-01"}
    ahu = recs["AHU-1"]
    assert ahu["equip"] is True and ahu["ahu"] is True
    assert ahu["bacnetAddr"] == "10.0.0.11" and ahu["bacnetDevice"] == 1001
    vav = recs["VAV-1-01"]
    assert vav["vav"] is True and vav["equipRef"] == ahu["id"]
    sat = next(p for p in ahu["points"] if p["dis"] == "SA-T")
    assert sat["point"] and sat["sensor"] and sat["temp"] and sat["discharge"] and sat["air"]
    assert sat["unit"] == "degF" and sat["sourceUri"] == "urn:point:site/AHU_1/SA-T"
    assert any(s.startswith("VAV-1-02") for s in out["skipped"])
