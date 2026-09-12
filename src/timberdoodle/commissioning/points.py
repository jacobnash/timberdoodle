"""
Plain-English classification of a discovered BACnet object: what it
measures or controls (`function`), what it is *for* (`role` - sensor,
setpoint, command, status), which way it points (`direction`), and
whether its present value is even plausible for what it claims to be.

Naming is vendor-specific and inconsistent, so this works from three
independent signals and says how much it trusts the result:

  1. the words in the object name (and description), expanded through the
     abbreviation lexicon in vocabulary.py;
  2. the BACnet object type (an analogInput is read by the controller, a
     binaryOutput is written by it);
  3. the engineering units, which can supply a missing quantity - or
     contradict the name, which is itself a finding.

Nothing here invents a point that isn't in the discovered data, and a
name it can't read comes back with function None and a low confidence,
never a best guess dressed up as a fact.
"""

from __future__ import annotations

import re
from typing import TypedDict

from timberdoodle.commissioning import vocabulary as V


class ClassifiedPoint(TypedDict, total=False):
    object_identifier: str | None
    name: str
    description: str | None
    function: str | None  # plain-English function, e.g. "supply air temperature"
    role: str | None  # sensor | setpoint | command | status
    direction: str  # input | output | value | unknown
    units: str | None  # canonical unit symbol
    units_as_written: str | None
    present_value: object
    plausibility: str  # plausible | implausible | unknown
    confidence: float  # 0..1, how sure the classification itself is
    evidence: list[str]  # the words/signals that produced it
    notes: list[str]  # anything a human should see (units contradict name, ...)
    point_uri: str | None
    tags: list[str]


_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")

# JCI-style trailing role letters: SF-C (command), SF-S (status).
_TRAILING_ROLE_LETTERS = {"c": V.ROLE_COMMAND, "s": V.ROLE_STATUS, "o": V.ROLE_COMMAND}
_ROLE_WORDS = {"setpoint": V.ROLE_SETPOINT, "command": V.ROLE_COMMAND, "status": V.ROLE_STATUS, "alarm": V.ROLE_STATUS}

_OBJECT_TYPE_DIRECTION = {
    "analoginput": ("input", V.ROLE_SENSOR),
    "binaryinput": ("input", V.ROLE_STATUS),
    "multistateinput": ("input", V.ROLE_STATUS),
    "analogoutput": ("output", V.ROLE_COMMAND),
    "binaryoutput": ("output", V.ROLE_COMMAND),
    "multistateoutput": ("output", V.ROLE_COMMAND),
    "analogvalue": ("value", None),
    "binaryvalue": ("value", None),
    "multistatevalue": ("value", None),
}


def tokenize(text: str | None) -> list[str]:
    """`AHU3_SA-T` -> ['ahu', '3', 'sa', 't']; `SupplyAirTemp` -> ['supply',
    'air', 'temp']. Lower-cased, camelCase and letter/digit boundaries split."""
    if not text:
        return []
    out: list[str] = []
    for part in _SPLIT.split(str(text)):
        if not part:
            continue
        for sub in _CAMEL.findall(part):
            out.append(sub.lower())
    return out


def object_type_of(object_identifier: str | None) -> str | None:
    """`analogInput,1` / `analog-input:1` / `AI 1` -> 'analoginput'
    (lower-cased, punctuation dropped - a key into _OBJECT_TYPE_DIRECTION)."""
    if not object_identifier:
        return None
    head = re.split(r"[,:\s]", str(object_identifier).strip(), maxsplit=1)[0]
    compact = re.sub(r"[^a-z]", "", head.lower())
    short = {"ai": "analoginput", "ao": "analogoutput", "av": "analogvalue", "bi": "binaryinput", "bo": "binaryoutput", "bv": "binaryvalue", "msi": "multistateinput", "mso": "multistateoutput", "msv": "multistatevalue"}
    if compact in short:
        return short[compact]
    return compact if compact in _OBJECT_TYPE_DIRECTION else None


def expand_words(tokens: list[str]) -> tuple[set[str], list[str]]:
    """Abbreviation tokens -> the plain words they stand for, plus the
    evidence trail of which tokens were understood."""
    words: set[str] = set()
    evidence: list[str] = []
    prev_words: set[str] = set()
    for i, tok in enumerate(tokens):
        if tok in V.ABBREVIATIONS:
            expansion = V.ABBREVIATIONS[tok]
            words.update(expansion)
            evidence.append(f"{tok}->{' '.join(expansion)}")
            prev_words = set(expansion)
            continue
        if tok in V.TRAILING_LETTER_QUANTITIES and prev_words & V.LOCATION_PREFIX_WORDS:
            q = V.TRAILING_LETTER_QUANTITIES[tok]
            words.add(q)
            evidence.append(f"{tok}->{q} (after {' '.join(sorted(prev_words))})")
            continue
        if tok in _TRAILING_ROLE_LETTERS and i == len(tokens) - 1 and i > 0 and words:
            role = _TRAILING_ROLE_LETTERS[tok]
            words.add(role)
            evidence.append(f"trailing -{tok.upper()}->{role}")
            continue
        prev_words = set()
    return words, evidence


def match_function(words: set[str]) -> str | None:
    """Most specific FUNCTION_PATTERNS entry whose words are all present."""
    best: tuple[int, str] | None = None
    for pattern, function in V.FUNCTION_PATTERNS:
        if pattern <= words and (best is None or len(pattern) > best[0]):
            best = (len(pattern), function)
    return best[1] if best else None


