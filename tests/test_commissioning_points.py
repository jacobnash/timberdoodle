"""
Point classification: function + role from the object name, the BACnet
object type as the tie-breaker for role, engineering units as a check
on the name (never a substitute for it), and honest confidence when the
name cannot be read.
"""

import pytest

from timberdoodle.commissioning.points import (
    classify_object,
    expand_words,
    object_type_of,
    tokenize,
)


def test_tokenize_splits_delimiters_and_camel_case():
    assert tokenize("AHU3_SupplyAirTemp") == ["ahu", "3", "supply", "air", "temp"]
    assert tokenize("ZN-T-SP") == ["zn", "t", "sp"]
    assert tokenize("SF/C") == ["sf", "c"]


def test_expand_words_records_which_abbreviations_it_used():
    words, evidence = expand_words(["sa", "t", "sp"])
    assert "supply" in words and "air" in words and "setpoint" in words
    assert any("sa" in e for e in evidence)


@pytest.mark.parametrize("name,oid,units,fn,role", [
    ("SA-T", "analogInput,1", "degF", "supply air temperature", "sensor"),
    ("Supply Air Temp", "analogInput,1", None, "supply air temperature", "sensor"),
    ("DAT", "analogInput,4", None, "supply air temperature", "sensor"),
    ("SA-T-SP", "analogValue,1", "degF", "supply air temperature", "setpoint"),
    ("SAT Setpoint", None, None, "supply air temperature", "setpoint"),
    ("RA-T", "analogInput,3", None, "return air temperature", "sensor"),
    ("MAT", "analogInput,5", None, "mixed air temperature", "sensor"),
    ("OAT", "analogInput,6", None, "outside air temperature", "sensor"),
    ("ZN-T", "analogInput,1", None, "zone air temperature", "sensor"),
    ("Zone Temp Setpoint", None, None, "zone air temperature", "setpoint"),
    ("SF-S", "binaryInput,1", None, "supply fan", "status"),
    ("SF-C", "binaryOutput,1", None, "supply fan", "command"),
    ("Supply Fan Status", None, None, "supply fan", "status"),
    ("SF Speed", "analogOutput,2", "%", "supply fan speed", "command"),
    ("DSP", "analogInput,2", "inH2O", "supply air static pressure", "sensor"),
    ("Duct Static Pressure", None, None, "supply air static pressure", "sensor"),
    ("DMPR-POS", "analogOutput,1", "%", "damper position", "command"),
    ("FLOW", "analogInput,2", "cfm", "flow", "sensor"),
    ("Airflow", None, "cfm", "flow", "sensor"),  # which air is not in the name - not invented
    ("Supply Airflow", None, "cfm", "supply air flow", "sensor"),
    ("CHWS Temp", "analogInput,1", None, "chilled water supply temperature", "sensor"),
    ("CHWR Temp", "analogInput,2", None, "chilled water return temperature", "sensor"),
    ("Chiller Enable", "binaryOutput,1", None, "unit", "command"),
    ("Chiller Status", "binaryInput,1", None, "unit", "status"),
    ("AHU3 Run", "binaryInput,2", None, "unit", "status"),
    ("VAV Damper", "analogOutput,1", "%", "damper position", "command"),
    ("RV", "binaryOutput,3", None, "reversing valve", "command"),
    ("Filter DP", "analogInput,7", "inH2O", "filter differential pressure", "sensor"),
])
def test_classify_common_names(name, oid, units, fn, role):
    p = classify_object(name, oid, units)
    assert p["function"] == fn, p
    assert p["role"] == role, p


def test_role_word_beats_object_type_but_object_type_tempers_it():
    # "setpoint" on an analogInput cannot be written - it is a sensor of a setpoint, and says so
    p = classify_object("ZN-T-SP", "analogInput,9")
    assert p["role"] == "sensor"
    assert any("input" in n.lower() for n in p["notes"])
    # "status" on a binaryOutput is really the command
    p = classify_object("SF Status", "binaryOutput,2")
    assert p["role"] == "command"


def test_units_that_contradict_the_name_lower_confidence_and_leave_a_note():
    clean = classify_object("SA-T", "analogInput,1", "degF", 55.0)
    wrong = classify_object("SA-T", "analogInput,1", "cfm", 55.0)
    assert wrong["confidence"] < clean["confidence"]
    assert any("contradict" in n for n in wrong["notes"])
    assert not any("contradict" in n for n in clean["notes"])


def test_present_value_plausibility_is_judged_against_the_function_band():
    assert classify_object("SA-T", "analogInput,1", "degF", 55.0)["plausibility"] == "plausible"
    assert classify_object("SA-T", "analogInput,1", "degF", 412.0)["plausibility"] == "implausible"
    assert classify_object("SA-T", "analogInput,1", None, 55.0)["plausibility"] in (None, "unknown")


def test_unreadable_name_is_never_invented():
    p = classify_object("AI_7", "analogInput,7")
    assert p["function"] is None
    assert p["role"] == "sensor"  # object type still tells the direction
    assert p["confidence"] <= 0.2
    q = classify_object(None, None)
    assert q["function"] is None and q["role"] is None


def test_object_type_of_parses_bacnet_identifiers():
    assert object_type_of("analogInput,1") == "analoginput"
    assert object_type_of("binary-output:3") == "binaryoutput"
    assert object_type_of("AI 7") == "analoginput"
    assert object_type_of(None) is None
