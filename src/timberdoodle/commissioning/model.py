"""
The canonical model's record shapes - plain TypedDicts, same reasoning as
timberdoodle/schemas.py: these live as JSON (Postgres JSONB, HTTP bodies,
the pass report) and a TypedDict costs nothing at runtime while letting
mypy catch a typo'd key.

Everything is plain English and ontology-neutral. A `canonical_type` is
`air handling unit`, a point `function` is `supply air temperature`. The
Brick/Haystack spellings only ever appear in projection.py's output.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, TypedDict

Phase = Literal["new_construction", "warranty", "operations", "retrofit"]
PHASES: tuple[Phase, ...] = ("new_construction", "warranty", "operations", "retrofit")

Confidence = Literal["confirmed", "high", "medium", "low", "unmatched"]
CONFIDENCE_ORDER: dict[str, int] = {"unmatched": 0, "low": 1, "medium": 2, "high": 3, "confirmed": 4}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --- spec model (Phase 1) -----------------------------------------------------

class SpecPoint(TypedDict, total=False):
    name: str  # as written in the point schedule
    function: str | None  # canonical plain-English function
    role: str | None  # sensor | setpoint | command | status
    units: str | None
    direction: str | None  # input | output
    required: bool
    source: str | None
    notes: list[str]


class SpecSource(TypedDict, total=False):
    document: str | None
    page: str | int | None


class Location(TypedDict, total=False):
    building: str | None
    floor: str | None
    room: str | None
    description: str | None  # "mech room above corridor 2E, ladder access"


class SpecEquipment(TypedDict, total=False):
    tag: str  # exactly as written
    type: str | None  # canonical plain-English type, None when unrecognized
    type_as_written: str | None
    description: str
    serves: list[str]
    fed_by: list[str]
    feeds: list[str]
    location: Location | None
    expected_points: list[SpecPoint]
    source: SpecSource
    vintage: str | None  # "2019", "original 1980 pneumatic"
    networked: bool  # expected to appear on BACnet at all (a pneumatic AHU: False)
    extraction_confidence: float  # how legible the source was, 0..1
    confidence: Confidence  # scored: is this entry usable as a hypothesis
    flags: list[str]


class Inconsistency(TypedDict, total=False):
    kind: str
    detail: str
    tags: list[str]


class SpecModel(TypedDict, total=False):
    equipment: list[SpecEquipment]
    inconsistencies: list[Inconsistency]
    escalate: list[dict]  # entries below threshold a human must look at before alignment is defensible
    assumptions: list[str]
    documents: list[dict]
    built_at: str
    confidence: Confidence  # overall: is the spec model usable without sign-off


# --- discovered devices (field) -------------------------------------------------

class DiscoveredObject(TypedDict, total=False):
    object_identifier: str | None
    name: str | None
    description: str | None
    units: str | None
    present_value: object
    point_uri: str | None
    tags: list[str]
    last_seen: str | None


class DiscoveredDevice(TypedDict, total=False):
    field_id: str  # stable field identity - equip URI, or bacnet:<instance>@<address>
    device_instance: int | None
    address: str | None
    name: str | None
    description: str | None
    vendor_id: int | None
    vendor_name: str | None
    model: str | None
    firmware: str | None
    topic_prefix: str | None
    equip_uri: str | None
    tags: list[str]
    objects: list[DiscoveredObject]
    first_seen: str | None
    last_seen: str | None
    source: str  # graph | fbf | field_capture | pushed
    location: Location | None


# --- mappings, entities, deviations --------------------------------------------

class Alternative(TypedDict, total=False):
    interpretation: str
    why: str
    would_resolve: str


class EvidenceItem(TypedDict, total=False):
    rung: int  # evidence ladder rung 1..5 (tag, signature, plausibility, topology, vendor)
    kind: str
    detail: str
    score: float


class Mapping(TypedDict, total=False):
    spec_tag: str | None
    field_id: str | None
    canonical_type: str | None
    confidence: Confidence
    score: float
    basis: list[EvidenceItem]
    alternatives: list[Alternative]
    acceptance: str | None  # auto-accepted | confirmed | None
    contradictions: list[str]


class RungResult(TypedDict, total=False):
    rung: int
    name: str
    result: str  # pass | fail | insufficient_data | not_attempted | not_applicable
    evidence: list[str]
    symptom: str | None
    candidates: list[dict]  # [{cause, would_distinguish}]
    fell_to_rung: int | None  # a failure re-checked at a lower rung and found there
    checked_at: str


class EntityPoint(TypedDict, total=False):
    function: str | None
    role: str | None
    direction: str | None
    units: str | None
    bacnet_object: str | None
    name: str | None
    point_uri: str | None
    status: str  # matched | specified_absent | unspecified_present | unreadable
    spec_name: str | None
    plausibility: str | None
    notes: list[str]


class Relationships(TypedDict, total=False):
    feeds: list[str]
    is_fed_by: list[str]
    serves: list[str]
    is_part_of: list[str]
    is_located_in: Location | None


class Provenance(TypedDict, total=False):
    spec_source: SpecSource | None
    first_discovered: str | None
    last_observed: str | None
    field_captures: list[dict]
    corrections: list[dict]


class Liveness(TypedDict, total=False):
    state: str  # present | missing | absent | never_seen | not_networked
    absent_since: str | None  # first pass at which it stopped being observed
    last_observed: str | None
    staleness_seconds: float | None
    signature_hash: str | None  # of the last *present* object list
    misses: int  # consecutive passes not observed ("missing" until the grace runs out)
    last_field_id: str | None  # where it was last seen - re-address detection


class Entity(TypedDict, total=False):
    id: str
    canonical_type: str | None
    description: str
    spec_tag: str | None
    field_identity: dict | None
    relationships: Relationships
    points: list[EntityPoint]
    ladder: dict  # {"highest_rung_passed": int, "rungs": [RungResult]}
    confidence: Confidence
    confidence_basis: list[EvidenceItem]
    alternatives: list[Alternative]
    acceptance: str | None
    provenance: Provenance
    liveness: Liveness
    deviations: list[str]
    faults: list[str]
    accepted_risks: list[str]
    reconcile_state: str | None  # unchanged | absent | returned | new | drifted | re_addressed
    reconcile_detail: dict | None
    updated_at: str


class Deviation(TypedDict, total=False):
    id: str
    kind: str  # spec_device_not_found | field_device_not_in_spec | type_mismatch | point_mismatch | duplicate_claim | unit_contradiction | topology_mismatch
    spec_tag: str | None
    field_id: str | None
    entity_id: str | None
    spec_says: str
    field_shows: str
    interpretation: str
    suspected: str | None
    to_settle: str  # what a person would have to do
    severity: str  # info | warning | critical
    status: str  # open | resolved
    opened_at: str
    resolved_at: str | None
    resolved_by: str | None
    resolution: str | None


class Correction(TypedDict, total=False):
    id: str
    kind: str  # confirm_mapping | correct_mapping | reject_mapping | correct_type | confirm_topology | naming_alias | resolve_deviation
    spec_tag: str | None
    field_id: str | None
    entity_id: str | None
    canonical_type: str | None
    relationship: dict | None  # {"kind": "feeds", "from": tag, "to": tag}
    alias: dict | None  # {"field_token": "AH", "spec_token": "AHU"}
    by: str
    at: str
    note: str | None


class FieldCapture(TypedDict, total=False):
    id: str
    captured_at: str
    engineer: str | None
    location: Location | None
    punch_item_id: str | None
    note: str | None
    photo_ref: str | None
    # What was legible, transcribed by a person (or an upstream vision step).
    # Only characters actually read - partial reads keep '?' for the unknown
    # characters and are listed in `partial`, never completed.
    legible: dict
    partial: dict
    unreadable: list[str]
    status: str  # ingested | unreadable | unprompted
    group_id: str | None  # consecutive captures at one location
    resolved: list[str]  # ids of punch items / deviations this settled
    corroborates: list[str]  # field_ids / spec tags this capture supports


class AcceptedRisk(TypedDict, total=False):
    id: str
    entity_id: str | None
    field_id: str | None
    spec_tag: str | None
    description: str
    accepted_by: str
    accepted_at: str
    review_by: str | None


class PunchItem(TypedDict, total=False):
    id: str
    location: Location
    location_key: str
    where: str
    what_to_find: str
    what_to_capture: list[str]
    what_would_resolve: list[str]
    reason: str
    entity_id: str | None
    spec_tag: str | None
    field_id: str | None
    deviation_ids: list[str]
    unblocks: int  # downstream entities waiting on this
    severity: str
    status: str  # open | resolved


class Project(TypedDict, total=False):
    id: str
    name: str
    phase: Phase | None
    phase_assumed: bool
    phase_rationale: str | None
    cidr_scopes: list[str]
    settings: dict
    created_at: str
    updated_at: str
