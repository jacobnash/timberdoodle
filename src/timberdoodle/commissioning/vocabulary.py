"""
The canonical, ontology-neutral vocabulary every other commissioning
module speaks. Plain English throughout - `air handling unit`, `supply
air temperature` - never `brick:AHU` or `hs:discharge-air-temp`. Brick
and Haystack spellings appear here only as *projection hints* (the
`brick`/`haystack` fields) so projection.py can translate out of the
canonical model; they are never the storage format.

Three kinds of knowledge live here, all of it the sort of thing a
controls integrator carries around in their head:

  * EQUIPMENT_TYPES - what each kind of equipment is expected to expose
    (its point signature), which points are distinctive enough to tell
    look-alikes apart (a reversing valve command rules out a plain RTU),
    and the abbreviations specs and vendors use for it.
  * The point lexicon (ABBREVIATIONS, LOCATION_WORDS, ...) - how field
    object names are actually spelled, so `SA-T`, `SAT`, `sa_temp` and
    `Supply Air Temp` all resolve to the same function.
  * PLAUSIBLE_RANGES - what a value of a given function/unit can
    reasonably read. 55 is a plausible supply air temperature; 210 is
    not.

Every Brick class named in a projection hint was checked against the
vendored ontology/Brick-only.ttl (see tests/test_commissioning_vocabulary.py)
- projection.py also re-checks at run time and reports, rather than
invents, anything Brick can't express.
"""

from __future__ import annotations

from typing import TypedDict

# --- roles ------------------------------------------------------------------

# The four things a BACnet object can be *for*. Field naming routinely
# conflates them; confusing a setpoint for a sensor corrupts every analytic
# downstream, so classification keeps them strictly apart.
ROLE_SENSOR = "sensor"
ROLE_SETPOINT = "setpoint"
ROLE_COMMAND = "command"
ROLE_STATUS = "status"
ROLES = (ROLE_SENSOR, ROLE_SETPOINT, ROLE_COMMAND, ROLE_STATUS)


class PointProjection(TypedDict, total=False):
    brick: str | None  # Brick class local name, or None when Brick has no class for it
    haystack: list[str]  # Haystack v4 marker tags


class EquipmentType(TypedDict, total=False):
    description: str
    aliases: list[str]  # tag prefixes / abbreviations as specs and vendors write them
    # Point signature, as (function, role) pairs. role "*" = any role.
    required: list[tuple[str, str]]  # a device missing most of these is not this type
    typical: list[tuple[str, str]]  # commonly present, supporting evidence
    distinctive: list[tuple[str, str]]  # strongly indicates this type over look-alikes
    contradicting: list[tuple[str, str]]  # presence argues *against* this type
    terminal: bool  # a terminal unit (served by an upstream air/water source)
    brick: str | None
    haystack: list[str]


