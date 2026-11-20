"""Synthetic apartment plant: tank mass balance, pump and EV-charging emulator, in model units.

Truth lives here only. Policies see observations. Switch positions change only through explicit
(simulated human) actions, never through proposal acceptance.
"""
import copy

from simulation import meter

DEFAULT_PARAMS = {
    "source_kind": "synthetic",
    "apartment_id": "APT_1",
    "model_dt_minutes": 15,
    "horizon_intervals": 96,
    "tank_max_l": 10000, "tank_min_l": 3000, "tank_init_l": 4500,
    "pump_inflow_lph": 6000, "pump_model_kw": 2.2,
    "ev_charger_model_kw": 3.3,
    "allowance_model_kw": 6.0,
    # Planning estimate == synthetic truth here; experiments may add disturbances to truth only.
    "withdrawal_profile_l": None,
    "source_outage_intervals": [],
    # DEMONSTRATION ASSUMPTIONS, not real-pump specifications (no bench pump has been characterised):
    # minimum run and restart-lockout durations in model intervals, and whether a dry-run protection device is fitted.
    "pump_min_on_intervals": 4,
    "pump_min_off_intervals": 2,
    "pump_dry_run_protection": False,
    # Physical level cutoff inside an interval (default off: existing runs unchanged). When on, a running pump stops at
    # the moment the tank reaches full during the 15-minute step, instead of pumping the whole step and overflowing:
    # inflow and pump energy are pro-rated to the running fraction, filling and withdrawal are concurrent, and the stop
    # is a level cutoff (a safety stop), so the pump is off at the end of the step.
    "pump_level_cutoff_within_interval": False,
    # Fine-step physics (default None: existing runs unchanged). A dict switches step() to internal sub-steps with a
    # hardware float switch on the pump circuit, the same for every policy:
    #   substep_min, sensing_min      internal step and level-sensing cadence (minutes)
    #   start_l, stop_l               float switch: latch a start at or below start_l, release at or above stop_l;
    #                                 at or above stop_l no command (manual or float) can run the pump
    #   actuation_delay_min           contactor/priming delay between a decision and the pump state changing
    #   reserve_l, critical_l         service levels used only for reporting
    # The pump command passed to step() is a manual request (advisory policies); the float call is ORed with it, so a
    # rejected or missed suggestion never removes the low-level start. Minimum on/off come from pump_min_*_intervals
    # (in minutes); the tank-full cutoff at tank_max_l acts within a sub-step with no delay and overrides minimum on.
    "fine_physics": None,
    # Scheduling policy. "advisory_only": the original rules scheduler (the library default, so studies and stored
    # runs keep their meaning). "demand_aware_causal_experimental": the same scheduler with a per-interval
    # participating-load cap from a causal whole-building forecast (scheduler/demand_forecast.py). The API makes the
    # causal policy the default for NEW runs (backend/main.py). Without whole_connection readings it plans with the
    # advisory-only fallback.
    "scheduling_policy": "advisory_only",
    # Whole-connection measurement (default None). Simulator runs only: {"source": "synthetic", ...} adds a synthetic
    # main-incomer channel that INCLUDES the pump and EV branches; the server fills its history and clock fields.
    # A physical deployment needs an authorized data interface or independent monitoring; the utility smart meter is
    # not assumed to be accessible.
    "whole_connection": None,
    # DEMONSTRATION ASSUMPTION: planning buffer above the reserve, about 1 h of the synthetic profile peak withdrawal
    # (250 L per 15 min x 4). It costs earlier/extra pumping and less flexibility; violations still count against
    # tank_min_l only.
    "reserve_margin_l": 1000,
    # DEMONSTRATION ASSUMPTION: critical water level, below which supply to residents is at risk. Distinct from the
    # ordinary reserve (tank_min_l) and reported separately.
    "tank_critical_l": 1000,
    # Verified physical/electrical limit for the participating circuits, in model kW. None = not verified/configured:
    # the operator allowance must then never be presented as physically checked.
    "verified_electrical_limit_model_kw": None,
    # branch: pump and EV branches metered separately; aggregate: one meter on the participating circuits.
    # The tank-level sensor is independent of either and is present in both.
    "observation_scope": "branch",
    # simulator: the server's synthetic plant produces observations at each step.
    # telemetry: a device (or the emulator) posts readings; each step closes the interval from them.
    "observation_source": "simulator",
    # Device units -> model units. 1.0 means the device meters apartment-scale circuits directly.
    "telemetry_power_scale": 1.0,
    "telemetry_volume_scale": 1.0,
    # How a telemetry run's meter keeps time. "device" (default): a physical meter; its counters are measured energy
    # and durations come from the device clock; nothing is time-scaled. "simulated": the device emulator; counters
    # accumulate simulated energy and every meter sample carries a simulated timestamp. The server rejects meter
    # samples whose clock differs from the run's.
    "meter_clock": "device",
    # Simulated multi-channel meter (simulation/meter.py). None = default channels for the observation scope:
    # branch -> pump channel + EV charger-group channel; aggregate -> one combined channel. Channels must not overlap.
    "meter_id": "meter-1",
    "meter_channels": None,
    # Meter power noise as a fraction of true power (same model as the device emulator). 0 keeps readings exact.
    "meter_power_noise_sd": 0.0,
    "pump_device_id": "pump-1",
    # Each EV session may name the charger it uses ("charger_id", default "charger-1"). The plant still models one
    # charging circuit rated ev_charger_model_kw (one EV at a time); the scheduler is unchanged.
    "ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32, "requested_model_kwh": 10}],
}


