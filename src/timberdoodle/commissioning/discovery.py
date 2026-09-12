"""
What the field actually shows - assembled from what the existing
connector has already landed, not from a second BACnet stack.

Two sources, merged on a stable field identity:

  * The live graph + history. FBF walks BACnet devices and publishes to
    MQTT; mqtt_listener.py turns that into `urn:equip:{topic_prefix}`
    (with its Haystack tags and brick:hasPoint edges) and
    `urn:point:{topic}` (td:sourceTopic, haystack:dis/unit/kind, markers)
    in Oxigraph plus rows in Postgres' point_history. Reading those back
    is discovery here - every object, its name, units and latest value,
    and when it was last heard from. Points nobody linked to equipment
    are grouped by topic prefix so they still show up rather than being
    silently out of scope.
  * Device records pushed through the API - the shape FBF's own
    GET /devices and POST /learn return (device_instance, address,
    vendor_id, object list) - for devices discovered on the network but
    not (yet) provisioned into a connection, so a controller that is
    responding but not being polled still counts as *existing*.

A field identity is the equip URI when there is one (it is the boundary a
human already drew at connection time - see ingest.topic_prefix_to_equip_uri),
otherwise `bacnet:<instance>@<address>`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.history import History
from timberdoodle.commissioning.model import DiscoveredDevice, DiscoveredObject

PREFIXES = """
PREFIX brick: <https://brickschema.org/schema/Brick#>
PREFIX haystack: <urn:timberdoodle:haystack#>
PREFIX td: <urn:timberdoodle:td#>
"""

_BACNET_OBJECT_TAIL = re.compile(r"^([a-zA-Z]+(?:Input|Output|Value)),(\d+)$")

# Valued Haystack tags FBF (or a Haystack source) may attach to a point
# that carry the BACnet object identity/name. Read if present, never
# required.
_OBJECT_ID_TAGS = ("bacnetObjectIdentifier", "objectIdentifier", "bacnetCur", "bacnetObject")
_OBJECT_NAME_TAGS = ("objectName", "bacnetName", "dis", "navName")
_DEVICE_TAGS = {"device_instance": ("bacnetDeviceInstance", "deviceInstance", "bacnetDevice"), "address": ("bacnetAddress", "deviceAddress", "address", "ip"), "vendor_id": ("vendorId", "bacnetVendorId", "vendor_id"), "model": ("modelName", "model"), "firmware": ("firmwareRevision", "firmware")}


def bacnet_field_id(device_instance, address) -> str:
    return f"bacnet:{device_instance}@{address}"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _split_tags(rows_value) -> list[str]:
    return [t for t in str(rows_value or "").split(" ") if t]


def _query_entity_facts(store, uris: list[str]) -> dict[str, dict]:
    """Every predicate/object for each URI, batched. Returns
    {uri: {"markers": [...], "valued": {tag: value}, "types": [...]}}."""
    out: dict[str, dict] = {u: {"markers": [], "valued": {}, "types": []} for u in uris}
    if not uris:
        return out
    # VALUES blocks of a few hundred URIs are fine for both rdflib and
    # Oxigraph; chunk so a 5,000-point building doesn't build one giant query.
    for i in range(0, len(uris), 400):
        chunk = uris[i:i + 400]
        values = " ".join(f"<{u}>" for u in chunk)
        rows = store.query(PREFIXES + f"SELECT ?s ?p ?o WHERE {{ VALUES ?s {{ {values} }} ?s ?p ?o }}")
        for row in rows:
            s, p, o = str(row.s), str(row.p), row.o
            facts = out.setdefault(s, {"markers": [], "valued": {}, "types": []})
            if p == "urn:timberdoodle:haystack#hasTag":
                facts["markers"].append(str(o))
            elif p.startswith("urn:timberdoodle:haystack#"):
                facts["valued"][p[len("urn:timberdoodle:haystack#"):]] = str(o) if not isinstance(o, (int, float)) else o
            elif p == "http://www.w3.org/1999/02/22-rdf-syntax-ns#type":
                facts["types"].append(str(o))
            elif p == "urn:timberdoodle:td#sourceTopic":
                facts["valued"]["_topic"] = str(o)
    return out


def _first(valued: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in valued and valued[k] not in (None, ""):
            return valued[k]
    return None


def _object_from_point(point_uri: str, facts: dict, latest: dict) -> DiscoveredObject:
    valued = facts.get("valued", {})
    topic = valued.get("_topic") or point_uri.replace("urn:point:", "", 1)
    tail = str(topic).rsplit("/", 1)[-1]
    object_identifier = _first(valued, _OBJECT_ID_TAGS)
    name = _first(valued, _OBJECT_NAME_TAGS)
    if _BACNET_OBJECT_TAIL.match(tail):
        object_identifier = object_identifier or tail
    else:
        name = name or tail
    if object_identifier is not None and "," not in str(object_identifier) and ":" in str(object_identifier):
        object_identifier = str(object_identifier).replace(":", ",", 1)
    sample = latest.get(point_uri)
    return {
        "object_identifier": str(object_identifier) if object_identifier else None,
        "name": str(name) if name else None,
        "description": valued.get("description") or valued.get("bacnetDescription"),
        "units": valued.get("unit"),
        "present_value": sample[1] if sample else None,
        "point_uri": point_uri,
        "tags": sorted(facts.get("markers", [])),
        "last_seen": _iso(sample[0]) if sample else None,
    }


def discover_from_store(store, history: History | None = None) -> list[DiscoveredDevice]:
    """Every piece of equipment (and every unlinked point group) the
    connector has landed in the graph, with latest values and last-seen
    stamps from history when a History is given."""
    equip_rows = store.query(PREFIXES + """
        SELECT DISTINCT ?equip WHERE {
            { ?equip brick:hasPoint ?p } UNION { ?equip haystack:hasTag ?t . FILTER(STRSTARTS(STR(?equip), "urn:equip:")) }
        }
    """)
    equip_uris = sorted({str(r.equip) for r in equip_rows if str(r.equip).startswith("urn:equip:")})

    link_rows = store.query(PREFIXES + "SELECT ?equip ?point WHERE { ?equip brick:hasPoint ?point }")
    points_of: dict[str, list[str]] = {u: [] for u in equip_uris}
    linked_points: set[str] = set()
    for r in link_rows:
        e, p = str(r.equip), str(r.point)
        if e in points_of:
            points_of[e].append(p)
            linked_points.add(p)

    unlinked_rows = store.query(PREFIXES + """
        SELECT ?point ?topic WHERE {
            ?point td:sourceTopic ?topic .
            FILTER NOT EXISTS { ?point brick:isPointOf ?e }
            FILTER NOT EXISTS { ?e2 brick:hasPoint ?point }
        }
    """)
    unlinked_groups: dict[str, list[str]] = {}
    for r in unlinked_rows:
        topic = str(r.topic)
        prefix = topic.rsplit("/", 1)[0] if "/" in topic else topic
        unlinked_groups.setdefault(prefix, []).append(str(r.point))

    all_points = sorted(linked_points | {p for ps in unlinked_groups.values() for p in ps})
    point_facts = _query_entity_facts(store, all_points)
    equip_facts = _query_entity_facts(store, equip_uris)
    latest = history.latest(all_points) if history is not None else {}

    devices: list[DiscoveredDevice] = []
    for equip_uri in equip_uris:
        facts = equip_facts.get(equip_uri, {"markers": [], "valued": {}})
        valued = facts["valued"]
        objects = [_object_from_point(p, point_facts.get(p, {}), latest) for p in sorted(points_of.get(equip_uri, []))]
        seen = [o["last_seen"] for o in objects if o.get("last_seen")]
        vendor_id = _first(valued, _DEVICE_TAGS["vendor_id"])
        try:
            vendor_id_int = int(vendor_id) if vendor_id is not None else None
        except (TypeError, ValueError):
            vendor_id_int = None
        topic_prefix = equip_uri.replace("urn:equip:", "", 1)
        instance = _first(valued, _DEVICE_TAGS["device_instance"])
        devices.append({
            "field_id": equip_uri,
            "device_instance": int(instance) if isinstance(instance, (int, float)) or (isinstance(instance, str) and instance.isdigit()) else None,
            "address": _first(valued, _DEVICE_TAGS["address"]),
            "name": valued.get("dis") or valued.get("navName") or topic_prefix.rsplit("/", 1)[-1],
            "description": valued.get("description"),
            "vendor_id": vendor_id_int,
            "vendor_name": (V.vendor_info(vendor_id_int) or {}).get("name") if vendor_id_int is not None else valued.get("vendor"),
            "model": _first(valued, _DEVICE_TAGS["model"]),
            "firmware": _first(valued, _DEVICE_TAGS["firmware"]),
            "topic_prefix": topic_prefix,
            "equip_uri": equip_uri,
            "tags": sorted(facts["markers"]),
            "objects": objects,
            "first_seen": None,
            "last_seen": max(seen) if seen else None,
            "source": "graph",
            "location": None,
        })

    for prefix, point_uris in sorted(unlinked_groups.items()):
        objects = [_object_from_point(p, point_facts.get(p, {}), latest) for p in sorted(point_uris)]
        seen = [o["last_seen"] for o in objects if o.get("last_seen")]
        devices.append({
            "field_id": f"urn:equip:{prefix}",
            "device_instance": None,
            "address": None,
            "name": prefix.rsplit("/", 1)[-1],
            "description": "points published under this topic prefix with no equipment record - grouped by prefix, not a connector-declared device",
            "vendor_id": None,
            "vendor_name": None,
            "model": None,
            "firmware": None,
            "topic_prefix": prefix,
            "equip_uri": None,
            "tags": [],
            "objects": objects,
            "first_seen": None,
            "last_seen": max(seen) if seen else None,
            "source": "graph_unlinked",
            "location": None,
        })
    return devices


def device_from_fbf(record: dict, learned: list[dict] | None = None) -> DiscoveredDevice:
    """FBF's GET /devices row (+ optional POST /learn object list) -> a
    DiscoveredDevice. Field names follow FBF's own API, unchanged."""
    instance = record.get("device_instance")
    address = record.get("address")
    topic_prefix = record.get("topic_prefix")
    objects: list[DiscoveredObject] = []
    for obj in learned or record.get("objects") or []:
        objects.append({
            "object_identifier": obj.get("object_identifier"),
            "name": obj.get("label") or obj.get("object_name") or obj.get("name"),
            "description": obj.get("description"),
            "units": obj.get("units"),
            "present_value": obj.get("present_value"),
            "point_uri": (f"urn:point:{topic_prefix}/{obj.get('label') or obj.get('object_identifier')}" if topic_prefix else None),
            "tags": list(obj.get("tags") or []),
            "last_seen": None,
        })
    vendor_id = record.get("vendor_id")
    first_seen = record.get("first_seen_at")
    last_seen = record.get("last_seen_at")
    return {
        "field_id": f"urn:equip:{topic_prefix}" if topic_prefix else bacnet_field_id(instance, address),
        "device_instance": int(instance) if instance is not None else None,
        "address": address,
        "name": record.get("name") or record.get("object_name") or record.get("dis"),
        "description": record.get("description"),
        "vendor_id": int(vendor_id) if vendor_id is not None else None,
        "vendor_name": (V.vendor_info(int(vendor_id)) or {}).get("name") if vendor_id is not None else record.get("vendor_name"),
        "model": record.get("model_name") or record.get("model"),
        "firmware": record.get("firmware_revision") or record.get("firmware"),
        "topic_prefix": topic_prefix,
        "equip_uri": f"urn:equip:{topic_prefix}" if topic_prefix else None,
        "tags": list(record.get("tags") or []),
        "objects": objects,
        "first_seen": _iso(datetime.fromtimestamp(first_seen, tz=timezone.utc)) if isinstance(first_seen, (int, float)) else first_seen,
        "last_seen": _iso(datetime.fromtimestamp(last_seen, tz=timezone.utc)) if isinstance(last_seen, (int, float)) else last_seen,
        "source": "fbf",
        "location": record.get("location"),
    }