# Canonical type name -> what it is. Names are deliberately the words an
# engineer says out loud, not a schema's identifiers.
EQUIPMENT_TYPES: dict[str, EquipmentType] = {
    "air handling unit": {
        "description": "Central built-up or modular air handler: fan(s), coils, mixing/economizer section.",
        "aliases": ["AHU", "AH", "MAU", "AHU-", "AC"],
        "required": [("supply air temperature", ROLE_SENSOR), ("supply fan", "*")],
        "typical": [
            ("return air temperature", ROLE_SENSOR),
            ("mixed air temperature", ROLE_SENSOR),
            ("outside air temperature", ROLE_SENSOR),
            ("outside air damper position", ROLE_COMMAND),
            ("cooling valve position", ROLE_COMMAND),
            ("heating valve position", ROLE_COMMAND),
            ("supply air static pressure", ROLE_SENSOR),
            ("supply air static pressure", ROLE_SETPOINT),
            ("supply air temperature", ROLE_SETPOINT),
            ("supply fan speed", ROLE_COMMAND),
            ("return fan", "*"),
        ],
        "distinctive": [("mixed air temperature", ROLE_SENSOR), ("supply air static pressure", "*")],
        "contradicting": [("reversing valve", ROLE_COMMAND), ("compressor", "*")],
        "terminal": False,
        "brick": "Air_Handling_Unit",
        "haystack": ["ahu", "equip"],
    },
    "rooftop unit": {
        "description": "Packaged DX rooftop unit: fan, compressor stage(s), economizer, gas or electric heat.",
        "aliases": ["RTU", "RT", "PU", "ACU"],
        "required": [("supply air temperature", ROLE_SENSOR), ("supply fan", "*"), ("compressor", "*")],
        "typical": [
            ("return air temperature", ROLE_SENSOR),
            ("outside air temperature", ROLE_SENSOR),
            ("outside air damper position", ROLE_COMMAND),
            ("heating", ROLE_COMMAND),
            ("cooling", ROLE_COMMAND),
            ("zone air temperature", ROLE_SENSOR),
        ],
        "distinctive": [("compressor", "*")],
        "contradicting": [("reversing valve", ROLE_COMMAND), ("chilled water valve position", ROLE_COMMAND)],
        "terminal": False,
        "brick": "Rooftop_Unit",
        "haystack": ["rtu", "ahu", "equip"],
    },
    "packaged heat pump": {
        "description": "Packaged air-source heat pump - like an RTU but reverses its refrigerant cycle for heating.",
        "aliases": ["HP", "PHP", "WSHP", "ASHP"],
        "required": [("supply air temperature", ROLE_SENSOR), ("supply fan", "*"), ("reversing valve", ROLE_COMMAND)],
        "typical": [("compressor", "*"), ("zone air temperature", ROLE_SENSOR), ("zone air temperature", ROLE_SETPOINT)],
        "distinctive": [("reversing valve", ROLE_COMMAND)],
        "contradicting": [("chilled water valve position", ROLE_COMMAND), ("mixed air temperature", ROLE_SENSOR)],
        "terminal": False,
        "brick": "Packaged_Heat_Pump",
        "haystack": ["heatPump", "equip"],
    },
    "variable air volume box": {
        "description": "VAV terminal: damper modulating primary air to a zone, often with reheat.",
        "aliases": ["VAV", "V", "TU", "VVT", "VAVR"],
        "required": [("zone air temperature", ROLE_SENSOR), ("damper position", ROLE_COMMAND)],
        "typical": [
            ("zone air temperature", ROLE_SETPOINT),
            ("supply air flow", ROLE_SENSOR),
            ("supply air flow", ROLE_SETPOINT),
            ("heating valve position", ROLE_COMMAND),
            ("supply air temperature", ROLE_SENSOR),
            ("occupancy", ROLE_STATUS),
        ],
        "distinctive": [("supply air flow", ROLE_SETPOINT), ("damper position", ROLE_COMMAND)],
        "contradicting": [("compressor", "*"), ("mixed air temperature", ROLE_SENSOR)],
        "terminal": True,
        "brick": "Variable_Air_Volume_Box",
        "haystack": ["vav", "equip"],
    },
    "fan coil unit": {
        "description": "Fan coil: small fan plus hydronic coil(s) serving one zone; no primary-air damper.",
        "aliases": ["FCU", "FC"],
        "required": [("zone air temperature", ROLE_SENSOR), ("supply fan", "*")],
        "typical": [
            ("zone air temperature", ROLE_SETPOINT),
            ("cooling valve position", ROLE_COMMAND),
            ("heating valve position", ROLE_COMMAND),
            ("supply fan speed", ROLE_COMMAND),
        ],
        "distinctive": [("supply fan speed", ROLE_COMMAND)],
        "contradicting": [("damper position", ROLE_COMMAND), ("supply air flow", "*"), ("compressor", "*")],
        "terminal": True,
        "brick": "Fan_Coil_Unit",
        "haystack": ["fcu", "equip"],
    },
    "chiller": {
        "description": "Chiller producing chilled water.",
        "aliases": ["CH", "CHLR", "CHL"],
        "required": [("chilled water supply temperature", ROLE_SENSOR), ("unit", "*")],
        "typical": [("chilled water return temperature", ROLE_SENSOR), ("electric power", ROLE_SENSOR), ("chilled water supply temperature", ROLE_SETPOINT)],
        "distinctive": [("chilled water supply temperature", ROLE_SENSOR), ("chilled water return temperature", ROLE_SENSOR)],
        "contradicting": [("supply air temperature", ROLE_SENSOR), ("damper position", "*")],
        "terminal": False,
        "brick": "Chiller",
        "haystack": ["chiller", "equip"],
    },
    "boiler": {
        "description": "Boiler producing hot water (or steam).",
        "aliases": ["B", "BLR", "HWB"],
        "required": [("hot water supply temperature", ROLE_SENSOR), ("unit", "*")],
        "typical": [("hot water return temperature", ROLE_SENSOR), ("hot water supply temperature", ROLE_SETPOINT)],
        "distinctive": [("hot water supply temperature", ROLE_SENSOR), ("hot water return temperature", ROLE_SENSOR)],
        "contradicting": [("supply air temperature", ROLE_SENSOR), ("damper position", "*")],
        "terminal": False,
        "brick": "Boiler",
        "haystack": ["boiler", "equip"],
    },
    "pump": {
        "description": "Pump (chilled, hot, or condenser water).",
        "aliases": ["P", "CHWP", "HWP", "CWP", "PMP"],
        "required": [("pump", "*")],
        "typical": [("pump speed", ROLE_COMMAND), ("differential pressure", ROLE_SENSOR)],
        "distinctive": [("pump", ROLE_STATUS)],
        "contradicting": [("supply air temperature", ROLE_SENSOR), ("zone air temperature", ROLE_SENSOR)],
        "terminal": False,
        "brick": "Pump",
        "haystack": ["pump", "equip"],
    },
    "exhaust fan": {
        "description": "Stand-alone exhaust fan.",
        "aliases": ["EF", "EXF"],
        "required": [("exhaust fan", "*")],
        "typical": [("exhaust fan speed", ROLE_COMMAND)],
        "distinctive": [("exhaust fan", ROLE_COMMAND)],
        "contradicting": [("supply air temperature", ROLE_SENSOR), ("zone air temperature", ROLE_SENSOR)],
        "terminal": False,
        "brick": "Exhaust_Fan",
        "haystack": ["exhaust", "fan", "equip"],
    },
    "electrical meter": {
        "description": "Electric meter.",
        "aliases": ["EM", "MTR", "KWH", "PM"],
        "required": [("electric power", ROLE_SENSOR)],
        "typical": [("electric energy", ROLE_SENSOR)],
        "distinctive": [("electric energy", ROLE_SENSOR)],
        "contradicting": [("supply air temperature", ROLE_SENSOR), ("damper position", "*")],
        "terminal": False,
        "brick": "Electrical_Meter",
        "haystack": ["elec", "meter", "equip"],
    },
    "cooling tower": {
        "description": "Cooling tower rejecting condenser water heat.",
        "aliases": ["CT", "TWR"],
        "required": [("condenser water supply temperature", ROLE_SENSOR), ("tower fan", "*")],
        "typical": [("condenser water return temperature", ROLE_SENSOR), ("tower fan speed", ROLE_COMMAND)],
        "distinctive": [("condenser water supply temperature", ROLE_SENSOR)],
        "contradicting": [("supply air temperature", ROLE_SENSOR), ("zone air temperature", ROLE_SENSOR)],
        "terminal": False,
        "brick": "Cooling_Tower",
        "haystack": ["coolingTower", "equip"],
    },
    "unit heater": {
        "description": "Unit heater - fan and heating element or coil, one space, no cooling.",
        "aliases": ["UH", "CUH", "HUH"],
        "required": [("zone air temperature", ROLE_SENSOR), ("heating", "*")],
        "typical": [("supply fan", "*"), ("zone air temperature", ROLE_SETPOINT)],
        "distinctive": [],
        "contradicting": [("cooling valve position", "*"), ("compressor", "*"), ("damper position", "*")],
        "terminal": True,
        "brick": None,  # Brick 1.4 has no Unit_Heater class - projection reports this as lossy
        "haystack": ["unitHeater", "equip"],
    },
}

