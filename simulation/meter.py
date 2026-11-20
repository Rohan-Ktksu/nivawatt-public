"""Simulated multi-channel electrical meter (synthetic): one meter, non-overlapping channels, configurable device mapping.

Producer side (what the simulated meter reports) and consumer side (how NivaWatt reads it) are kept apart:
- read_interval() turns the actual simulated device power of one interval into one reading per channel. The meter
  knows device power only through its channel wiring (the mapping); it knows nothing about plans, claims or EV identity.
- channel_total(), register_energy() are the consumer rules: group totals and counter-restart handling.

Units and semantics (model scale, like every *_model_* field in the API):
- average_power_model_kw: mean power over [interval_start_utc, interval_end_utc).
- interval_energy_model_kwh: energy over that interval = average power x interval hours.
- cumulative_energy_model_kwh: the channel's energy register since the meter session started. It restarts at 0
  when the meter restarts (meter_session increases, sequence_number restarts at 1). It is never used across sessions.
- interval_start_utc / interval_end_utc: model time, start inclusive, end exclusive.
- sequence_number: per meter session, +1 per reading; a gap means readings were lost.
- quality: VALID, LATE (arrived after its interval) or MISSING (no reading; all values null, never zero).
Power noise reuses the device emulator's model: reading = true x (1 + N(0, power_fraction_sd)), clipped at 0.
"""
import copy

PUMP, EV_GROUP, COMBINED = "pump", "ev_group", "participating"

# Branch metering: pump on its own channel, every EV charger on one group channel.
DEFAULT_BRANCH_CHANNELS = [
    {"channel_id": PUMP, "label": "Pump", "devices": ["pump-1"]},
    {"channel_id": EV_GROUP, "label": "EV charger group", "devices": ["charger-1", "charger-2"]},
]
# Aggregate metering: one channel on the combined participating circuits.
DEFAULT_AGGREGATE_CHANNELS = [
    {"channel_id": COMBINED, "label": "Pump and EV chargers combined", "devices": ["pump-1", "charger-1", "charger-2"]},
]


def default_channels(scope):
    return copy.deepcopy(DEFAULT_AGGREGATE_CHANNELS if scope == "aggregate" else DEFAULT_BRANCH_CHANNELS)


def validate_channels(channels, scope):
    """Channels must not overlap: every device is wired to exactly one channel, so channel sums never double count."""
    if not isinstance(channels, list) or not channels:
        raise ValueError("meter_channels must be a non-empty list")
    ids, seen = set(), {}
    for ch in channels:
        cid, devs = ch.get("channel_id"), ch.get("devices")
        if not isinstance(cid, str) or not cid or cid in ids:
            raise ValueError("each meter channel needs a unique channel_id")
        if not isinstance(devs, list) or not devs or not all(isinstance(d, str) and d for d in devs):
            raise ValueError(f"channel {cid} needs a non-empty devices list")
        for d in devs:
            if d in seen:
                raise ValueError(f"device {d} is mapped to both {seen[d]} and {cid}; channels must not overlap")
            seen[d] = cid
        ids.add(cid)
    need = {COMBINED} if scope == "aggregate" else {PUMP, EV_GROUP}
    if ids != need:
        raise ValueError(f"{scope} metering needs exactly the channels {sorted(need)}")
    return channels


def device_channel(channels, device):
    return next((c["channel_id"] for c in channels if device in c["devices"]), None)


def new_state(meter_id="meter-1"):
    return {"meter_id": meter_id, "meter_session": 1, "sequence_number": 0, "registers": {}}


def restart(state):
    """Meter restart: registers and sequence restart from zero in a new session. Nothing carries over."""
    state["meter_session"] += 1
    state["sequence_number"] = 0
    state["registers"] = {}


