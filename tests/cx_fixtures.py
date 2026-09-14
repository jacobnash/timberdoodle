"""
One small, realistic building shared by the commissioning tests: an AHU
with two VAV boxes, a chiller the network has not seen, and a non-
networked exhaust fan. Field names deliberately differ from the spec
tags the way real controllers do (AHU-1 -> AHU_1, VAV-1-01 -> V1_1) and
one box (V1_3) is *not* the one the spec expects (VAV-1-02).
"""

from datetime import datetime, timedelta, timezone

from timberdoodle.commissioning.history import InMemoryHistory

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

SPEC_MATERIAL = {
    "documents": [{"document": "M-601", "title": "Mechanical schedules"}],
    "equipment": [
        {
            "tag": "AHU-1", "type": "air handling unit", "serves": ["VAV-1-01", "VAV-1-02"],
            "location": {"floor": "1", "room": "Mech 101"},
            "points": [
                {"name": "Supply Air Temp", "units": "degF"}, {"name": "SA Temp Setpoint", "units": "degF"},
                {"name": "Supply Fan Status"}, {"name": "Supply Fan Command"}, {"name": "Duct Static Pressure", "units": "inH2O"},
            ],
            "source": {"document": "M-601", "page": 3},
        },
        {"tag": "VAV-1-01", "type": "VAV", "fed_by": ["AHU-1"], "location": {"floor": "1", "room": "101"},
         "points": [{"name": "Zone Temp"}, {"name": "Zone Temp Setpoint"}, {"name": "Damper Position"}, {"name": "Airflow", "units": "cfm"}]},
        {"tag": "VAV-1-02", "type": "VAV", "fed_by": ["AHU-1"], "location": {"floor": "1", "room": "102"},
         "points": [{"name": "Zone Temp"}, {"name": "Zone Temp Setpoint"}, {"name": "Damper Position"}]},
        {"tag": "EF-1", "type": "exhaust fan", "networked": False, "location": {"floor": "roof"}},
        {"tag": "CH-1", "type": "chiller", "location": {"floor": "B", "room": "Chiller room"},
         "points": [{"name": "CHWS Temp"}, {"name": "CHWR Temp"}, {"name": "Chiller Enable"}, {"name": "Chiller Status"}]},
    ],
}


def device(name, objs, instance, addr, vendor=8, now=NOW):
    return {
        "id": f"bacnet:{instance}@{addr}", "field_id": f"bacnet:{instance}@{addr}", "name": name,
        "device_instance": instance, "address": addr, "vendor_id": vendor,
        "objects": [{"name": n, "object_identifier": oid, "units": u, "present_value": pv, "point_uri": f"urn:point:site/{name}/{n}"} for n, oid, u, pv in objs],
        "source": "pushed", "first_seen": (now - timedelta(days=3)).isoformat(), "last_seen": now.isoformat(), "topic_prefix": f"site/{name}",
    }


AHU_OBJECTS = [
    ("SA-T", "analogInput,1", "degF", 55.2), ("SA-T-SP", "analogValue,1", "degF", 55.0),
    ("SF-S", "binaryInput,1", None, 1), ("SF-C", "binaryOutput,1", None, 1),
    ("DSP", "analogInput,2", "inH2O", 1.2), ("RA-T", "analogInput,3", "degF", 72.1),
]
VAV_OBJECTS = [
    ("ZN-T", "analogInput,1", "degF", 71.5), ("ZN-T-SP", "analogValue,1", "degF", 72.0),
    ("DMPR-POS", "analogOutput,1", "%", 40.0), ("FLOW", "analogInput,2", "cfm", 350.0),
]


def devices(now=NOW):
    return [
        device("AHU_1", AHU_OBJECTS, 1001, "10.0.0.11", now=now),
        device("V1_1", VAV_OBJECTS, 2001, "10.0.0.21", now=now),
        device("V1_3", VAV_OBJECTS[:3], 2003, "10.0.0.23", now=now),
    ]


def history(devs, now=NOW, hours=12, flat=("RA-T",)) -> InMemoryHistory:
    """Hourly samples for every object; analog values wobble a little so
    liveness passes, except the names in `flat`, which sit still."""
    hist = InMemoryHistory()
    for d in devs:
        for o in d["objects"]:
            for i in range(hours):
                t = now - timedelta(hours=hours - 1 - i)
                v = o["present_value"]
                if isinstance(v, float) and o["name"] not in flat:
                    v = v + (i % 3) * 0.2
                hist.add(o["point_uri"], t, v)
    return hist