# Spec-written type words -> canonical type. Aliases from EQUIPMENT_TYPES
# are folded in at import time below; these are the longer spellings.
TYPE_SYNONYMS: dict[str, str] = {
    "air handler": "air handling unit",
    "air handling unit": "air handling unit",
    "ahu": "air handling unit",
    "makeup air unit": "air handling unit",
    "make-up air unit": "air handling unit",
    "rooftop": "rooftop unit",
    "rooftop unit": "rooftop unit",
    "packaged rooftop unit": "rooftop unit",
    "heat pump": "packaged heat pump",
    "packaged heat pump": "packaged heat pump",
    "water source heat pump": "packaged heat pump",
    "vav": "variable air volume box",
    "vav box": "variable air volume box",
    "vav terminal": "variable air volume box",
    "variable air volume": "variable air volume box",
    "variable air volume box": "variable air volume box",
    "terminal unit": "variable air volume box",
    "fan coil": "fan coil unit",
    "fan coil unit": "fan coil unit",
    "chiller": "chiller",
    "boiler": "boiler",
    "pump": "pump",
    "exhaust fan": "exhaust fan",
    "meter": "electrical meter",
    "electric meter": "electrical meter",
    "electrical meter": "electrical meter",
    "power meter": "electrical meter",
    "cooling tower": "cooling tower",
    "unit heater": "unit heater",
    "cabinet unit heater": "unit heater",
}
for _name, _t in EQUIPMENT_TYPES.items():
    for _alias in _t.get("aliases", []):
        TYPE_SYNONYMS.setdefault(_alias.lower().rstrip("-"), _name)


def canonical_type(written: str | None) -> str | None:
    """`AHU` / `Air Handler` / `rooftop` -> canonical type; None when the
    spec's word isn't one this vocabulary knows (kept as written, flagged,
    never guessed)."""
    if not written:
        return None
    key = " ".join(written.lower().replace("_", " ").replace("-", " ").split())
    if key in TYPE_SYNONYMS:
        return TYPE_SYNONYMS[key]
    if key in EQUIPMENT_TYPES:
        return key
    return None


# --- point lexicon ------------------------------------------------------------