def default_withdrawals(n=96):
    """Synthetic daily profile (litres per 15 min): morning and evening use peaks. Not measured."""
    out = []
    for i in range(n):
        h = i * 15 / 60
        out.append(250 if 6 <= h < 9 else 200 if 18 <= h < 21 else 60 if 0 <= h < 5 else 110)
    return out


def make_params(overrides=None):
    p = copy.deepcopy(DEFAULT_PARAMS)
    unknown = set(overrides or {}) - set(DEFAULT_PARAMS)
    if unknown:  # an ignored key would silently change what a scenario means
        raise ValueError(f"unknown apartment parameter(s) {sorted(unknown)}")
    p.update(overrides or {})
    if not p["withdrawal_profile_l"]:
        p["withdrawal_profile_l"] = default_withdrawals(p["horizon_intervals"])
    if len(p["withdrawal_profile_l"]) != p["horizon_intervals"]:
        raise ValueError("withdrawal_profile_l length must equal horizon_intervals")
    if not 0 <= p["tank_min_l"] <= p["tank_max_l"] or not 0 <= p["tank_init_l"] <= p["tank_max_l"]:
        raise ValueError("tank bounds invalid")
    if p["observation_source"] not in ("simulator", "telemetry"):
        raise ValueError("observation_source must be simulator or telemetry")
    if p["meter_clock"] not in ("device", "simulated"):
        raise ValueError("meter_clock must be device or simulated")
    if p["telemetry_power_scale"] <= 0 or p["telemetry_volume_scale"] <= 0:
        raise ValueError("telemetry scales must be > 0")
    if p["observation_scope"] not in ("branch", "aggregate"):
        raise ValueError("observation_scope must be branch or aggregate")
    for k in ("pump_inflow_lph", "pump_model_kw", "ev_charger_model_kw", "allowance_model_kw"):
        if p[k] < 0:
            raise ValueError(f"{k} must be >= 0")
    if not (isinstance(p["pump_min_on_intervals"], int) and p["pump_min_on_intervals"] >= 1
            and isinstance(p["pump_min_off_intervals"], int) and p["pump_min_off_intervals"] >= 0):
        raise ValueError("pump_min_on_intervals must be an int >= 1 and pump_min_off_intervals an int >= 0")
    if not 0 <= p["tank_critical_l"] <= p["tank_min_l"]:
        raise ValueError("tank_critical_l must be between 0 and tank_min_l")
    v = p["verified_electrical_limit_model_kw"]
    if v is not None and not (isinstance(v, (int, float)) and v > 0):
        raise ValueError("verified_electrical_limit_model_kw must be null or > 0")
    if not 0 <= p["reserve_margin_l"] <= p["tank_max_l"] - p["tank_min_l"]:
        raise ValueError("reserve_margin_l must be between 0 and tank_max_l - tank_min_l")
    if p["scheduling_policy"] not in ("advisory_only", "demand_aware_causal_experimental"):
        raise ValueError("scheduling_policy must be advisory_only or demand_aware_causal_experimental")
    wc = p["whole_connection"]
    if wc is not None and not (isinstance(wc, dict) and wc.get("source") == "synthetic"
                               and wc.get("category") == "HT-4" and isinstance(wc.get("contract_demand_kva"), (int, float))
                               and wc["contract_demand_kva"] >= 50):
        raise ValueError("whole_connection must be {source: synthetic, category: HT-4, contract_demand_kva >= 50, ...}")
    fp = p["fine_physics"]
    if fp is not None:
        need = {"substep_min", "sensing_min", "start_l", "stop_l", "actuation_delay_min", "reserve_l", "critical_l"}
        if set(fp) != need:
            raise ValueError(f"fine_physics needs exactly {sorted(need)}")
        if not (0 < fp["substep_min"] <= fp["sensing_min"] and fp["actuation_delay_min"] >= 0
                and 0 <= fp["critical_l"] <= fp["reserve_l"] < fp["start_l"] < fp["stop_l"] <= p["tank_max_l"]):
            raise ValueError("fine_physics: need 0 < substep <= sensing, delay >= 0, critical <= reserve < start < stop <= tank_max")
    for s in p["ev_sessions"]:
        if not 0 <= s["arrival_interval"] < s["departure_interval"] <= p["horizon_intervals"] or s["requested_model_kwh"] < 0:
            raise ValueError(f"invalid EV session {s['ev_id']}")
    if p["meter_channels"] is None:
        p["meter_channels"] = meter.default_channels(p["observation_scope"])
    meter.validate_channels(p["meter_channels"], p["observation_scope"])
    loose = meter.unmetered(p["meter_channels"], [p["pump_device_id"]] + [charger_id(s) for s in p["ev_sessions"]])
    if loose:
        raise ValueError(f"device(s) {loose} are not wired to any meter channel")
    if not 0 <= p["meter_power_noise_sd"] < 1:
        raise ValueError("meter_power_noise_sd must be in [0, 1)")
    return p


