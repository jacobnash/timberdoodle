"""
Phase 3 - the verification ladder. Four rungs, climbed in order, in
every building, regardless of phase:

  1. existence      - on the network and responding
  2. liveness       - values changing at all (the dead sensor reading a
                      constant 72 forever is present, responding, useless)
  3. responsiveness - values track what they should track (status follows
                      command, zone temperature sits near its setpoint)
  4. command        - a setpoint change is followed by movement toward it

A failure sends the check *down*, never sideways: a zone that doesn't
track setpoint may be a rung-4 problem or a rung-2 problem wearing a
rung-4 costume, because a sensor frozen for six months produces identical
evidence. Every rung-3/4 failure re-checks liveness of the specific
sensor involved and reports where the fault really sits (`fell_to_rung`).

Rung 4 is passive by preference: buildings change their own setpoints
constantly (night setback, morning warm-up, occupancy), and observing
those transitions supplies the stimulus for free. Active writes are only
*planned* here (`plan_command_tests`) - before-state captured, small
delta, revert value - because this repository has no point-write
transport (FBF's command channel dispatches discover/learn, not writes);
executing a plan is the caller's responsibility and is logged as such.

Symptoms are certain; causes are candidates. Every failure names the
candidates and what would distinguish them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean

from timberdoodle.commissioning import vocabulary as V
from timberdoodle.commissioning.history import History, Sample
from timberdoodle.commissioning.model import Entity, EntityPoint, RungResult

RUNG_NAMES = {1: "existence", 2: "liveness", 3: "responsiveness", 4: "command response"}

DEFAULTS = {
    "existence_window_hours": 24.0,
    "history_window_hours": 24.0,
    "min_samples": 3,
    "flat_epsilon": 0.01,
    "response_window_minutes": 60.0,
    "pair_tolerance_minutes": 5.0,
    "zone_tolerance_degF": 3.0,
    "static_tolerance_inH2O": 0.3,
    "flow_tolerance_fraction": 0.2,
    "setpoint_step_degF": 2.0,
    "status_agreement_min": 0.9,
}


def _numeric(samples: list[Sample]) -> list[tuple[datetime, float]]:
    out = []
    for ts, v in samples:
        if isinstance(v, bool):
            out.append((ts, 1.0 if v else 0.0))
        elif isinstance(v, (int, float)):
            out.append((ts, float(v)))
        elif isinstance(v, str) and v.lower() in ("active", "on", "true", "running", "1"):
            out.append((ts, 1.0))
        elif isinstance(v, str) and v.lower() in ("inactive", "off", "false", "stopped", "0"):
            out.append((ts, 0.0))
    return out


def _to_f(value: float, unit: str | None) -> float:
    return value * 9.0 / 5.0 + 32.0 if unit == "degC" else value


def _pair(a: list[tuple[datetime, float]], b: list[tuple[datetime, float]], tolerance: timedelta) -> list[tuple[datetime, float, float]]:
    """(ts, a, b) for every a-sample with a b-sample within tolerance -
    nearest-in-time, no interpolation, so a gap stays a gap."""
    out = []
    j = 0
    for ts, va in a:
        while j + 1 < len(b) and abs(b[j + 1][0] - ts) <= abs(b[j][0] - ts):
            j += 1
        if b and abs(b[j][0] - ts) <= tolerance:
            out.append((ts, va, b[j][1]))
    return out


def _find(points: list[EntityPoint], function: str, role: str) -> EntityPoint | None:
    return next((p for p in points if p.get("function") == function and p.get("role") == role and p.get("point_uri") and p.get("status") in ("matched", "unspecified_present")), None)


def _is_flat(series: list[tuple[datetime, float]], eps: float) -> bool:
    values = [v for _, v in series]
    return bool(values) and (max(values) - min(values)) <= eps


def _candidates_for_no_response(kind: str) -> list[dict]:
    if kind == "temperature":
        return [
            {"cause": "stuck damper or failed damper actuator", "would_distinguish": "command the damper open/closed and watch airflow or damper feedback; listen at the box"},
            {"cause": "failed reheat/cooling valve or valve actuator", "would_distinguish": "command the valve and check coil discharge temperature or pipe temperature by hand"},
            {"cause": "dead or disconnected actuator wiring", "would_distinguish": "output voltage at the controller terminal vs. movement at the actuator"},
            {"cause": "sensor located where it can't see the zone (behind a cabinet, in a return plenum, wrong room)", "would_distinguish": "compare a handheld reading at the sensor with one in the occupied part of the zone"},
            {"cause": "undersized coil / unit out of capacity on a design day", "would_distinguish": "was outside air at or beyond design when this was observed; check supply air temperature at full command"},
            {"cause": "frozen sensor (a rung-2 fault in a rung-4 costume)", "would_distinguish": "the sensor never changes value at all in the window - checked automatically, see fell_to_rung"},
        ]
    if kind == "pressure":
        return [
            {"cause": "fan VFD not following its speed command", "would_distinguish": "compare fan speed command with VFD output frequency at the drive"},
            {"cause": "static pressure sensor tube kinked, disconnected, or in the wrong duct", "would_distinguish": "handheld manometer at the sensor tap vs. the reported value"},
            {"cause": "downstream dampers fully open so pressure cannot build", "would_distinguish": "check terminal unit damper positions during the test"},
        ]
    return [
        {"cause": "actuator/output not driving the device", "would_distinguish": "check output terminal vs. movement"},
        {"cause": "sensor not measuring what the point name says", "would_distinguish": "handheld verification at the sensor"},
    ]


def _rung1(entity: Entity, history: History, now: datetime, cfg: dict) -> RungResult:
    ts = now.isoformat()
    if entity.get("liveness", {}).get("state") == "not_networked":
        return {"rung": 1, "name": RUNG_NAMES[1], "result": "not_applicable", "evidence": ["spec says this equipment is not networked - no BACnet existence to check; a nameplate capture is the existence test"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    fid = entity.get("field_identity")
    if not fid:
        return {"rung": 1, "name": RUNG_NAMES[1], "result": "fail", "evidence": ["no discovered device maps to this equipment"], "symptom": "not found on the network", "candidates": [], "fell_to_rung": None, "checked_at": ts}
    uris = [p["point_uri"] for p in entity.get("points", []) if p.get("point_uri")]
    latest = history.latest(uris) if uris else {}
    window = timedelta(hours=cfg["existence_window_hours"])
    recent = [ts_ for ts_, _ in latest.values() if now - ts_ <= window]
    if recent:
        return {"rung": 1, "name": RUNG_NAMES[1], "result": "pass", "evidence": [f"{len(recent)}/{len(uris)} points reported within the last {cfg['existence_window_hours']:g} h (latest {max(recent).isoformat()})"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    if latest:
        newest = max(ts_ for ts_, _ in latest.values())
        return {"rung": 1, "name": RUNG_NAMES[1], "result": "fail", "evidence": [f"device is in the model but nothing has reported since {newest.isoformat()}"], "symptom": f"no samples in the last {cfg['existence_window_hours']:g} h", "candidates": [{"cause": "controller offline (power, network)", "would_distinguish": "ping/Who-Is at the address; check the enclosure"}, {"cause": "connector not polling it (connection removed or FBF down)", "would_distinguish": "is anything else from the same connector still reporting?"}], "fell_to_rung": None, "checked_at": ts}
    if fid.get("source") and "fbf" in str(fid.get("source")):
        return {"rung": 1, "name": RUNG_NAMES[1], "result": "pass", "evidence": ["responded to the connector's network discovery (FBF device record) - not yet provisioned, so no point history"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    return {"rung": 1, "name": RUNG_NAMES[1], "result": "insufficient_data", "evidence": ["device is in the graph but has no point history at all"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}


def _rung2(entity: Entity, series: dict[str, list[Sample]], cfg: dict, now: datetime) -> RungResult:
    ts = now.isoformat()
    sensors = [p for p in entity.get("points", []) if p.get("point_uri") and p.get("role") == V.ROLE_SENSOR and p.get("status") in ("matched", "unspecified_present")]
    if not sensors:
        return {"rung": 2, "name": RUNG_NAMES[2], "result": "not_attempted", "evidence": ["no sensor points to judge liveness on"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    live, flat, thin = [], [], []
    for p in sensors:
        s = _numeric(series.get(p["point_uri"] or "", []))
        if len(s) < cfg["min_samples"]:
            thin.append(p)
        elif _is_flat(s, cfg["flat_epsilon"]):
            flat.append((p, s[0][1]))
        else:
            live.append(p)
    evidence = [f"{len(live)} sensor(s) changing, {len(flat)} flat, {len(thin)} with fewer than {cfg['min_samples']} samples in the window"]
    if flat:
        names = "; ".join(f"{p.get('function')} ({p.get('name')}) constant at {v:g}{' ' + str(p.get('units')) if p.get('units') else ''}" for p, v in flat)
        return {"rung": 2, "name": RUNG_NAMES[2], "result": "fail", "evidence": evidence + [names], "symptom": f"{len(flat)} sensor(s) reading a constant value for the whole window: {names}", "candidates": [{"cause": "failed or disconnected sensor (controller reports a default or last value)", "would_distinguish": "reliability flag / out-of-service on the object; handheld reading at the sensor"}, {"cause": "point overridden or out of service in the controller", "would_distinguish": "priority array / override indicator in the vendor tool"}, {"cause": "connector polling a stale cache", "would_distinguish": "do other points on the same device change?"}], "fell_to_rung": None, "checked_at": ts}
    if live:
        return {"rung": 2, "name": RUNG_NAMES[2], "result": "pass", "evidence": evidence, "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    return {"rung": 2, "name": RUNG_NAMES[2], "result": "insufficient_data", "evidence": evidence + ["observe longer - nothing here has enough samples yet"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}


def _rung3(entity: Entity, series: dict[str, list[Sample]], cfg: dict, now: datetime) -> RungResult:
    ts = now.isoformat()
    pts = entity.get("points", [])
    tol = timedelta(minutes=cfg["pair_tolerance_minutes"])
    checks: list[tuple[str, bool, str, str | None, EntityPoint | None]] = []  # (name, passed, evidence, symptom, sensor)

    for fan in ("supply fan", "return fan", "exhaust fan", "pump", "compressor"):
        cmd, sts = _find(pts, fan, V.ROLE_COMMAND), _find(pts, fan, V.ROLE_STATUS)
        if cmd and sts:
            pairs = _pair(_numeric(series.get(cmd["point_uri"] or "", [])), _numeric(series.get(sts["point_uri"] or "", [])), tol)
            if len(pairs) >= cfg["min_samples"]:
                agree = sum(1 for _, a, b in pairs if (a > 0.5) == (b > 0.5)) / len(pairs)
                checks.append((f"{fan} status follows command", agree >= cfg["status_agreement_min"], f"{fan}: status agreed with command in {agree:.0%} of {len(pairs)} paired samples", None if agree >= cfg["status_agreement_min"] else f"{fan} status disagreed with its command {1 - agree:.0%} of the time", sts))

    for sensor_fn, sp_fn, kind, tol_val in (("zone air temperature", "zone air temperature", "temperature", cfg["zone_tolerance_degF"]), ("supply air temperature", "supply air temperature", "temperature", cfg["zone_tolerance_degF"]), ("supply air static pressure", "supply air static pressure", "pressure", cfg["static_tolerance_inH2O"])):
        sen, sp = _find(pts, sensor_fn, V.ROLE_SENSOR), _find(pts, sp_fn, V.ROLE_SETPOINT)
        if sen and sp:
            a = [(t, _to_f(v, sen.get("units")) if kind == "temperature" else v) for t, v in _numeric(series.get(sen["point_uri"] or "", []))]
            b = [(t, _to_f(v, sp.get("units")) if kind == "temperature" else v) for t, v in _numeric(series.get(sp["point_uri"] or "", []))]
            pairs = _pair(a, b, tol)
            if len(pairs) >= cfg["min_samples"]:
                mae = mean(abs(x - y) for _, x, y in pairs)
                unit = "degF" if kind == "temperature" else (sen.get("units") or "")
                ok = mae <= tol_val
                checks.append((f"{sensor_fn} tracks setpoint", ok, f"{sensor_fn}: mean |sensor - setpoint| = {mae:.2f} {unit} over {len(pairs)} paired samples (tolerance {tol_val:g})", None if ok else f"{sensor_fn} averaged {mae:.1f} {unit} from its setpoint over the window", sen))

    sen, sp = _find(pts, "supply air flow", V.ROLE_SENSOR), _find(pts, "supply air flow", V.ROLE_SETPOINT)
    if sen and sp:
        pairs = _pair(_numeric(series.get(sen["point_uri"] or "", [])), _numeric(series.get(sp["point_uri"] or "", [])), tol)
        pairs = [(t, a, b) for t, a, b in pairs if b > 0]
        if len(pairs) >= cfg["min_samples"]:
            rel = mean(abs(a - b) / b for _, a, b in pairs)
            ok = rel <= cfg["flow_tolerance_fraction"]
            checks.append(("supply air flow tracks setpoint", ok, f"supply air flow: mean relative error {rel:.0%} over {len(pairs)} paired samples", None if ok else f"supply air flow averaged {rel:.0%} from its setpoint", sen))

    sat, cool = _find(pts, "supply air temperature", V.ROLE_SENSOR), (_find(pts, "cooling valve position", V.ROLE_COMMAND) or _find(pts, "cooling", V.ROLE_COMMAND) or _find(pts, "chilled water valve position", V.ROLE_COMMAND))
    ref = _find(pts, "mixed air temperature", V.ROLE_SENSOR) or _find(pts, "return air temperature", V.ROLE_SENSOR)
    if sat and cool and ref:
        s = [(t, _to_f(v, sat.get("units"))) for t, v in _numeric(series.get(sat["point_uri"] or "", []))]
        r = [(t, _to_f(v, ref.get("units"))) for t, v in _numeric(series.get(ref["point_uri"] or "", []))]
        c = _numeric(series.get(cool["point_uri"] or "", []))
        sc = _pair(s, c, tol)
        active = [(t, v) for t, v, cv in sc if cv > 0.5 or cv > 50]
        pairs = _pair(active, r, tol)
        if len(pairs) >= cfg["min_samples"]:
            cooling_ok = sum(1 for _, a, b in pairs if a < b) / len(pairs)
            ok = cooling_ok >= 0.7
            checks.append(("supply air is cooler than mixed/return air when cooling is commanded", ok, f"supply air was below {ref.get('function')} in {cooling_ok:.0%} of {len(pairs)} samples with cooling active", None if ok else f"cooling commanded but supply air was not below {ref.get('function')} ({cooling_ok:.0%} of samples)", sat))

    if not checks:
        return {"rung": 3, "name": RUNG_NAMES[3], "result": "not_attempted", "evidence": ["no related point pair (command/status, sensor/setpoint, cooling/temperatures) with enough overlapping samples to judge tracking"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    failed = [c for c in checks if not c[1]]
    evidence = [c[2] for c in checks]
    if not failed:
        return {"rung": 3, "name": RUNG_NAMES[3], "result": "pass", "evidence": evidence, "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    worst = failed[0]
    fell = None
    if worst[4] is not None and worst[4].get("point_uri"):
        s = _numeric(series.get(worst[4]["point_uri"] or "", []))
        if len(s) >= cfg["min_samples"] and _is_flat(s, cfg["flat_epsilon"]):
            fell = 2
            evidence.append(f"re-checked rung 2: {worst[4].get('function')} was flat at {s[0][1]:g} for the whole window - this is a frozen sensor, not a tracking problem")
    kind = "pressure" if "pressure" in worst[0] else "temperature"
    candidates = _candidates_for_no_response(kind) if "tracks" in worst[0] or "cooler" in worst[0] else [{"cause": "motor/starter not responding to the controller output", "would_distinguish": "check the starter/VFD run indication against the controller's output terminal"}, {"cause": "status input wired to the wrong contact or a failed current switch", "would_distinguish": "meter the status input while the motor is known to be running"}, {"cause": "hand/off/auto switch not in auto", "would_distinguish": "look at the switch"}]
    return {"rung": 3, "name": RUNG_NAMES[3], "result": "fail", "evidence": evidence, "symptom": "; ".join(str(c[3]) for c in failed), "candidates": candidates, "fell_to_rung": fell, "checked_at": ts}


def _rung4_passive(entity: Entity, series: dict[str, list[Sample]], cfg: dict, now: datetime) -> RungResult:
    ts = now.isoformat()
    pts = entity.get("points", [])
    response = timedelta(minutes=cfg["response_window_minutes"])
    observed: list[tuple[str, datetime, float, float, float | None, float | None, bool, EntityPoint]] = []
    for fn, kind, step_min in (("zone air temperature", "temperature", cfg["setpoint_step_degF"]), ("supply air temperature", "temperature", cfg["setpoint_step_degF"]), ("supply air static pressure", "pressure", 0.2)):
        sen, sp = _find(pts, fn, V.ROLE_SENSOR), _find(pts, fn, V.ROLE_SETPOINT)
        if not (sen and sp):
            continue
        s = [(t, _to_f(v, sen.get("units")) if kind == "temperature" else v) for t, v in _numeric(series.get(sen["point_uri"] or "", []))]
        p = [(t, _to_f(v, sp.get("units")) if kind == "temperature" else v) for t, v in _numeric(series.get(sp["point_uri"] or "", []))]
        for i in range(1, len(p)):
            t0, old, new = p[i][0], p[i - 1][1], p[i][1]
            if abs(new - old) < step_min or t0 + response > now:
                continue
            before = [v for t, v in s if t0 - response <= t <= t0]
            after = [v for t, v in s if t0 < t <= t0 + response]
            if not before or not after:
                continue
            b, a = before[-1], after[-1]
            moved_toward = (a - b) * (new - old) > 0 and abs(a - b) >= 0.25 * abs(new - old)
            observed.append((fn, t0, old, new, b, a, moved_toward, sen))
    if not observed:
        return {"rung": 4, "name": RUNG_NAMES[4], "result": "insufficient_data", "evidence": ["no setpoint change large enough to act as a stimulus was observed in the window (night setback / morning warm-up / occupancy transitions supply these for free - observe across an occupancy boundary)", "no write transport is configured, so no active test was performed - see plan_command_tests for what one would do"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    responded = [o for o in observed if o[6]]
    evidence = [f"{fn}: setpoint {old:g}->{new:g} at {t0.isoformat()}; sensor {b:g}->{a:g} within {cfg['response_window_minutes']:g} min ({'responded' if ok else 'did not move toward it'})" for fn, t0, old, new, b, a, ok, _ in observed]
    if len(responded) * 2 >= len(observed):
        return {"rung": 4, "name": RUNG_NAMES[4], "result": "pass", "evidence": evidence, "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": ts}
    worst = next(o for o in observed if not o[6])
    fell = None
    s = _numeric(series.get(worst[7]["point_uri"] or "", []))
    if len(s) >= cfg["min_samples"] and _is_flat(s, cfg["flat_epsilon"]):
        fell = 2
        evidence.append(f"re-checked rung 2: {worst[0]} sensor was flat for the whole window - frozen sensor, not a command-response failure")
    symptom = f"{worst[0]} setpoint changed {worst[2]:g}->{worst[3]:g} at {worst[1].isoformat()}; the sensor read {worst[4]:g} before and {worst[5]:g} {cfg['response_window_minutes']:g} min later - it did not move toward the new setpoint"
    return {"rung": 4, "name": RUNG_NAMES[4], "result": "fail", "evidence": evidence, "symptom": symptom, "candidates": _candidates_for_no_response("pressure" if "pressure" in worst[0] else "temperature"), "fell_to_rung": fell, "checked_at": ts}


def climb(entity: Entity, history: History, now: datetime | None = None, config: dict | None = None) -> dict:
    """Run the ladder for one entity. Stops at the first rung that fails;
    later rungs are recorded as not_attempted so the report shows exactly
    how far the climb got and why."""
    now = now or datetime.now(timezone.utc)
    cfg = {**DEFAULTS, **(config or {})}
    rungs: list[RungResult] = []
    r1 = _rung1(entity, history, now, cfg)
    rungs.append(r1)
    highest = 0
    if r1["result"] == "not_applicable":
        return {"highest_rung_passed": 0, "rungs": rungs, "note": "not networked - verification is by nameplate capture"}
    if r1["result"] != "pass":
        for r in (2, 3, 4):
            rungs.append({"rung": r, "name": RUNG_NAMES[r], "result": "not_attempted", "evidence": ["lower rung did not pass"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": now.isoformat()})
        return {"highest_rung_passed": 0, "rungs": rungs}
    highest = 1
    uris = [p["point_uri"] for p in entity.get("points", []) if p.get("point_uri")]
    series = history.window(uris, now - timedelta(hours=cfg["history_window_hours"]), now)
    r2 = _rung2(entity, series, cfg, now)
    rungs.append(r2)
    if r2["result"] == "pass":
        highest = 2
    elif r2["result"] == "fail":
        for r in (3, 4):
            rungs.append({"rung": r, "name": RUNG_NAMES[r], "result": "not_attempted", "evidence": ["rung 2 failed - fix or explain the flat sensor(s) before judging tracking"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": now.isoformat()})
        return {"highest_rung_passed": highest, "rungs": rungs}
    r3 = _rung3(entity, series, cfg, now)
    rungs.append(r3)
    if r3["result"] == "pass" and highest == 2:
        highest = 3
    if r3["result"] == "fail":
        rungs.append({"rung": 4, "name": RUNG_NAMES[4], "result": "not_attempted", "evidence": ["rung 3 failed"], "symptom": None, "candidates": [], "fell_to_rung": None, "checked_at": now.isoformat()})
        if r3.get("fell_to_rung") == 2:
            highest = 1
        return {"highest_rung_passed": highest, "rungs": rungs}
    r4 = _rung4_passive(entity, series, cfg, now)
    rungs.append(r4)
    if r4["result"] == "pass" and highest == 3:
        highest = 4
    if r4["result"] == "fail" and r4.get("fell_to_rung") == 2:
        highest = 1
    return {"highest_rung_passed": highest, "rungs": rungs}


def plan_command_tests(entity: Entity, history: History, now: datetime | None = None) -> list[dict]:
    """What an active rung-4 test *would* do, with the before-state
    captured now: a small write, the revert value, and what to watch. Not
    executed here - there is no write path in this stack - so a caller
    with one (or a person at the vendor tool) can run it and post the
    outcome back as a correction/observation."""
    now = now or datetime.now(timezone.utc)
    pts = entity.get("points", [])
    plans = []
    for fn, delta, unit_default in (("zone air temperature", 2.0, "degF"), ("supply air temperature", 2.0, "degF"), ("supply air static pressure", 0.2, "inH2O")):
        sen, sp = _find(pts, fn, V.ROLE_SENSOR), _find(pts, fn, V.ROLE_SETPOINT)
        if not (sen and sp):
            continue
        latest = history.latest([sp["point_uri"] or "", sen["point_uri"] or ""])
        before = latest.get(sp["point_uri"] or "")
        sensor_now = latest.get(sen["point_uri"] or "")
        if before is None or not isinstance(before[1], (int, float)):
            continue
        # Step away from where the sensor sits so the response is visible.
        sensor_below = sensor_now is None or not isinstance(sensor_now[1], (int, float)) or sensor_now[1] <= before[1]
        direction = 1.0 if sensor_below else -1.0
        plans.append({
            "entity_id": entity.get("id"),
            "write_point": {"function": fn, "role": "setpoint", "point_uri": sp.get("point_uri"), "bacnet_object": sp.get("bacnet_object")},
            "watch_point": {"function": fn, "role": "sensor", "point_uri": sen.get("point_uri"), "bacnet_object": sen.get("bacnet_object")},
            "before_state": {"setpoint": before[1], "at": before[0].isoformat(), "sensor": sensor_now[1] if sensor_now else None},
            "write_value": before[1] + direction * delta,
            "revert_value": before[1],
            "revert_after_minutes": 60,
            "expect": f"{fn} sensor moves at least {0.25 * delta:g} {sp.get('units') or unit_default} toward {before[1] + direction * delta:g} within 60 min",
            "log": "record before-state, the write, and the revert; keep the write small",
            "planned_at": now.isoformat(),
        })
    return plans