# Abbreviation tokens (lower-cased) -> the plain words they stand for.
# Verified against how the major BAS vendors actually name things
# (JCI Metasys "SA-T", Siemens "SAT", ALC "sa_temp", Trane "Disch Air Temp",
# Distech/Niagara mixed) rather than a single convention.
ABBREVIATIONS: dict[str, list[str]] = {
    # combined temperature abbreviations
    "sat": ["supply", "air", "temperature"],
    "dat": ["supply", "air", "temperature"],  # "discharge" is the same concept in plain English
    "sa-t": ["supply", "air", "temperature"],
    "rat": ["return", "air", "temperature"],
    "mat": ["mixed", "air", "temperature"],
    "oat": ["outside", "air", "temperature"],
    "eat": ["exhaust", "air", "temperature"],
    "znt": ["zone", "temperature"],
    "zt": ["zone", "temperature"],
    "rmt": ["zone", "temperature"],
    "chwst": ["chilled", "water", "supply", "temperature"],
    "chwrt": ["chilled", "water", "return", "temperature"],
    "hwst": ["hot", "water", "supply", "temperature"],
    "hwrt": ["hot", "water", "return", "temperature"],
    "cwst": ["condenser", "water", "supply", "temperature"],
    "cwrt": ["condenser", "water", "return", "temperature"],
    # locations / media
    "sa": ["supply", "air"],
    "da": ["supply", "air"],
    "disch": ["supply", "air"],
    "discharge": ["supply", "air"],
    "supply": ["supply"],
    "ra": ["return", "air"],
    "return": ["return"],
    "ma": ["mixed", "air"],
    "mixed": ["mixed"],
    "oa": ["outside", "air"],
    "outdoor": ["outside"],
    "outside": ["outside"],
    "osa": ["outside", "air"],
    "ea": ["exhaust", "air"],
    "exh": ["exhaust"],
    "exhaust": ["exhaust"],
    "zn": ["zone"],
    "zone": ["zone"],
    "space": ["zone"],
    "rm": ["zone"],
    "room": ["zone"],
    "chw": ["chilled", "water"],
    "chws": ["chilled", "water", "supply"],
    "chwr": ["chilled", "water", "return"],
    "hw": ["hot", "water"],
    "hws": ["hot", "water", "supply"],
    "hwr": ["hot", "water", "return"],
    "cw": ["condenser", "water"],
    "cws": ["condenser", "water", "supply"],
    "cwr": ["condenser", "water", "return"],
    "cdw": ["condenser", "water"],
    # quantities
    "temp": ["temperature"],
    "tmp": ["temperature"],
    "temperature": ["temperature"],
    "rh": ["humidity"],
    "hum": ["humidity"],
    "humidity": ["humidity"],
    "co2": ["co2"],
    "press": ["pressure"],
    "pressure": ["pressure"],
    "prs": ["pressure"],
    "static": ["static", "pressure"],
    "sp": ["setpoint"],  # far more often "setpoint" than "static pressure" in object names; "static"/"dsp" carry that meaning
    "dsp": ["supply", "air", "static", "pressure"],
    "sasp": ["supply", "air", "static", "pressure"],
    "dp": ["differential", "pressure"],
    "flow": ["flow"],
    "airflow": ["flow"],
    "cfm": ["flow"],
    "gpm": ["flow"],
    "pos": ["position"],
    "position": ["position"],
    "fdbk": ["position", "status"],
    "feedback": ["position", "status"],
    "spd": ["speed"],
    "speed": ["speed"],
    "vfd": ["speed"],
    "kw": ["power"],
    "power": ["power"],
    "kwh": ["energy"],
    "energy": ["energy"],
    "occ": ["occupancy"],
    "occupancy": ["occupancy"],
    "occupied": ["occupancy"],
    # equipment parts
    "sf": ["supply", "fan"],
    "rf": ["return", "fan"],
    "ef": ["exhaust", "fan"],
    "fan": ["fan"],
    "comp": ["compressor"],
    "compressor": ["compressor"],
    "rv": ["reversing", "valve"],
    "rev": ["reversing"],
    "reversing": ["reversing"],
    "vlv": ["valve"],
    "valve": ["valve"],
    "dmpr": ["damper"],
    "oad": ["outside", "air", "damper"],
    "dpr": ["damper"],
    "dmp": ["damper"],
    "damper": ["damper"],
    "clg": ["cooling"],
    "cool": ["cooling"],
    "cooling": ["cooling"],
    "htg": ["heating"],
    "heat": ["heating"],
    "heating": ["heating"],
    "reheat": ["heating"],
    "rht": ["heating"],
    "pmp": ["pump"],
    "pump": ["pump"],
    "fltr": ["filter"],
    "filter": ["filter"],
    "econ": ["economizer"],
    "twr": ["tower"],
    "tower": ["tower"],
    # roles
    "setpoint": ["setpoint"],
    "stpt": ["setpoint"],
    "setpt": ["setpoint"],
    "set": ["setpoint"],
    "cmd": ["command"],
    "command": ["command"],
    "ss": ["command"],  # start/stop
    "en": ["command"],
    "enable": ["command"],
    "start": ["command"],
    "out": ["command"],
    "sts": ["status"],
    "stat": ["status"],
    "status": ["status"],
    "run": ["status"],
    "proof": ["status"],
    "alm": ["alarm"],
    "alarm": ["alarm"],
    "fail": ["alarm"],
}

# Single letters that only mean something after a location abbreviation
# (`SA_T`, `ZN-T`, `RA T`): "t" alone is too ambiguous to trust.
TRAILING_LETTER_QUANTITIES = {"t": "temperature", "h": "humidity", "p": "pressure", "f": "flow"}
LOCATION_PREFIX_WORDS = {"supply", "return", "mixed", "outside", "exhaust", "zone", "chilled", "hot", "condenser"}