def charger_id(session):
    return session.get("charger_id", "charger-1")


def initial_truth(p):
    return {"tank_l": float(p["tank_init_l"]), "pump_on": False, "ev_on": False, "pump_starts": 0,
            "ev_delivered_model_kwh": {s["ev_id"]: 0.0 for s in p["ev_sessions"]},
            "unmet_water_l": 0.0, "overflow_l": 0.0,
            "pump_run_intervals": 0, "pump_off_intervals": None}  # None: no stop yet in this run, so no lockout


def pump_gate(truth, p, i, commanded):
    """Effective pump state for interval i. Precedence: safety stop > restart lockout > command.

    Safety stops (tank already full; no source water when dry-run protection is fitted) always win and never wait
    for the minimum run. A start inside the minimum off time after a stop is not honoured (restart lockout).
    A manual OFF is always honoured; a run ended before the minimum on time is counted as short, not prevented.
    Returns (pump_on, safety_stop_reason or None, restart_lockout, short_run_stop).
    """
    source = i not in p["source_outage_intervals"]
    if truth["tank_l"] >= p["tank_max_l"] - 1e-9:
        return False, "TANK_FULL_CUTOFF", False, False
    if not source and p["pump_dry_run_protection"]:
        return False, "NO_SOURCE_WATER", False, False
    off = truth["pump_off_intervals"]
    if commanded and not truth["pump_on"] and off is not None and off < p["pump_min_off_intervals"]:
        return False, None, True, False
    short = (not commanded) and truth["pump_on"] and truth["pump_run_intervals"] < p["pump_min_on_intervals"]
    return commanded, None, False, short


