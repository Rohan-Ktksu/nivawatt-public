"""Deterministic deadline-aware rules scheduler (source="rules"). Pure: no DB or hardware writes.

Constraint classes, in the order the scheduler applies them (sequential, not a weighted objective):
  1. PHYSICAL SAFETY (hard): no planned pumping in known source outages; no planned tank overflow; a VERIFIED
     electrical limit for the participating circuits if one is configured (none is in this prototype).
  2. ASSUMED EQUIPMENT LIMITS (hard unless impossible): minimum pump on/off durations (demonstration assumptions);
     an unavoidable short run is listed in pump_duration_exceptions, never silently allowed.
  3. PARTICIPATING-LOAD ALLOWANCE (hard; a configured participating-load limit set by the operator, not
     BESCOM-sanctioned load or transformer capacity): never planned above, and manager acceptance never raises it.
     Rescheduling within it is tried first; if service is still short, the option reports operator_intervention with
     the binding constraint, the predicted shortfall and, where a higher allowance would help, the smallest such value
     up to the connected load (assumed ratings, not a verified limit). Only the operator can change the allowance
     (existing allowance event); until then the cap stays and the risk is shown.
  4. SERVICE: the water reserve (which also protects the separate, lower critical level), then each EV's energy
     before its departure deadline; critical-level, reserve and EV shortfalls are quantified separately.
  5. ADVISORY PEAK WINDOWS (preference): avoided where the classes above allow.
  6. READINESS MARGIN (demonstration target): extra water above the reserve, planned only outside advisory windows
     after service is met; misses are reported, not hidden. The planner is never told about unannounced outages.
  7. Fewer pump starts (tie-breaker).
Every option carries a constraint_report with the status of each class.
"""
import time

EPS = 1e-9
MIN_RUN = 4  # pump run length in model intervals (1 h at 15 min): avoids one pump start per 15-min top-up


def _service(o):
    w = o["options"][0]["water_shortfall"]
    return (w.get("intervals_below_critical", 0), w["intervals_below_reserve"],
            round(sum(o["options"][0]["ev_shortfall_model_kwh"].values()), 3))


def plan(current_state, known_events, requirements, constraints):
    """Plan within the operator allowance, then say whether the allowance binds and what only the operator can fix."""
    c = constraints
    out = _plan_with_margin(current_state, known_events, requirements, c)
    opt = out["options"][0]
    cap = c["allowance_model_kw"]
    connected = c["pump_model_kw"] + c["ev_charger_model_kw"]
    verified = c.get("verified_electrical_limit_model_kw")
    ceiling = min(connected, verified) if verified else connected
    here = _service(out)
    status, oi = "not_binding", None
    if ceiling > cap + EPS:
        free = _plan_with_margin(current_state, known_events, requirements, {**c, "allowance_model_kw": ceiling})
        differs = [a["pump"] for a in free["options"][0]["actions"]] != [a["pump"] for a in opt["actions"]] or \
            [a["ev"] for a in free["options"][0]["actions"]] != [a["ev"] for a in opt["actions"]]
        if _service(free) < here:
            need = None
            for x in sorted({v for v in (c["pump_model_kw"], c["ev_charger_model_kw"], connected, ceiling)
                             if cap + EPS < v <= ceiling + EPS}):
                if _service(_plan_with_margin(current_state, known_events, requirements,
                                              {**c, "allowance_model_kw": x})) == _service(free):
                    need = x
                    break
            status = "binding_service_shortfall"
            oi = {"required": True, "reason": "ALLOWANCE_BLOCKS_SERVICE", "binding_constraint": "allowance",
                  "current_allowance_model_kw": cap, "shortfall_within_allowance": dict(zip(
                      ("intervals_below_critical", "intervals_below_reserve", "ev_shortfall_model_kwh"), here)),
                  "allowance_that_would_reduce_shortfall_model_kw": need,
                  "shortfall_at_that_allowance": dict(zip(
                      ("intervals_below_critical", "intervals_below_reserve", "ev_shortfall_model_kwh"), _service(free))),
                  "physical_check": "verified electrical limit" if verified else
                  "NOT VERIFIED: value bounded only by assumed connected-load ratings; the operator must confirm "
                  "electrical capacity before any increase",
                  "who_can_approve": "operator only (allowance event); manager acceptance does not change the allowance",
                  "until_approved": "the current allowance stays in force and the shortfall above is expected"}
        elif differs:
            status = "binding_resolved_by_rescheduling"
    if oi is None and any(v > 0 for v in here):
        oi = {"required": True, "reason": "SERVICE_INFEASIBLE_WITHIN_LIMITS",
              "binding_constraint": "not the allowance: EV window/charger rating, water supply or tank limits",
              "current_allowance_model_kw": cap, "shortfall_within_allowance": dict(zip(
                  ("intervals_below_critical", "intervals_below_reserve", "ev_shortfall_model_kwh"), here)),
              "allowance_that_would_reduce_shortfall_model_kw": None,
              "who_can_approve": "operator/manager decision on the requirement; no schedule meets it",
              "until_approved": "the shortfall above is expected"}
    # requires_operator_exception keeps its contract meaning: THIS option exceeds the allowance (rules plans never do).
    # An allowance that blocks service is reported in operator_intervention; the within-cap plan stays acceptable.
    opt["operator_intervention"] = oi
    for row in opt["constraint_report"]:
        if row["class"] == "allowance":
            row["status"] = status if status != "not_binding" or row["status"] == "satisfied" else row["status"]
    return out


