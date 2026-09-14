"""
The vocabulary is the agent's grounding - every Brick class it claims a
canonical type or point projects to must actually exist in the vendored
ontology, and the plain-English side (aliases, units, plausibility) must
behave the way a controls engineer would expect.
"""

import pytest

from timberdoodle import brick_vocab
from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.points import classify_object, label, signature


@pytest.fixture(scope="module")
def vocab():
    return brick_vocab.load()


def test_every_equipment_brick_class_exists_in_vendored_brick(vocab):
    for name, et in V.EQUIPMENT_TYPES.items():
        if et["brick"] is None:
            continue  # declared lossy on purpose (unit heater)
        assert vocab.canonical_class(et["brick"]) == et["brick"], f"{name}: brick:{et['brick']} is not in ontology/Brick-only.ttl"


def test_every_point_projection_brick_class_exists_in_vendored_brick(vocab):
    for (fn, role), proj in V.POINT_PROJECTIONS.items():
        if proj["brick"] is None:
            continue
        assert vocab.canonical_class(proj["brick"]) == proj["brick"], f"({fn}, {role}) -> brick:{proj['brick']} missing"


def test_every_equipment_type_has_a_haystack_marker_set():
    for name, et in V.EQUIPMENT_TYPES.items():
        assert et["haystack"], name
        assert "equip" in et["haystack"], name


@pytest.mark.parametrize("written,expected", [
    ("AHU", "air handling unit"), ("Air Handler", "air handling unit"), ("air-handling unit", "air handling unit"),
    ("VAV", "variable air volume box"), ("VAV Terminal", "variable air volume box"),
    ("RTU", "rooftop unit"), ("HP", "packaged heat pump"), ("heat pump", "packaged heat pump"),
    ("FCU", "fan coil unit"), ("CH", "chiller"), ("Boiler", "boiler"), ("EF", "exhaust fan"),
    ("Unit Heater", "unit heater"), ("kW meter", None), ("widget", None),
])
def test_canonical_type_folds_common_spellings(written, expected):
    assert V.canonical_type(written) == expected


@pytest.mark.parametrize("unit,expected", [
    ("°F", "degF"), ("deg F", "degF"), ("degreesFahrenheit", "degF"), ("F", "degF"),
    ("°C", "degC"), ("inH2O", "inH2O"), ("in wc", "inH2O"), ("inchesOfWater", "inH2O"),
    ("CFM", "cfm"), ("cubicFeetPerMinute", "cfm"), ("%", "%"), ("percent", "%"),
    ("noUnits", None), ("", None), (None, None),
])
def test_normalize_unit(unit, expected):
    assert V.normalize_unit(unit) == expected


def test_plausible_range_is_function_specific_then_quantity_generic():
    lo, hi = V.plausible_range("supply air temperature", "degF")
    assert lo < 55 < hi <= 150
    lo, hi = V.plausible_range("chilled water supply temperature", "degF")
    assert lo <= 44 <= hi < 100
    assert V.plausible_range("nonsense", "degF") is not None  # falls back to the quantity band
    assert V.plausible_range("supply air temperature", None) is None


def test_vendor_table_knows_the_common_bacnet_vendors_and_flags_gateways():
    assert V.vendor_info(8)["name"].startswith("Delta")
    assert V.vendor_info(5)["name"].startswith("Johnson")
    assert V.vendor_info(999) is not None
    assert V.vendor_info(424242) is None


def test_point_label_and_signature_are_stable_and_readable():
    pts = [classify_object("SA-T", "analogInput,1", "degF", 55.0), classify_object("SF-C", "binaryOutput,1")]
    assert label(pts[0]) == "supply air temperature sensor"
    assert label(pts[1]) == "supply fan command"
    assert signature(pts) == signature(list(reversed(pts)))