def _water_fine(t, p, i, manual, fp):
    """Pump and tank over interval i in sub-steps; returns (pump_mean_kw, inflow_l, safety, fine_record)."""
    dt = fp["substep_min"]
    n = round(p["model_dt_minutes"] / dt)
    assert abs(n * dt - p["model_dt_minutes"]) < 1e-9, "substep must divide the interval"
    sense = max(1, round(fp["sensing_min"] / dt))
    min_on, min_off = (p["pump_min_on_intervals"] * p["model_dt_minutes"], p["pump_min_off_intervals"] * p["model_dt_minutes"])
    st = t.setdefault("fine", {"call": False, "high": False, "queue": [False] * round(fp["actuation_delay_min"] / dt),
                               "run_min": 0.0, "off_min": float(min_off), "k": 0})
    source = i not in p["source_outage_intervals"]
    ws = p["withdrawal_profile_l"][i] / n  # shortcut: demand uniform inside the 15-minute interval
    flow = p["pump_inflow_lph"] / 60 * dt
    r = {"starts": 0, "reserve_min": 0.0, "critical_min": 0.0, "reserve_deficit_lmin": 0.0, "critical_deficit_lmin": 0.0,
         "min_tank_l": t["tank_l"], "cutoffs": 0, "protection_short_runs": 0, "start_blocked_min": 0.0,
         "float_only_run_min": 0.0, "float_starts": 0}
    kwh, inflow_l = 0.0, 0.0
    for _ in range(n):
        if st["k"] % sense == 0:  # level sensed on a fixed cadence
            lvl = t["tank_l"]
            st["call"] = True if lvl <= fp["start_l"] else False if lvl >= fp["stop_l"] else st["call"]
            st["high"] = lvl >= fp["stop_l"]
        st["queue"].append((st["call"] or bool(manual)) and not st["high"])
        want = st["queue"].pop(0)
        on = t["pump_on"]
        if on and not want and st["run_min"] < min_on and not st["high"]:
            want = True  # minimum on time (never past the high-level stop: a level protection wins)
        if want and not on and st["off_min"] < min_off:
            want, r["start_blocked_min"] = False, r["start_blocked_min"] + dt  # minimum off time
        if want and not source and p["pump_dry_run_protection"]:
            want = False
        frac, q = 1.0, (flow if (want and source) else 0.0)
        if want and q > ws and t["tank_l"] + q - ws > p["tank_max_l"]:  # tank-full cutoff inside the sub-step
            frac = max(0.0, (p["tank_max_l"] - t["tank_l"]) / (q - ws))
            r["cutoffs"] += 1
        r["starts"] += want and not on
        r["float_starts"] += want and not on and st["call"] and not manual  # automatic safety start
        v = t["tank_l"] + q * frac
        served = min(ws, v)
        t["unmet_water_l"] += ws - served
        t["overflow_l"] += max(0.0, v - served - p["tank_max_l"])
        t["tank_l"] = min(v - served, p["tank_max_l"])
        inflow_l += q * frac
        if want:
            kwh += p["pump_model_kw"] * dt / 60 * frac
            r["float_only_run_min"] += dt * frac if (st["call"] and not manual) else 0.0
            st["run_min"] = (st["run_min"] if on else 0.0) + dt * frac
            if frac < 1.0:
                r["protection_short_runs"] += st["run_min"] < min_on
                want, st["run_min"], st["off_min"] = False, 0.0, dt * (1 - frac)
        else:
            r["protection_short_runs"] += on and st["run_min"] < min_on  # only the high-level stop can end it early
            st["off_min"] = (st["off_min"] if not on else 0.0) + dt
            st["run_min"] = 0.0
        t["pump_on"] = want
        lvl = t["tank_l"]
        r["min_tank_l"] = min(r["min_tank_l"], lvl)
        if lvl < fp["reserve_l"]:
            r["reserve_min"] += dt
            r["reserve_deficit_lmin"] += (fp["reserve_l"] - lvl) * dt
        if lvl < fp["critical_l"]:
            r["critical_min"] += dt
            r["critical_deficit_lmin"] += (fp["critical_l"] - lvl) * dt
        st["k"] += 1
    t["pump_starts"] += r["starts"]
    # interval-granularity view of the pump timers for the planner (completed whole intervals)
    t["pump_run_intervals"] = int(st["run_min"] // p["model_dt_minutes"]) if t["pump_on"] else 0
    t["pump_off_intervals"] = None if t["pump_on"] else int(st["off_min"] // p["model_dt_minutes"])
    r = {k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()}
    return kwh / (p["model_dt_minutes"] / 60), inflow_l, ("TANK_FULL_LEVEL_CUTOFF" if r["cutoffs"] else None), r


def step(truth, p, i, pump_on, ev_on):
    """Advance interval i with the commanded switch positions. Returns (new_truth, interval_record)."""
    t = copy.deepcopy(truth)
    dt_h = p["model_dt_minutes"] / 60
    commanded = pump_on
    if p.get("fine_physics"):
        pump_kw, inflow, safety, fine = _water_fine(t, p, i, commanded, p["fine_physics"])
        t["ev_on"] = ev_on
        return _finish(t, p, i, ev_on, pump_kw, inflow, commanded, safety, False, False, fine)
    pump_on, safety, lockout, short = pump_gate(t, p, i, commanded)
    t["pump_starts"] += bool(pump_on and not t["pump_on"])
    if pump_on:  # completed ON intervals in the current run
        t["pump_run_intervals"] = t["pump_run_intervals"] + 1 if t["pump_on"] else 1
    else:  # completed OFF intervals since the last stop (None until the first stop)
        if t["pump_on"]:
            t["pump_off_intervals"] = 1
        elif t["pump_off_intervals"] is not None:
            t["pump_off_intervals"] += 1
        t["pump_run_intervals"] = 0
    t["pump_on"], t["ev_on"] = pump_on, ev_on
    source = i not in p["source_outage_intervals"]
    w = p["withdrawal_profile_l"][i]
    frac = 1.0  # running fraction of the step
    if p.get("pump_level_cutoff_within_interval"):
        full = p["pump_inflow_lph"] * dt_h if (pump_on and source) else 0.0
        net = full - w
        if pump_on and source and net > 0 and t["tank_l"] + net > p["tank_max_l"]:
            frac = max(0.0, min(1.0, (p["tank_max_l"] - t["tank_l"]) / net))  # tank full at this point of the step
            safety = safety or "TANK_FULL_LEVEL_CUTOFF"
        inflow = full * frac
        v = t["tank_l"] + inflow  # concurrent fill and withdrawal over the step
        served = min(w, v)
        end = v - served
        t["overflow_l"] += max(0.0, end - p["tank_max_l"])
        t["unmet_water_l"] += w - served
        t["tank_l"] = min(end, p["tank_max_l"])
        if frac < 1.0:  # the level switch stopped the pump during this step
            t["pump_on"], t["pump_run_intervals"], t["pump_off_intervals"] = False, 0, 0
    else:
        inflow = p["pump_inflow_lph"] * dt_h if (pump_on and source) else 0.0  # dry pump draws power, moves no water
        v = t["tank_l"] + inflow
        t["overflow_l"] += max(0.0, v - p["tank_max_l"])
        v = min(v, p["tank_max_l"])
        served = min(w, v)
        t["unmet_water_l"] += w - served
        t["tank_l"] = v - served
    pump_kw = p["pump_model_kw"] * frac if pump_on else 0.0  # mean power over the step
    return _finish(t, p, i, ev_on, pump_kw, inflow, commanded, safety, lockout, short, None)


def _finish(t, p, i, ev_on, pump_kw, inflow, commanded, safety, lockout, short, fine):
    """EV charging and the interval record (shared by the 15-minute and fine-step paths)."""
    dt_h = p["model_dt_minutes"] / 60
    w = p["withdrawal_profile_l"][i]
    source = i not in p["source_outage_intervals"]
    ev_kwh = 0.0
    if ev_on:
        for s in p["ev_sessions"]:  # single charger: first present EV with remaining request
            rem = s["requested_model_kwh"] - t["ev_delivered_model_kwh"][s["ev_id"]]
            if s["arrival_interval"] <= i < s["departure_interval"] and rem > 1e-9:
                ev_kwh = min(p["ev_charger_model_kw"] * dt_h, rem)
                t["ev_delivered_model_kwh"][s["ev_id"]] += ev_kwh
                ev_id = s["ev_id"]
                break
    ev_kw = ev_kwh / dt_h
    # Actual mean power per physical device this interval: what a meter wired to these devices would see.
    devices = {p.get("pump_device_id", "pump-1"): pump_kw}
    if ev_kwh:
        devices[charger_id(next(s for s in p["ev_sessions"] if s["ev_id"] == ev_id))] = round(ev_kw, 4)
    rec = {"interval_index": i, "tank_l": round(t["tank_l"], 2), "inflow_l": round(inflow, 2), "withdrawal_l": w,
           "source_water_available": source, "pump_model_kw": pump_kw, "ev_model_kw": round(ev_kw, 4),
           "ev_id": ev_id if ev_kwh else None, "aggregate_model_kw": round(pump_kw + ev_kw, 4),
           "unmet_water_l": round(t["unmet_water_l"], 2), "overflow_l": round(t["overflow_l"], 2),
           "pump_commanded": bool(commanded), "pump_safety_stop": safety, "pump_restart_lockout": lockout,
           "pump_short_run_stop": short, "device_model_kw": devices}
    if fine is not None:
        rec["fine"] = fine
    return t, rec