# Word set patterns -> canonical function. Checked most-specific first (by
# number of words). The role is decided separately - a function here is the
# *thing measured or controlled*, never whether it's a sensor or setpoint.
FUNCTION_PATTERNS: list[tuple[frozenset[str], str]] = [
    (frozenset({"chilled", "water", "supply", "temperature"}), "chilled water supply temperature"),
    (frozenset({"chilled", "water", "return", "temperature"}), "chilled water return temperature"),
    (frozenset({"hot", "water", "supply", "temperature"}), "hot water supply temperature"),
    (frozenset({"hot", "water", "return", "temperature"}), "hot water return temperature"),
    (frozenset({"condenser", "water", "supply", "temperature"}), "condenser water supply temperature"),
    (frozenset({"condenser", "water", "return", "temperature"}), "condenser water return temperature"),
    (frozenset({"supply", "air", "static", "pressure"}), "supply air static pressure"),
    (frozenset({"supply", "static", "pressure"}), "supply air static pressure"),
    (frozenset({"filter", "differential", "pressure"}), "filter differential pressure"),
    (frozenset({"filter", "pressure"}), "filter differential pressure"),
    (frozenset({"outside", "air", "damper"}), "outside air damper position"),
    (frozenset({"outside", "damper"}), "outside air damper position"),
    (frozenset({"supply", "air", "temperature"}), "supply air temperature"),
    (frozenset({"return", "air", "temperature"}), "return air temperature"),
    (frozenset({"mixed", "air", "temperature"}), "mixed air temperature"),
    (frozenset({"outside", "air", "temperature"}), "outside air temperature"),
    (frozenset({"exhaust", "air", "temperature"}), "exhaust air temperature"),
    (frozenset({"zone", "air", "temperature"}), "zone air temperature"),
    (frozenset({"supply", "air", "flow"}), "supply air flow"),
    (frozenset({"supply", "air", "humidity"}), "supply air humidity"),
    (frozenset({"return", "air", "humidity"}), "return air humidity"),
    (frozenset({"outside", "air", "humidity"}), "outside air humidity"),
    (frozenset({"zone", "air", "humidity"}), "zone air humidity"),
    (frozenset({"chilled", "water", "valve"}), "chilled water valve position"),
    (frozenset({"hot", "water", "valve"}), "heating valve position"),
    (frozenset({"cooling", "valve"}), "cooling valve position"),
    (frozenset({"heating", "valve"}), "heating valve position"),
    (frozenset({"reversing", "valve"}), "reversing valve"),
    (frozenset({"supply", "fan", "speed"}), "supply fan speed"),
    (frozenset({"return", "fan", "speed"}), "return fan speed"),
    (frozenset({"exhaust", "fan", "speed"}), "exhaust fan speed"),
    (frozenset({"tower", "fan", "speed"}), "tower fan speed"),
    (frozenset({"pump", "speed"}), "pump speed"),
    (frozenset({"supply", "fan"}), "supply fan"),
    (frozenset({"return", "fan"}), "return fan"),
    (frozenset({"exhaust", "fan"}), "exhaust fan"),
    (frozenset({"tower", "fan"}), "tower fan"),
    (frozenset({"supply", "temperature"}), "supply air temperature"),
    (frozenset({"return", "temperature"}), "return air temperature"),
    (frozenset({"mixed", "temperature"}), "mixed air temperature"),
    (frozenset({"outside", "temperature"}), "outside air temperature"),
    (frozenset({"zone", "temperature"}), "zone air temperature"),
    (frozenset({"zone", "humidity"}), "zone air humidity"),
    (frozenset({"zone", "co2"}), "zone co2"),
    (frozenset({"return", "co2"}), "return air co2"),
    (frozenset({"static", "pressure"}), "supply air static pressure"),
    (frozenset({"differential", "pressure"}), "differential pressure"),
    (frozenset({"damper", "position"}), "damper position"),
    (frozenset({"valve", "position"}), "valve position"),
    (frozenset({"fan", "speed"}), "supply fan speed"),
    (frozenset({"electric", "power"}), "electric power"),
    (frozenset({"electric", "energy"}), "electric energy"),
    (frozenset({"compressor"}), "compressor"),
    (frozenset({"damper"}), "damper position"),
    (frozenset({"valve"}), "valve position"),
    (frozenset({"cooling"}), "cooling"),
    (frozenset({"heating"}), "heating"),
    (frozenset({"pump"}), "pump"),
    (frozenset({"fan"}), "supply fan"),
    (frozenset({"co2"}), "zone co2"),
    (frozenset({"humidity"}), "humidity"),
    (frozenset({"temperature"}), "temperature"),
    (frozenset({"flow"}), "flow"),
    (frozenset({"pressure"}), "pressure"),
    (frozenset({"occupancy"}), "occupancy"),
    (frozenset({"power"}), "electric power"),
    (frozenset({"energy"}), "electric energy"),
    (frozenset({"speed"}), "speed"),
    (frozenset({"position"}), "position"),
    (frozenset({"alarm"}), "alarm"),
]

# Functions whose natural role is on/off rather than a measured quantity -
# a bare "SF" with a binary object type is the fan's command or status, and
# a "temperature" with no role word on an analog input is a sensor.
ONOFF_FUNCTIONS = {
    "supply fan", "return fan", "exhaust fan", "tower fan", "compressor", "pump", "unit",
    "cooling", "heating", "reversing valve", "occupancy", "alarm",
}

# Function -> the physical quantity it measures, for unit cross-checks.
FUNCTION_QUANTITY: dict[str, str] = {}
for _fn in [p[1] for p in FUNCTION_PATTERNS]:
    if "temperature" in _fn:
        FUNCTION_QUANTITY[_fn] = "temperature"
    elif "humidity" in _fn:
        FUNCTION_QUANTITY[_fn] = "humidity"
    elif "pressure" in _fn:
        FUNCTION_QUANTITY[_fn] = "pressure"
    elif "flow" in _fn:
        FUNCTION_QUANTITY[_fn] = "flow"
    elif "co2" in _fn:
        FUNCTION_QUANTITY[_fn] = "co2"
    elif "position" in _fn or "speed" in _fn:
        FUNCTION_QUANTITY[_fn] = "percent"
    elif _fn == "electric power":
        FUNCTION_QUANTITY[_fn] = "power"
    elif _fn == "electric energy":
        FUNCTION_QUANTITY[_fn] = "energy"
    elif _fn in ONOFF_FUNCTIONS:
        FUNCTION_QUANTITY[_fn] = "onoff"

# --- units --------------------------------------------------------------------

