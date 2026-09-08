"""
hisquery.py's three pure layers - parser, filter->SPARQL compiler, span/
interval resolution - plus the compiled SPARQL actually run against an
in-memory rdflib Store (same graph shape ingest.py writes), so "compiles"
means "selects the right entities", not just "is a string". No live
services; the GET /his round trip is tests/test_his_route.py.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef

from timberdoodle import hisquery as h
from timberdoodle.store import BRICK, HAYSTACK, TD, Store

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 9, 8, 2, 30, tzinfo=UTC)  # a Tuesday


# --- parser ---------------------------------------------------------------


def test_full_chain_parses_into_mode_filter_span_rollup():
    q = h.parse_query("readAll(air and ahu).hisRead(thisMonth).hisRollup(avg, 1hr)")
    assert q.mode == "readAll"
    assert q.filter == h.And(h.Has("air"), h.Has("ahu"))
    assert q.filter_text == "air and ahu"
    assert q.span_text == "thisMonth"
    assert q.rollup == h.Rollup("avg", "1hr")


def test_read_singular_mode():
    assert h.parse_query("read(ahu).hisRead(today)").mode == "read"


def test_bare_filter_is_readall_with_no_span():
    q = h.parse_query("air and temp and sensor")
    assert q.mode == "readAll"
    assert q.span_text is None
    assert q.filter == h.And(h.And(h.Has("air"), h.Has("temp")), h.Has("sensor"))


def test_bare_filter_can_still_chain_hisread():
    q = h.parse_query("ahu.hisRead(yesterday)")
    assert q.filter == h.Has("ahu") and q.span_text == "yesterday"


def test_span_function_call_form_is_kept_verbatim():
    assert h.parse_query("readAll(ahu).hisRead(today())").span_text == "today()"


def test_date_month_year_and_range_spans_parse():
    for span in ["2026-09-03", "2026-09", "2026", "2026-09-01..2026-09-07", "2026-01..2026-03"]:
        assert h.parse_query(f"readAll(ahu).hisRead({span})").span_text == span


@pytest.mark.parametrize(
    "text, expected",
    [
        ("not vav", h.Not(h.Has("vav"))),
        ("a or b and c", h.Or(h.Has("a"), h.And(h.Has("b"), h.Has("c")))),
        ("(a or b) and c", h.And(h.Or(h.Has("a"), h.Has("b")), h.Has("c"))),
        ("not (a or b)", h.Not(h.Or(h.Has("a"), h.Has("b")))),
        ('dis == "Zone Temp"', h.Cmp("dis", "==", "Zone Temp")),
        ('dis != "x\\"y"', h.Cmp("dis", "!=", 'x"y')),
        ("temp > 72", h.Cmp("temp", ">", 72)),
        ("temp >= 72.5°F", h.Cmp("temp", ">=", 72.5)),
        ("area < -5", h.Cmp("area", "<", -5)),
        ("equipRef == @AHU-1", h.Cmp("equipRef", "==", h.Ref("AHU-1"))),
        ("id == @urn:point:fbf/mock-ahu-1/analogValue,1", h.Cmp("id", "==", h.Ref("urn:point:fbf/mock-ahu-1/analogValue,1"))),
        ("cur == true", h.Cmp("cur", "==", True)),
        ("writable == false", h.Cmp("writable", "==", False)),
    ],
)
def test_filter_grammar(text, expected):
    assert h.parse_query(text).filter == expected


def test_and_binds_tighter_than_or_and_not_tighter_than_and():
    assert h.parse_query("not a and b or c").filter == h.Or(h.And(h.Not(h.Has("a")), h.Has("b")), h.Has("c"))


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "empty query"),
        ("   ", "empty query"),
        ("ahu and", "expected a tag name"),
        ("ahu or (vav", "expected ')'"),
        ("ahu)", "unexpected ')'"),
        ("ahu->dis == \"x\"", "tag paths"),
        ("readAll(ahu).foo()", "unsupported function 'foo'"),
        ("readAll(ahu).hisRead()", "needs a span"),
        ("readAll(ahu).hisRead(nope)", "unknown span 'nope'"),
        ("readAll(ahu).hisRead(today).hisRead(today)", "only appear once"),
        ("readAll(ahu).hisRead(2026-09-07..2026-09-01)", "ends before it starts"),
        ("readAll(ahu).hisRollup(median, 1hr)", "unknown rollup fold 'median'"),
        ("readAll(ahu).hisRollup(avg, 1fortnight)", "unknown interval unit 'fortnight'"),
        ("readAll(ahu).hisRollup(avg, 0hr)", "must be positive"),
        ("readAll(ahu).hisRollup(avg)", "expected ','"),
        ("readAll(ahu).hisRollup(avg, 1hr).hisRollup(max, 1day)", "only appear once"),
        ("dis == null", "comparing to null"),
        ("dis ==", "expected a value"),
        ("id == 5", "id must be compared to a @ref"),
        ("id < @x", "id only supports"),
        ("equipRef > @x", "only applies to numbers and strings"),
        ("temp > true", "only applies to numbers and strings"),
        ("a $ b", "unexpected character '$'"),
    ],
)
def test_parse_errors_name_the_problem(text, fragment):
    with pytest.raises(h.HisQueryError) as exc:
        h.parse_query(text)
    assert fragment in str(exc.value)


# --- spans -----------------------------------------------------------------


def _span(text, tz="UTC"):
    s = h.resolve_span(text, NOW, tz)
    return s.start.isoformat(), s.end.isoformat()


def test_default_span_is_today():
    assert h.resolve_span(None, NOW).label == "today"


@pytest.mark.parametrize(
    "text, start, end",
    [
        ("today", "2026-09-08T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("today()", "2026-09-08T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("yesterday", "2026-09-07T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
        ("thisWeek", "2026-09-07T00:00:00+00:00", "2026-09-14T00:00:00+00:00"),  # Monday start
        ("lastWeek", "2026-08-31T00:00:00+00:00", "2026-09-07T00:00:00+00:00"),
        ("pastWeek", "2026-09-01T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("thisMonth", "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("lastMonth", "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        ("pastMonth", "2026-08-09T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("thisQuarter", "2026-07-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("lastQuarter", "2026-04-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        ("thisYear", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        ("lastYear", "2025-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        ("pastYear", "2025-09-08T00:00:00+00:00", "2026-09-09T00:00:00+00:00"),
        ("2026-09-03", "2026-09-03T00:00:00+00:00", "2026-09-04T00:00:00+00:00"),
        ("2026-09", "2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        ("2024-02", "2024-02-01T00:00:00+00:00", "2024-03-01T00:00:00+00:00"),  # leap year
        ("2026-12", "2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),  # year rollover
        ("2026", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        ("2026-09-01..2026-09-07", "2026-09-01T00:00:00+00:00", "2026-09-08T00:00:00+00:00"),
        ("2026-02..2026-03", "2026-02-01T00:00:00+00:00", "2026-04-01T00:00:00+00:00"),
    ],
)
def test_spans_are_half_open_midnight_aligned(text, start, end):
    assert _span(text) == (start, end)


def test_span_resolves_today_in_the_requested_zone():
    # 02:30 UTC on the 8th is still the evening of the 7th in New York.
    start, end = _span("today", "America/New_York")
    assert start == "2026-09-07T00:00:00-04:00" and end == "2026-09-08T00:00:00-04:00"


def test_lastmonth_across_a_year_boundary():
    s = h.resolve_span("lastMonth", datetime(2026, 1, 15, tzinfo=UTC))
    assert (s.start.month, s.start.year, s.end.month, s.end.year) == (12, 2025, 1, 2026)


def test_unknown_timezone_is_a_query_error():
    with pytest.raises(h.HisQueryError, match="unknown timezone"):
        h.resolve_span("today", NOW, "Mars/Olympus")


# --- intervals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text, seconds, months",
    [("15min", 900, None), ("1hr", 3600, None), ("2h", 7200, None), ("1day", 86400, None), ("1wk", 604800, None), ("30sec", 30, None), ("1mo", None, 1), ("3mo", None, 3), ("1yr", None, 12)],
)
def test_parse_interval(text, seconds, months):
    iv = h.parse_interval(text)
    assert (iv.seconds, iv.months) == (seconds, months)


# --- compiler, run against a real (in-memory) graph -------------------------

AHU = URIRef("urn:equip:fbf/ahu-1")
VAV = URIRef("urn:equip:fbf/vav-1")
HX_VAV = URIRef("urn:equip:haystack:VAV-9")
DAT = URIRef("urn:point:fbf/ahu-1/dat")
FAN = URIRef("urn:point:fbf/ahu-1/fan")
ZT = URIRef("urn:point:fbf/vav-1/zt")
HX_ZT = URIRef("urn:point:hx/VAV-9/zt")
B_RTU = URIRef("urn:equip:brick/rtu-7")
B_ZT = URIRef("urn:point:brick/rtu-7/zt")
B_FAN = URIRef("urn:point:brick/rtu-7/fan")
PROJ = Namespace("urn:timberdoodle:proj#")
TAG = Namespace("https://brickschema.org/schema/BrickTag#")


@pytest.fixture
def graph():
    s = Store()
    s.add_many([
        (AHU, HAYSTACK.hasTag, Literal("ahu")), (AHU, RDF.type, BRICK.Air_Handling_Unit),
        (AHU, BRICK.hasPoint, DAT), (AHU, BRICK.hasPoint, FAN),
        (VAV, HAYSTACK.hasTag, Literal("vav")), (VAV, BRICK.hasPoint, ZT), (VAV, HAYSTACK.dis, Literal("VAV 1")),
        (DAT, RDF.type, TD.RawPoint), (DAT, TD.sourceTopic, Literal("fbf/ahu-1/dat")), (DAT, BRICK.isPointOf, AHU),
        (DAT, HAYSTACK.hasTag, Literal("temp")), (DAT, HAYSTACK.hasTag, Literal("air")), (DAT, HAYSTACK.hasTag, Literal("discharge")),
        (DAT, HAYSTACK.unit, Literal("°F")), (DAT, RDF.type, BRICK.Discharge_Air_Temperature_Sensor),
        (FAN, RDF.type, TD.RawPoint), (FAN, BRICK.isPointOf, AHU), (FAN, HAYSTACK.hasTag, Literal("fan")),
        (FAN, HAYSTACK.kind, Literal("Bool")), (FAN, HAYSTACK.dis, Literal("Fan Status")),
        (ZT, RDF.type, TD.RawPoint), (ZT, BRICK.isPointOf, VAV), (ZT, HAYSTACK.hasTag, Literal("temp")),
        (ZT, HAYSTACK.hasTag, Literal("zone")), (ZT, HAYSTACK.temp, Literal(72.5)),
        # A Haystack-sourced point that only knows its equip via the equipRef
        # tag (no brick:isPointOf yet - link_equip_ref hasn't run).
        (HX_ZT, HAYSTACK.hasTag, Literal("point")), (HX_ZT, HAYSTACK.hasTag, Literal("zone")), (HX_ZT, HAYSTACK.equipRef, Literal("VAV-9")),
        (HX_VAV, HAYSTACK.hasTag, Literal("vav")), (HX_VAV, HAYSTACK.hasTag, Literal("equip")),
        # --- Brick-only: typed, never tagged (a loaded Brick model, or a
        # derivation_engine point) - the case "Brick is hard to query" is about.
        (B_RTU, RDF.type, BRICK.RTU),  # Brick's alias for Rooftop_Unit, itself under Air_Handling_Unit
        (B_RTU, BRICK.hasPoint, B_ZT), (B_RTU, BRICK.hasPoint, B_FAN),
        (B_ZT, RDF.type, BRICK.Zone_Air_Temperature_Sensor), (B_ZT, BRICK.isPointOf, B_RTU),
        # mapping.py's PROJ fallback: a project class one rdfs:subClassOf below a Brick class.
        (B_FAN, RDF.type, PROJ.Fan_Status_Variant), (B_FAN, BRICK.isPointOf, B_RTU),
        (PROJ.Fan_Status_Variant, RDFS.subClassOf, BRICK.Fan_Status),
        # Ontology-level subjects, as if Brick itself were loaded into the store:
        # schema, not entities - must never come back from a filter.
        (BRICK.Zone_Air_Temperature_Sensor, RDF.type, OWL.Class),
        (BRICK.Zone_Air_Temperature_Sensor, RDFS.subClassOf, BRICK.Air_Temperature_Sensor),
        (TAG.Temperature, RDF.type, BRICK.Tag),
    ])
    return s


def _match(graph, text):
    return h.match_entities(graph, h.parse_query(text).filter)


@pytest.mark.parametrize(
    "text, expected",
    [
        # --- Haystack markers and value tags, as written by ingest ---
        ("temp and not (air or zone)", []),
        ("discharge or fan", [DAT, FAN, B_FAN]),  # B_FAN: Fan_Status carries Brick tag:Fan
        ("temp > 70", [ZT]),
        ("temp > 80", []),
        ("temp != 72.5", []),  # a missing tag never satisfies a comparison
        ('dis == "Fan Status"', [FAN]),
        ('kind == "Bool"', [FAN]),
        ("id == @urn:equip:fbf/vav-1", [VAV]),
        ("equipRef == @urn:equip:fbf/ahu-1", [DAT, FAN]),  # full URI -> brick:isPointOf
        ("equipRef == @VAV-9", [HX_ZT]),  # bare Haystack id -> equipRef literal
        # --- the same words also reach Brick-only entities (B_*: typed, never tagged) ---
        ("ahu", [AHU, B_RTU]),  # marker on AHU; Brick tag:AHU on RTU (via Rooftop_Unit <- Air_Handling_Unit)
        ("temp and air", [DAT, B_ZT]),  # `temp` is Haystack for Brick's tag:Temperature
        ("temp and not air", [ZT]),
        ("zone and not temp", [HX_ZT]),
        ("zone and air and temp and sensor", [B_ZT]),  # every word from the class name Zone_Air_Temperature_Sensor
        ("temperature and sensor", [DAT, B_ZT]),  # Brick's own spelling; DAT is typed Discharge_Air_Temperature_Sensor
        ("fan and run", [B_FAN]),  # no Brick tag for `run`: rules/haystack_to_brick.yaml [fan, run, sensor] -> Fan_Status, inverted
        ("point", [DAT, FAN, ZT, HX_ZT, B_ZT, B_FAN]),
        ("equip", [AHU, VAV, HX_VAV, B_RTU]),
        ("point and id != @urn:point:fbf/ahu-1/dat", [FAN, ZT, HX_ZT, B_ZT, B_FAN]),
        ("point and equipRef != @urn:equip:fbf/ahu-1", [ZT, HX_ZT, B_ZT, B_FAN]),
        # --- Brick class names: hierarchy, aliases, case ---
        ("Air_Handling_Unit", [AHU, B_RTU]),  # B_RTU is typed brick:RTU, an alias of a *subclass*
        ("AHU", [AHU, B_RTU]),  # Brick's alias name for the class
        ("air_handling_unit", [AHU, B_RTU]),  # case-insensitive
        ("Rooftop_Unit", [B_RTU]),
        ("Temperature_Sensor", [DAT, B_ZT]),  # subclasses
        ("Zone_Air_Temperature_Sensor", [B_ZT]),
        ("Fan_Status", [B_FAN]),  # via the PROJ subclass the graph declares
        ("Fan_Status_Variant", [B_FAN]),  # the PROJ class by its own name
        ("Point", [DAT, B_ZT, B_FAN]),  # rdf:type-based; FAN/ZT/HX_ZT carry no Brick type
        ("Equipment", [AHU, B_RTU]),
        ("Sensor and not Temperature_Sensor", []),
        ("Not_A_Brick_Class", []),
    ],
)
def test_compiled_filter_selects_the_right_entities(graph, text, expected):
    assert _match(graph, text) == sorted(str(u) for u in expected)


def test_ontology_subjects_are_never_entities(graph):
    # brick:Zone_Air_Temperature_Sensor and tag:Temperature are in the graph
    # (as a loaded ontology would put them) - `Class`/`Tag` must not list them.
    for text in ("Class", "Tag", "Zone_Air_Temperature_Sensor", "temperature"):
        assert not any(uri.startswith("https://brickschema.org/") for uri in _match(graph, text)), text


def test_hints_name_near_miss_brick_vocabulary_only_when_nothing_matched():
    hints = h.hints_for_empty_result(h.parse_query("Air_Handeling_Unit and temperture and hisqRunMarker").filter)
    assert {hint["token"]: hint["suggestions"][0] for hint in hints} == {"Air_Handeling_Unit": "Air_Handling_Unit", "temperture": "temperature"}
    # A valid class, tag word, Haystack rule marker, or structural alias is never "corrected".
    assert h.hints_for_empty_result(h.parse_query("Air_Handling_Unit and temp and run and point").filter) == []


def test_compiled_sparql_never_walks_the_hierarchy_in_the_query(graph):
    # Oxigraph takes 1-50s on rdfs:subClassOf paths inside a filter with the
    # full ontology loaded; every hop is resolved in Python beforehand.
    sparql = h.compile_filter(h.parse_query("Temperature_Sensor and temperature").filter, h.graph_vocab(graph))
    assert "subClassOf" not in sparql and "EXISTS" not in sparql
    assert "Zone_Air_Temperature_Sensor" in sparql and "Fan_Status_Variant" not in sparql


def test_string_values_are_escaped_not_interpolated(graph):
    # A closing quote inside the value must not break out of the literal.
    assert _match(graph, 'dis == "x\\" } . ?s ?p ?o . FILTER(true) #"') == []


def test_expand_to_points_walks_equipment_and_keeps_direct_points(graph):
    assert h.expand_to_points(graph, [str(AHU), str(ZT)]) == sorted([str(DAT), str(FAN), str(ZT)])


def test_expand_to_points_follows_owl_sameas_merges(graph):
    from rdflib import OWL

    other = URIRef("urn:equip:haystack:AHU-1")
    graph.add_many([(AHU, OWL.sameAs, other), (other, OWL.sameAs, AHU), (other, BRICK.hasPoint, HX_ZT)])
    assert str(HX_ZT) in h.expand_to_points(graph, [str(AHU)])


def test_describe_points_metadata_shape(graph):
    meta = h.describe_points(graph, [str(DAT), str(FAN), "urn:point:nowhere"])
    dat = meta[str(DAT)]
    assert dat["dis"] == "dat"  # no dis tag -> last topic segment
    assert dat["point"] == "fbf/ahu-1/dat"
    assert dat["unit"] == "°F"
    assert dat["brickClass"] == "Discharge_Air_Temperature_Sensor"
    assert dat["tags"] == ["air", "discharge", "temp"]
    assert dat["equip"] == str(AHU) and dat["equipDis"] == "fbf/ahu-1"
    fan = meta[str(FAN)]
    assert fan["dis"] == "Fan Status" and fan["kind"] == "Bool" and fan["brickClass"] is None
    # Unknown point still gets a complete, null-filled record rather than a KeyError downstream.
    assert meta["urn:point:nowhere"]["dis"] == "nowhere" and meta["urn:point:nowhere"]["tags"] == []


def test_run_query_read_singular_with_no_match_is_nomatcherror(graph):
    class NoTs:  # never reached - the filter matches nothing first
        pass

    with pytest.raises(h.NoMatchError):
        h.run_query(graph, NoTs(), h.parse_query("read(nothing).hisRead(today)"), now=NOW)