def _plan_with_margin(current_state, known_events, requirements, constraints):
    """Plan with the readiness margin; if the margin would worsen water or EV service, use the no-margin plan and
    say so in the constraint report (service outranks the margin)."""
    out = _plan_once(current_state, known_events, requirements, constraints)
    if float(constraints.get("reserve_margin_l", 0.0)) <= 0:
        return out
    base = _plan_once(current_state, known_events, requirements, {**constraints, "reserve_margin_l": 0.0})
    if all(x <= y for x, y in zip(_service(out), _service(base))):
        return out
    opt = base["options"][0]
    opt["water_shortfall"]["readiness_margin_l"] = float(constraints["reserve_margin_l"])
    for row in opt["constraint_report"]:
        if row["class"] == "readiness_margin":
            row.update(status="dropped_to_protect_service", rule=f"tank >= reserve + {constraints['reserve_margin_l']:g} L")
    return base


def _plan_once(current_state, known_events, requirements, constraints):
    t0 = time.perf_counter()
    st, c = current_state, constraints
    start, end = st["interval_index"], c["horizon_intervals"]
    dt_h = c["model_dt_minutes"] / 60
    idx = list(range(start, end))
    peak = set(known_events.get("peak_intervals", []))
    source_ok = known_events.get("source_water_available", {})  # interval -> bool (known outages)
    withdraw = c["expected_withdrawal_l"]
    allow = c["allowance_model_kw"]
    # Optional, separately labelled demand-aware variant: a per-interval cap on the participating load derived from a
    # whole-connection demand target minus the forecast non-flexible load. Absent: the scalar allowance only.
    daa = c.get("demand_aware_allowance_kw")

    def cap(i):
        return min(allow, daa[i]) if daa is not None else allow
    pump_kw, ev_kw, inflow = c["pump_model_kw"], c["ev_charger_model_kw"], c["pump_inflow_lph"] * dt_h
    pump = {i: False for i in idx}
    ev = {i: None for i in idx}  # ev_id charging in interval (one charger)
    infeas = []

    def load(i):
        return (pump_kw if pump[i] else 0) + (ev_kw if ev[i] else 0)

    def tank_path(floor=None):
        floor = c["tank_min_l"] if floor is None else floor
        v, path, unmet, viol = st["tank_l"], [], 0.0, []
        for i in idx:
            if pump[i] and source_ok.get(i, True):
                v += inflow
            v = min(v, c["tank_max_l"])  # overflow protection is physical, independent of the plan
            w = withdraw[i]
            served = min(w, v)
            unmet += w - served
            v -= served
            path.append(round(v, 1))
            if v < floor - EPS:
                viol.append(i)
        return path, unmet, viol

    def usable(i):
        return not pump[i] and source_ok.get(i, True) and load(i) + pump_kw <= cap(i) + EPS

    def overflows(extra):
        """Would pumping in the existing slots plus `extra` push the planned tank above capacity anywhere?"""
        v = st["tank_l"]
        for i in idx:
            if (pump[i] or i in extra) and source_ok.get(i, True):
                v += inflow
                if v > c["tank_max_l"] + EPS:
                    return True
            v -= min(withdraw[i], v)
        return False

    # Pump duration rules (demonstration assumptions supplied by the plant parameters; defaults keep older callers working).
    min_on = int(c.get("pump_min_on_intervals", MIN_RUN))
    min_off = int(c.get("pump_min_off_intervals", 0))
    # Last observed ON interval before the plan, as a virtual index: running now -> start-1; stopped k intervals ago
    # -> start-1-k; unknown state or no stop yet -> None (no restart lockout assumed; never invented).
    if st.get("pump_on") is True:
        last_on_before = start - 1
    elif st.get("pump_on") is False and st.get("pump_off_intervals") is not None:
        last_on_before = start - 1 - int(st["pump_off_intervals"])
    else:
        last_on_before = None
    exceptions = []

    def on_before(s):
        prior = [i for i in idx if i < s and pump[i]]
        return prior[-1] if prior else last_on_before

    def on_after(e):
        later = [i for i in idx if i > e and pump[i]]
        return later[0] if later else None

    def gap_ok(s, e):  # an OFF gap shorter than min_off on either side would trip the restart lockout
        b, a = on_before(s), on_after(e)
        return (b is None or b == s - 1 or s - b - 1 >= min_off) and (a is None or a == e + 1 or a - e - 1 >= min_off)

    def joins_run(s, e):
        return on_before(s) == s - 1 or on_after(e) == e + 1

    # 0) minimum run: a pump observed running for fewer than min_on intervals stays planned ON until min_on is
    #    reached, except where a known outage or tank capacity stops it (safety stops take precedence).
    if st.get("pump_on") is True and st.get("pump_run_intervals") is not None:
        for i in idx[:max(0, min_on - int(st["pump_run_intervals"]))]:
            if not usable(i) or overflows({i}):
                break
            pump[i] = True

    margin = float(c.get("reserve_margin_l", 0.0))
    floor = c["tank_min_l"] + margin

    def water_pass(level, target_only):
        """Before the first interval below `level`, add one contiguous run of min_on intervals (truncated only by the
        horizon end). Reserve pass (target_only=False): runs outside advisory hours first, then fewest advisory
        intervals (equipment duration rules outrank advisory preference); if no full run fits, extend an existing run
        by one slot, and only then add a short run, reported in pump_duration_exceptions. Readiness pass
        (target_only=True): full-length runs outside advisory hours only, no fallbacks. Within a tier: extend an
        existing run, then finish by the need, then the latest. Every run respects min_off gaps, the allowance,
        known outages and tank capacity."""
        for _ in range(len(idx)):
            path, _, low = tank_path(level)
            if not low:
                return
            first = low[0]
            runs = []
            for s in idx:
                if s > first:
                    break
                r = list(range(s, min(s + min_on, end)))
                if (len(r) == min_on or r[-1] == end - 1) and all(usable(j) and not (target_only and j in peak)
                                                                 for j in r) \
                        and gap_ok(r[0], r[-1]) and not overflows(set(r)):
                    runs.append(r)
            if runs:
                runs.sort(key=lambda r: (sum(j in peak for j in r), not joins_run(r[0], r[-1]), r[-1] > first, -r[0]))
                for j in runs[0]:
                    pump[j] = True
                continue
            if target_only:
                return
            cands = [i for i in idx if i <= first and usable(i) and gap_ok(i, i)
                     and _fits(path, idx, i, inflow, c["tank_max_l"])]
            if not cands:
                return
            cands.sort(key=lambda i: (not joins_run(i, i), i in peak, -i))
            i = cands[0]
            if not joins_run(i, i) and min_on > 1:
                exceptions.append({"kind": "MIN_ON_NOT_MET", "interval_index": i,
                                   "reason": "no full-length run fits before the water need"})
            pump[i] = True

    # 1) water: aim for reserve + readiness margin first (moves the same runs earlier where possible), then make sure
    #    the true reserve holds with the full fallback order.
    if margin > 0:
        water_pass(floor, True)
    water_pass(c["tank_min_l"], False)
    path, unmet_water, viol = tank_path()
    if viol:
        infeas.append({"kind": "WATER_RESERVE", "intervals_below_reserve": len(viol),
                       "first_interval": viol[0], "unmet_withdrawal_l": round(unmet_water, 1)})

    # 2) EV energy before departure (earliest-deadline first), non-peak preferred.
    ev_traj, shortfall = {}, {}
    for s in sorted(requirements.get("ev_sessions", []), key=lambda e: e["departure_interval"]):
        need = s["remaining_model_kwh"]
        slots = [i for i in idx if s["arrival_interval"] <= i < s["departure_interval"] and ev[i] is None
                 and load(i) + ev_kw <= cap(i) + EPS]
        slots.sort(key=lambda i: (i in peak, i))
        for i in slots:
            if need <= EPS:
                break
            ev[i] = s["ev_id"]
            need -= min(ev_kw * dt_h, need)
        shortfall[s["ev_id"]] = round(max(need, 0.0), 3)
        if need > EPS:
            infeas.append({"kind": "EV_ENERGY", "ev_id": s["ev_id"], "shortfall_model_kwh": round(need, 3)})
        got, cum = s["remaining_model_kwh"], 0.0
        traj = []
        for i in idx:
            if ev[i] == s["ev_id"]:
                cum = min(got, cum + ev_kw * dt_h)
            traj.append(round(cum, 3))
        ev_traj[s["ev_id"]] = traj

    path, unmet_water, viol = tank_path()
    _, _, below_margin = tank_path(floor)
    critical = float(c.get("tank_critical_l", 0.0))
    _, _, below_critical = tank_path(critical) if critical > 0 else (None, None, [])

    loads = [load(i) for i in idx]
    starts, prev = 0, bool(st.get("pump_on"))  # unknown pump state counts as OFF for planned starts
    for i in idx:
        starts += pump[i] and not prev
        prev = pump[i]
    option = {
        "option_id": "o1",
        "label": "Rules schedule: water reserve first, then EV energy, outside peak windows when possible",
        "effective_from_interval": start,
        "actions": [{"interval_index": i, "pump": "ON" if pump[i] else "OFF", "ev": ev[i] or "OFF"} for i in idx],
        "predicted_peak_model_kw": round(max(loads, default=0), 3),
        "predicted_peak_window_energy_model_kwh": round(sum(l * dt_h for i, l in zip(idx, loads) if i in peak), 3),
        "water_trajectory_model_l": path,
        "predicted_min_tank_model_l": min(path, default=st["tank_l"]),
        "ev_energy_trajectory_model_kwh": ev_traj,
        "ev_shortfall_model_kwh": shortfall,
        "water_shortfall": {"intervals_below_reserve": len(viol), "unmet_withdrawal_l": round(unmet_water, 1),
                            "intervals_below_readiness_margin": len(below_margin), "readiness_margin_l": margin,
                            "intervals_below_critical": len(below_critical), "critical_level_l": critical},
        "pump_starts": {"history": st.get("pump_starts_history"), "planned": starts},  # history None = not measurable
        "pump_duration_rules": {"min_on_intervals": min_on, "min_off_intervals": min_off,
                                "basis": "demonstration assumption, not a real-pump specification"},
        "pump_duration_exceptions": exceptions,
        "requires_operator_exception": False,
        "constraint_report": [
            {"class": "physical_safety", "rule": "no planned pumping in known outages; no planned overflow; "
             "verified electrical limit if configured", "basis": "hard",
             "status": "satisfied" if all(not (pump[i] and not source_ok.get(i, True)) for i in idx)
             and max(path, default=0) <= c["tank_max_l"] + EPS else "violated",
             "detail": {"verified_electrical_limit_model_kw": c.get("verified_electrical_limit_model_kw"),
                        "note": "no verified electrical limit is configured" if not
                        c.get("verified_electrical_limit_model_kw") else "verified limit applied"}},
            {"class": "equipment_limits", "rule": f"pump min on {min_on} / min off {min_off} intervals",
             "basis": "demonstration assumption", "status": "exceptions" if exceptions else "satisfied",
             "detail": exceptions},
            {"class": "allowance", "rule": f"participating load <= {allow} model kW",
             "basis": "operator-configured participating-load limit (hard); not BESCOM-sanctioned load or transformer "
                      "capacity",
             "status": "satisfied" if max(loads, default=0) <= allow + EPS else "violated"},
            {"class": "service_water_critical", "rule": f"tank >= critical level {critical:g} L",
             "basis": "service requirement (demonstration assumption)",
             "status": "shortfall" if below_critical else "satisfied", "detail": {"intervals_below": len(below_critical)}},
            {"class": "service_water_reserve", "rule": f"tank >= ordinary reserve {c['tank_min_l']} L",
             "basis": "service requirement", "status": "shortfall" if viol else "satisfied",
             "detail": {"intervals_below_reserve": len(viol)}},
            {"class": "service_ev_deadline", "rule": "requested energy before each departure", "basis": "service requirement",
             "status": "shortfall" if any(v > EPS for v in shortfall.values()) else "satisfied", "detail": shortfall},
            {"class": "advisory_windows", "rule": "avoid advisory peak intervals", "basis": "preference",
             "status": "used" if any(loads[k] > 0 and i in peak for k, i in enumerate(idx)) else "avoided",
             "detail": {"planned_energy_model_kwh": round(sum(ld * dt_h for i, ld in zip(idx, loads) if i in peak), 3)}},
            {"class": "readiness_margin", "rule": f"tank >= reserve + {margin:g} L", "basis": "demonstration assumption",
             "status": "not_met" if below_margin else "met", "detail": {"intervals_below": len(below_margin)}},
        ],
        "assumptions": ["expected withdrawal profile is a planning estimate",
                        "source water available unless a known outage event says otherwise",
                        "manual switching; acceptance does not operate equipment"],
    }
    return {"source": "rules", "options": [option], "infeasibilities": infeas,
            "computation_ms": round((time.perf_counter() - t0) * 1000, 2)}


def _fits(path, idx, i, inflow, vmax):
    """Adding inflow at i must not push the planned tank above capacity at i (no planned overflow)."""
    before = path[idx.index(i) - 1] if idx.index(i) > 0 else None
    return before is None or before + inflow <= vmax + EPS