# BACnet engineering-units enumeration names, Haystack unit symbols, and the
# way people type them -> a canonical unit symbol.
UNIT_ALIASES: dict[str, str | None] = {
    "degreesfahrenheit": "degF", "degf": "degF", "deg f": "degF", "°f": "degF", "f": "degF", "fahrenheit": "degF",
    "degreescelsius": "degC", "degc": "degC", "deg c": "degC", "°c": "degC", "c": "degC", "celsius": "degC",
    "percent": "%", "%": "%", "pct": "%", "percentrelativehumidity": "%RH", "%rh": "%RH",
    "pascals": "Pa", "pa": "Pa", "kilopascals": "kPa", "kpa": "kPa",
    "inchesofwater": "inH2O", "inh2o": "inH2O", "in wc": "inH2O", "inwc": "inH2O", "in. w.c.": "inH2O", "inh₂o": "inH2O",
    "cubicfeetperminute": "cfm", "cfm": "cfm", "litersperseconds": "L/s", "l/s": "L/s", "cubicmeterspersecond": "m3/s",
    "gallonsperminute": "gpm", "gpm": "gpm",
    "partspermillion": "ppm", "ppm": "ppm",
    "kilowatts": "kW", "kw": "kW", "watts": "W", "w": "W",
    "kilowatthours": "kWh", "kwh": "kWh",
    "hertz": "Hz", "hz": "Hz",
    "revolutionsperminute": "rpm", "rpm": "rpm",
    "nounits": None, "no units": None, "": None,
}

UNIT_QUANTITY: dict[str, str] = {
    "degF": "temperature", "degC": "temperature",
    "%": "percent", "%RH": "humidity",
    "Pa": "pressure", "kPa": "pressure", "inH2O": "pressure",
    "cfm": "flow", "L/s": "flow", "m3/s": "flow", "gpm": "flow",
    "ppm": "co2",
    "kW": "power", "W": "power", "kWh": "energy",
    "Hz": "percent", "rpm": "percent",
}


def normalize_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    key = str(unit).strip().lower()
    if key in UNIT_ALIASES:
        return UNIT_ALIASES[key]
    return str(unit).strip() or None


# --- plausibility -------------------------------------------------------------

# (function, canonical unit) -> (low, high). Deliberately generous: these
# catch a 210 degF "supply air" or a -40 zone, not tune-ups. Missing pairs
# fall back to the quantity-level ranges below.
PLAUSIBLE_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    ("supply air temperature", "degF"): (35.0, 140.0),
    ("return air temperature", "degF"): (40.0, 110.0),
    ("mixed air temperature", "degF"): (20.0, 110.0),
    ("zone air temperature", "degF"): (40.0, 110.0),
    ("outside air temperature", "degF"): (-60.0, 130.0),
    ("exhaust air temperature", "degF"): (30.0, 120.0),
    ("chilled water supply temperature", "degF"): (30.0, 75.0),
    ("chilled water return temperature", "degF"): (35.0, 90.0),
    ("hot water supply temperature", "degF"): (50.0, 230.0),
    ("hot water return temperature", "degF"): (50.0, 220.0),
    ("condenser water supply temperature", "degF"): (40.0, 110.0),
    ("condenser water return temperature", "degF"): (40.0, 120.0),
    ("supply air static pressure", "inH2O"): (-2.0, 10.0),
    ("supply air static pressure", "Pa"): (-500.0, 2500.0),
    ("zone co2", "ppm"): (250.0, 5000.0),
    ("return air co2", "ppm"): (250.0, 5000.0),
}
QUANTITY_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "temperature": {"degF": (-60.0, 300.0), "degC": (-50.0, 150.0)},
    "humidity": {"%": (0.0, 100.0), "%RH": (0.0, 100.0)},
    "percent": {"%": (0.0, 100.0), "Hz": (0.0, 120.0), "rpm": (0.0, 5000.0)},
    "pressure": {"inH2O": (-10.0, 50.0), "Pa": (-2500.0, 12500.0), "kPa": (-10.0, 2000.0)},
    "flow": {"cfm": (0.0, 500000.0), "L/s": (0.0, 250000.0), "gpm": (0.0, 50000.0), "m3/s": (0.0, 250.0)},
    "co2": {"ppm": (0.0, 10000.0)},
    "power": {"kW": (0.0, 1e6), "W": (0.0, 1e9)},
    "energy": {"kWh": (0.0, 1e12)},
}


def _f_to_c(lo_hi: tuple[float, float]) -> tuple[float, float]:
    lo, hi = lo_hi
    return ((lo - 32.0) * 5.0 / 9.0, (hi - 32.0) * 5.0 / 9.0)


def plausible_range(function: str | None, unit: str | None) -> tuple[float, float] | None:
    """The reasonable reading band for a function+unit, or None when this
    vocabulary has no opinion (then the point is 'unknown', not judged)."""
    if unit is None:
        return None
    if function is not None:
        if (function, unit) in PLAUSIBLE_RANGES:
            return PLAUSIBLE_RANGES[(function, unit)]
        if unit == "degC" and (function, "degF") in PLAUSIBLE_RANGES:
            return _f_to_c(PLAUSIBLE_RANGES[(function, "degF")])
    quantity = FUNCTION_QUANTITY.get(function or "") or UNIT_QUANTITY.get(unit)
    if quantity is None:
        return None
    return QUANTITY_RANGES.get(quantity, {}).get(unit)


# --- vendors ------------------------------------------------------------------