def merge_devices(*sources: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
    """Same field_id from several sources -> one device; the graph's object
    list (it has point URIs and history) wins on overlap, the pushed
    record fills in device-level facts (instance, address, vendor) the
    graph rarely carries."""
    merged: dict[str, DiscoveredDevice] = {}
    for devices in sources:
        for d in devices:
            fid = d["field_id"]
            if fid not in merged:
                merged[fid] = dict(d)  # type: ignore[assignment]
                continue
            base = merged[fid]
            for key in ("device_instance", "address", "vendor_id", "vendor_name", "model", "firmware", "name", "description", "first_seen", "location"):
                if not base.get(key) and d.get(key):
                    base[key] = d[key]  # type: ignore[literal-required]
            if d.get("last_seen") and (not base.get("last_seen") or d["last_seen"] > base["last_seen"]):  # type: ignore[operator]
                base["last_seen"] = d["last_seen"]
            known = {(o.get("object_identifier"), o.get("name")) for o in base.get("objects", [])}
            for o in d.get("objects", []):
                if (o.get("object_identifier"), o.get("name")) not in known:
                    base.setdefault("objects", []).append(o)
            base["tags"] = sorted(set(base.get("tags", [])) | set(d.get("tags", [])))
            if base.get("source") != d.get("source"):
                base["source"] = f"{base.get('source')}+{d.get('source')}"
    return list(merged.values())