def _plausibility(function: str | None, unit: str | None, value) -> tuple[str, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown", None
    band = V.plausible_range(function, unit)
    if band is None:
        return "unknown", None
    lo, hi = band
    if lo <= float(value) <= hi:
        return "plausible", None
    return "implausible", f"present value {value} {unit} is outside the plausible {lo:g}..{hi:g} {unit} band for {function or 'this unit'}"


def classify_object(
    name: str | None,
    object_identifier: str | None = None,
    units: str | None = None,
    present_value=None,
    description: str | None = None,
    point_uri: str | None = None,
    tags: list[str] | None = None,
) -> ClassifiedPoint:
    """One discovered object -> its plain-English classification. Pure;
    safe to call on anything, including names it can't read (those come
    back with function None and confidence < 0.3)."""
    name = name or ""
    tokens = tokenize(name) + tokenize(description)
    words, evidence = expand_words(tokens)
    notes: list[str] = []

    canonical_unit = V.normalize_unit(units)
    unit_quantity = V.UNIT_QUANTITY.get(canonical_unit) if canonical_unit else None

    role_words = [w for w in words if w in _ROLE_WORDS]
    role: str | None = _ROLE_WORDS[role_words[-1]] if role_words else None
    role_from_words = role is not None

    function_words = words - set(_ROLE_WORDS)
    function = match_function(function_words)

    # Units can supply a quantity the name left out ("AHU3 SA" in degF is a
    # supply air temperature) - but only a quantity, never a location.
    if unit_quantity and unit_quantity not in ("percent", "onoff") and (function is None or V.FUNCTION_QUANTITY.get(function) is None):
        quantity_word = {"temperature": "temperature", "humidity": "humidity", "pressure": "pressure", "flow": "flow", "co2": "co2", "power": "power", "energy": "energy"}.get(unit_quantity)
        if quantity_word and quantity_word not in function_words:
            with_unit = match_function(function_words | {quantity_word})
            if with_unit and with_unit != function:
                function = with_unit
                evidence.append(f"units {canonical_unit} supplied quantity {quantity_word}")

    obj_type = object_type_of(object_identifier)
    direction, default_role = _OBJECT_TYPE_DIRECTION.get(obj_type or "", ("unknown", None))
    if role is None:
        if default_role is not None:
            role = default_role
            evidence.append(f"role {role} from object type {obj_type}")
        elif obj_type in ("analogvalue",):
            role = V.ROLE_SENSOR
            notes.append("analog value with no role word in its name - could be a setpoint or a computed value, not necessarily a sensor")
        elif obj_type in ("binaryvalue", "multistatevalue"):
            role = V.ROLE_STATUS
            notes.append("binary/multistate value with no role word in its name - could be a command as easily as a status")
        elif function is not None:
            role = V.ROLE_COMMAND if function in V.ONOFF_FUNCTIONS and direction == "output" else V.ROLE_SENSOR
            notes.append("role inferred with no object type available")

    # An input object named like a setpoint is far more often a sensor whose
    # name mentions the setpoint it tracks than a writable setpoint on an AI.
    if role == V.ROLE_SETPOINT and direction == "input":
        notes.append("named like a setpoint but is an input object - treated as a sensor reading; check whether it is a setpoint feedback")
        role = V.ROLE_SENSOR
    # An output object whose name says "status": the controller writes it,
    # so it is a command whatever it's called.
    if role == V.ROLE_STATUS and direction == "output":
        notes.append("named like a status but is an output object - treated as a command")
        role = V.ROLE_COMMAND
    if role == V.ROLE_SENSOR and function in V.ONOFF_FUNCTIONS:
        role = V.ROLE_STATUS  # a run "reading" of a fan is its status

    # Units contradicting the name is a finding, not something to resolve.
    function_quantity = V.FUNCTION_QUANTITY.get(function or "")
    units_contradict = False
    if function_quantity and unit_quantity and function_quantity != unit_quantity:
        compatible = {("percent", "humidity"), ("humidity", "percent"), ("onoff", "percent")}
        if (function_quantity, unit_quantity) not in compatible:
            units_contradict = True
            notes.append(f"units {canonical_unit} ({unit_quantity}) contradict the name's function '{function}' ({function_quantity})")

    plausibility, plaus_note = _plausibility(function, canonical_unit, present_value)
    if plaus_note:
        notes.append(plaus_note)

    understood = len([e for e in evidence if "->" in e])
    if function is None:
        confidence = 0.2 if role_from_words else 0.1
    elif understood >= 2:
        confidence = 0.9
    else:
        confidence = 0.6
    if not role_from_words and default_role is None:
        confidence *= 0.7
    if units_contradict:
        confidence *= 0.6
    if plausibility == "implausible":
        confidence *= 0.8

    return {
        "object_identifier": object_identifier,
        "name": name,
        "description": description,
        "function": function,
        "role": role,
        "direction": direction,
        "units": canonical_unit,
        "units_as_written": units,
        "present_value": present_value,
        "plausibility": plausibility,
        "confidence": round(confidence, 2),
        "evidence": evidence,
        "notes": notes,
        "point_uri": point_uri,
        "tags": list(tags or []),
    }


def label(point: ClassifiedPoint) -> str:
    """`supply air temperature sensor` - the plain-English name of what this
    point is, or the raw object name when it couldn't be read."""
    if point.get("function") and point.get("role"):
        return f"{point['function']} {point['role']}"
    return point.get("name") or point.get("object_identifier") or "unreadable point"


def has(points: list[ClassifiedPoint], function: str, role: str = "*") -> bool:
    return any(p.get("function") == function and (role == "*" or p.get("role") == role) for p in points)


def signature(points: list[ClassifiedPoint]) -> list[str]:
    """Sorted, de-duplicated `function role` labels - the shape of a device
    independent of what it calls itself."""
    return sorted({label(p) for p in points if p.get("function")})