class Vendor(TypedDict, total=False):
    name: str
    note: str  # what knowing the vendor tells you about the naming/topology you'll see
    gateway: bool  # a supervisory device that commonly fronts many pieces of equipment


# BACnet vendor identifiers, from ASHRAE's registered-vendor list. Only
# entries actually verified against that list are here - an unknown id is
# reported as unknown, never guessed. Vendor is the *weakest* evidence
# (spec's evidence ladder rung 5) and is used for naming hints only.
VENDORS: dict[int, Vendor] = {
    0: {"name": "ASHRAE", "note": "Reference/test implementation - unusual on a real building.", "gateway": False},
    1: {"name": "NIST", "note": "Reference/test implementation.", "gateway": False},
    2: {"name": "Trane", "note": "Tracer names spell things out (\"Disch Air Temp\", \"Space Temp\"); space, not zone.", "gateway": False},
    5: {"name": "Johnson Controls", "note": "Metasys uses hyphenated caps (\"SA-T\", \"ZN-T\", \"SF-C\", \"SF-S\"); -C command, -S status, -SP setpoint.", "gateway": False},
    7: {"name": "Siemens", "note": "Apogee/Desigo compact caps (\"SAT\", \"RAT\", \"SFSS\"); a single field panel often hosts several pieces of equipment.", "gateway": True},
    8: {"name": "Delta Controls", "note": "ORCA names are free text, often mixed case with spaces.", "gateway": False},
    10: {"name": "Schneider Electric", "note": "Andover/EcoStruxure: dotted or underscored paths; one controller may expose several equipment programs.", "gateway": True},
    17: {"name": "Honeywell", "note": "Spelled-out or compact depending on line (WEBs is Niagara-based - see Tridium).", "gateway": False},
    24: {"name": "Automated Logic", "note": "WebCTRL lower_snake names (\"sa_temp\", \"sf_status\"); the BACnet device is often the router with equipment behind it on ARCnet/MS-TP.", "gateway": True},
    36: {"name": "Tridium", "note": "Niagara JACE: a supervisory gateway - expect ONE BACnet device to front MANY pieces of equipment (several spec tags on one device is normal, not a duplicate-claim error) and serially-trunked field devices behind it that no IP sweep will see.", "gateway": True},
    37: {"name": "Reliable Controls", "note": "MACH-System: descriptive names with spaces.", "gateway": False},
    999: {"name": "unregistered / mock", "note": "999 is the id FBF's mock BACnet device uses; on a real network it means an unregistered stack.", "gateway": False},
}


def vendor_info(vendor_id: int | None) -> Vendor | None:
    if vendor_id is None:
        return None
    return VENDORS.get(int(vendor_id))


# --- point projections --------------------------------------------------------