def read_interval(state, channels, start_utc, end_utc, dt_h, device_kw, noise_sd=0.0, noise_draws=None, missing=()):
    """One reading per channel for one interval, from the actual device power of that interval.

    device_kw: actual mean power per device id (devices not listed drew nothing). A device absent from the mapping is
    not metered; callers must reject such a configuration (see unmetered()).
    noise_draws: optional {channel_id: standard normal draw}; missing: channel ids whose reading is lost.
    The register keeps counting through a lost reading (the meter ran; the reading did not arrive).
    """
    state["sequence_number"] += 1
    out = []
    for ch in channels:
        cid = ch["channel_id"]
        true_kw = sum(float(device_kw.get(d, 0.0)) for d in ch["devices"])
        z = (noise_draws or {}).get(cid, 0.0)
        kw = max(0.0, true_kw * (1 + noise_sd * z)) if true_kw > 0 else 0.0
        kwh = kw * dt_h
        reg = state["registers"].get(cid, 0.0) + kwh
        state["registers"][cid] = reg
        lost = cid in missing
        out.append({
            "meter_id": state["meter_id"], "channel_id": cid, "label": ch.get("label", cid),
            "devices": list(ch["devices"]), "grouped": len(ch["devices"]) > 1,
            "interval_start_utc": start_utc, "interval_end_utc": end_utc,
            "average_power_model_kw": None if lost else round(kw, 4),
            "interval_energy_model_kwh": None if lost else round(kwh, 5),
            "cumulative_energy_model_kwh": None if lost else round(reg, 5),
            "meter_session": state["meter_session"], "sequence_number": state["sequence_number"],
            "quality": "MISSING" if lost else "VALID",
        })
    return out


def unmetered(channels, devices):
    """Devices that would draw power but are wired to no channel (a configuration error, not a zero reading)."""
    mapped = {d for c in channels for d in c["devices"]}
    return sorted(set(devices) - mapped)


def channel_total(readings):
    """Total of non-overlapping channels. Incomplete when any channel is missing: the total is then null, not a
    partial sum shown as if complete. The total is reported beside the channels and never added back to them."""
    usable = [r for r in readings if r["quality"] in ("VALID", "LATE") and r["average_power_model_kw"] is not None]
    missing = [r["channel_id"] for r in readings if r not in usable]
    complete = bool(readings) and not missing
    return {
        "basis": "sum of non-overlapping meter channels",
        "channels": [r["channel_id"] for r in readings],
        "complete": complete, "missing_channels": missing,
        "average_power_model_kw": round(sum(r["average_power_model_kw"] for r in usable), 4) if complete else None,
        "interval_energy_model_kwh": round(sum(r["interval_energy_model_kwh"] for r in usable), 5) if complete else None,
    }


def register_energy(points):
    """Energy and elapsed time from counter readings [(meter_session, time_ms, counter, session_start_ms)], in order.

    time_ms is the meter's own clock for the reading (simulated time for the emulator, the device's monotonic clock
    for a physical meter), never network arrival time. session_start_ms is when that session's counter was zero.
    The first point is the reference.
    - Same session, counter not lower: energy = counter increase, time = time increase.
    - New session (meter restart): the counter restarted at zero at session_start_ms, so the new reading is the energy
      since the restart, over the time since the restart. Energy between the last old reading and the restart is
      unknown and not added.
    - Same session but counter lower (reset without a session change): energy since the reset is the new reading;
      time is the time increase.
    Never negative. A repeated reading (same session, time and counter) adds nothing.
    Returns (energy, seconds, restarts_detected).
    """
    energy, secs, restarts, prev = 0.0, 0.0, 0, None
    for sess, t, reg, start in points:
        if reg is None:
            continue
        if prev is not None:
            psess, pt, preg, _ = prev
            if sess == psess and reg >= preg:
                energy += reg - preg
                secs += max(0.0, (t - pt) / 1000)
            else:
                restarts += 1
                energy += max(0.0, reg)
                secs += max(0.0, (t - start) / 1000) if sess != psess else max(0.0, (t - pt) / 1000)
        prev = (sess, t, reg, start)
    return energy, secs, restarts