# (function, role) -> Brick class + Haystack markers. Anything not listed
# projects to the nearest generic class and is reported as lossy. Class
# names verified to exist in ontology/Brick-only.ttl.
POINT_PROJECTIONS: dict[tuple[str, str], PointProjection] = {
    ("supply air temperature", ROLE_SENSOR): {"brick": "Supply_Air_Temperature_Sensor", "haystack": ["discharge", "air", "temp", "sensor"]},
    ("supply air temperature", ROLE_SETPOINT): {"brick": "Supply_Air_Temperature_Setpoint", "haystack": ["discharge", "air", "temp", "sp"]},
    ("return air temperature", ROLE_SENSOR): {"brick": "Return_Air_Temperature_Sensor", "haystack": ["return", "air", "temp", "sensor"]},
    ("mixed air temperature", ROLE_SENSOR): {"brick": "Mixed_Air_Temperature_Sensor", "haystack": ["mixed", "air", "temp", "sensor"]},
    ("outside air temperature", ROLE_SENSOR): {"brick": "Outside_Air_Temperature_Sensor", "haystack": ["outside", "air", "temp", "sensor"]},
    ("zone air temperature", ROLE_SENSOR): {"brick": "Zone_Air_Temperature_Sensor", "haystack": ["zone", "air", "temp", "sensor"]},
    ("zone air temperature", ROLE_SETPOINT): {"brick": "Zone_Air_Temperature_Setpoint", "haystack": ["zone", "air", "temp", "sp"]},
    ("zone air humidity", ROLE_SENSOR): {"brick": "Zone_Air_Humidity_Sensor", "haystack": ["zone", "air", "humidity", "sensor"]},
    ("zone co2", ROLE_SENSOR): {"brick": "Zone_CO2_Level_Sensor", "haystack": ["zone", "air", "co2", "sensor"]},
    ("supply air static pressure", ROLE_SENSOR): {"brick": "Supply_Air_Static_Pressure_Sensor", "haystack": ["discharge", "air", "pressure", "sensor"]},
    ("supply air static pressure", ROLE_SETPOINT): {"brick": "Supply_Air_Static_Pressure_Setpoint", "haystack": ["discharge", "air", "pressure", "sp"]},
    ("supply air flow", ROLE_SENSOR): {"brick": "Supply_Air_Flow_Sensor", "haystack": ["discharge", "air", "flow", "sensor"]},
    ("supply air flow", ROLE_SETPOINT): {"brick": "Supply_Air_Flow_Setpoint", "haystack": ["discharge", "air", "flow", "sp"]},
    ("supply fan", ROLE_COMMAND): {"brick": "Fan_Command", "haystack": ["discharge", "fan", "run", "cmd"]},
    ("supply fan", ROLE_STATUS): {"brick": "Fan_Status", "haystack": ["discharge", "fan", "run", "sensor"]},
    ("return fan", ROLE_COMMAND): {"brick": "Fan_Command", "haystack": ["return", "fan", "run", "cmd"]},
    ("return fan", ROLE_STATUS): {"brick": "Fan_Status", "haystack": ["return", "fan", "run", "sensor"]},
    ("exhaust fan", ROLE_COMMAND): {"brick": "Fan_Command", "haystack": ["exhaust", "fan", "run", "cmd"]},
    ("exhaust fan", ROLE_STATUS): {"brick": "Fan_Status", "haystack": ["exhaust", "fan", "run", "sensor"]},
    ("supply fan speed", ROLE_COMMAND): {"brick": "Fan_Speed_Command", "haystack": ["discharge", "fan", "speed", "cmd"]},
    ("damper position", ROLE_COMMAND): {"brick": "Damper_Position_Command", "haystack": ["damper", "cmd"]},
    ("damper position", ROLE_STATUS): {"brick": "Damper_Position_Sensor", "haystack": ["damper", "sensor"]},
    ("outside air damper position", ROLE_COMMAND): {"brick": "Damper_Position_Command", "haystack": ["outside", "air", "damper", "cmd"]},
    ("cooling valve position", ROLE_COMMAND): {"brick": "Valve_Position_Command", "haystack": ["cooling", "valve", "cmd"]},
    ("heating valve position", ROLE_COMMAND): {"brick": "Valve_Position_Command", "haystack": ["heating", "valve", "cmd"]},
    ("chilled water valve position", ROLE_COMMAND): {"brick": "Valve_Position_Command", "haystack": ["chilled", "water", "valve", "cmd"]},
    ("valve position", ROLE_COMMAND): {"brick": "Valve_Position_Command", "haystack": ["valve", "cmd"]},
    ("valve position", ROLE_STATUS): {"brick": "Valve_Position_Sensor", "haystack": ["valve", "sensor"]},
    ("chilled water supply temperature", ROLE_SENSOR): {"brick": "Chilled_Water_Supply_Temperature_Sensor", "haystack": ["chilled", "water", "leaving", "temp", "sensor"]},
    ("chilled water return temperature", ROLE_SENSOR): {"brick": "Chilled_Water_Return_Temperature_Sensor", "haystack": ["chilled", "water", "entering", "temp", "sensor"]},
    ("hot water supply temperature", ROLE_SENSOR): {"brick": "Hot_Water_Supply_Temperature_Sensor", "haystack": ["hot", "water", "leaving", "temp", "sensor"]},
    ("hot water return temperature", ROLE_SENSOR): {"brick": "Hot_Water_Return_Temperature_Sensor", "haystack": ["hot", "water", "entering", "temp", "sensor"]},
    ("condenser water supply temperature", ROLE_SENSOR): {"brick": "Condenser_Water_Temperature_Sensor", "haystack": ["condenser", "water", "leaving", "temp", "sensor"]},
    ("condenser water return temperature", ROLE_SENSOR): {"brick": "Condenser_Water_Temperature_Sensor", "haystack": ["condenser", "water", "entering", "temp", "sensor"]},
    ("electric power", ROLE_SENSOR): {"brick": "Electric_Power_Sensor", "haystack": ["elec", "power", "sensor"]},
    ("electric energy", ROLE_SENSOR): {"brick": "Electric_Energy_Sensor", "haystack": ["elec", "energy", "sensor"]},
    ("occupancy", ROLE_STATUS): {"brick": "Occupancy_Status", "haystack": ["occupied", "sensor"]},
    ("compressor", ROLE_COMMAND): {"brick": "On_Off_Command", "haystack": ["compressor", "run", "cmd"]},
    ("compressor", ROLE_STATUS): {"brick": "Run_Status", "haystack": ["compressor", "run", "sensor"]},
    ("pump", ROLE_COMMAND): {"brick": "On_Off_Command", "haystack": ["pump", "run", "cmd"]},
    ("pump", ROLE_STATUS): {"brick": "Run_Status", "haystack": ["pump", "run", "sensor"]},
    ("unit", ROLE_COMMAND): {"brick": "Enable_Command", "haystack": ["run", "cmd"]},
    ("unit", ROLE_STATUS): {"brick": "Run_Status", "haystack": ["run", "sensor"]},
    ("cooling", ROLE_COMMAND): {"brick": "Cooling_Command", "haystack": ["cooling", "cmd"]},
    ("heating", ROLE_COMMAND): {"brick": "Heating_Command", "haystack": ["heating", "cmd"]},
    ("filter differential pressure", ROLE_SENSOR): {"brick": "Filter_Differential_Pressure_Sensor", "haystack": ["filter", "pressure", "sensor"]},
    # Brick has a Reversing_Valve *equipment* class but no reversing-valve
    # command point - projecting to a bare Command is the honest nearest
    # class, and projection.py reports the function as lost.
    ("reversing valve", ROLE_COMMAND): {"brick": None, "haystack": ["reversingValve", "cmd"]},
}

GENERIC_ROLE_BRICK: dict[str, str] = {
    ROLE_SENSOR: "Sensor",
    ROLE_SETPOINT: "Setpoint",
    ROLE_COMMAND: "Command",
    ROLE_STATUS: "Status",
}
