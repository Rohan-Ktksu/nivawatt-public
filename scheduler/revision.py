"""Revision gate: ask the manager to accept a new plan only for a material, actionable change.

Shared by the API (backend.main.replan) and the simulation coordinator (simulation.experiment), so both apply the same
rules. Callers only evaluate a candidate after a MEASURED deviation; a missing reading alone never triggers one.
Uncertainty stays visible (STALE / UNKNOWN / unverified intervals); acknowledgements are never treated as readings.

Decisions for a candidate plan compared with continuing the active plan from the same observed state:
  SUPPRESSED_EQUIVALENT     remaining actions are identical: nothing to accept
  PROPOSE (urgent)          continuing the active plan would breach the water reserve within ACTION_WINDOW
                            intervals and the candidate predicts fewer breach intervals, or the candidate predicts at
                            least EV_TOL_WH (100 Wh) less EV shortfall, or the active plan exceeds the current operator
                            allowance and the candidate does not: proposed even during the cooldown
Deferred decisions (SUPPRESSED_NOT_ACTIONABLE, DEFERRED_COOLDOWN) are re-evaluated at every later interval by the
API, and at every interval with a fresh reading by the simulation, until proposed or no longer needed.
  SUPPRESSED_NOT_ACTIONABLE the first changed action is ACTION_WINDOW or more intervals away: reconsidered later
  DEFERRED_COOLDOWN         a non-urgent change within COOLDOWN intervals of the last accepted plan
  SUPPRESSED_NO_IMPROVEMENT a non-urgent change that improves none of: reserve breaches (later than the window),
                            EV shortfall, advisory-window energy, pump starts
  PROPOSE                   otherwise
Window, cooldown and tolerance values are DEMONSTRATION CHOICES, not validated human-factors parameters.
"""
ACTION_WINDOW = 8   # intervals (2 h at 15 min)
COOLDOWN = 4        # intervals (1 h) after an accepted plan
# Materiality comparison rule: predicted energies are compared in whole watt-hours (each value rounded to the nearest
# Wh with Python round()), and a reduction of at least the threshold is material. 100 Wh (0.1 kWh) counts; 99 Wh does
# not. Comparing whole Wh avoids floating-point artefacts without an ad-hoc epsilon.
EV_TOL_WH = 100
PEAK_TOL_WH = 100


def _wh(kwh):
    return int(round(kwh * 1000))


def predict(actions, state, c, reqs, known=None):
    """Service outcome of following `actions` from the current observed state, with the planner's expectations."""
    known = known or {}
    src = known.get("source_water_available", {})
    peak = set(known.get("peak_intervals", []))
    dt_h = c["model_dt_minutes"] / 60
    inflow = c["pump_inflow_lph"] * dt_h
    by_i = {a["interval_index"]: a for a in actions}
    v, below, peak_kwh, starts, first_below, max_kw = float(state["tank_l"]), 0, 0.0, 0, None, 0.0
    prev = bool(state.get("pump_on"))
    got = {s["ev_id"]: 0.0 for s in reqs.get("ev_sessions", [])}
    for i in range(state["interval_index"], c["horizon_intervals"]):
        a = by_i.get(i, {"pump": "OFF", "ev": "OFF"})
        on = a["pump"] == "ON"
        starts += on and not prev
        prev = on
        if on and src.get(i, True):
            v = min(v + inflow, c["tank_max_l"])
        v -= min(c["expected_withdrawal_l"][i], v)
        if v < c["tank_min_l"] - 1e-9:
            below += 1
            first_below = i if first_below is None else first_below
        kw = c["pump_model_kw"] if on else 0.0
        e = a.get("ev", "OFF")
        if e != "OFF":
            s = next((x for x in reqs.get("ev_sessions", []) if x["ev_id"] == e), None)
            if s and s["arrival_interval"] <= i < s["departure_interval"]:
                add = min(c["ev_charger_model_kw"] * dt_h, max(0.0, s["remaining_model_kwh"] - got[e]))
                got[e] += add
                kw += add / dt_h
        if i in peak:
            peak_kwh += kw * dt_h
        max_kw = max(max_kw, kw)
    short = sum(max(0.0, s["remaining_model_kwh"] - got[s["ev_id"]]) for s in reqs.get("ev_sessions", []))
    return {"intervals_below_reserve": below, "first_reserve_breach_interval": first_below,
            "ev_shortfall_model_kwh": round(short, 3),
            "advisory_window_energy_model_kwh": round(peak_kwh, 3), "pump_starts": starts,
            "max_participating_model_kw": round(max_kw, 4)}


def assess(active_actions, candidate_actions, state, c, reqs, known, last_accept_interval):
    now = state["interval_index"]
    act = {a["interval_index"]: (a["pump"], a["ev"]) for a in active_actions if a["interval_index"] >= now}
    cand = {a["interval_index"]: (a["pump"], a["ev"]) for a in candidate_actions if a["interval_index"] >= now}
    changed = sorted(i for i in set(act) | set(cand) if act.get(i, ("OFF", "OFF")) != cand.get(i, ("OFF", "OFF")))
    out = {"at_interval": now, "first_changed_interval": changed[0] if changed else None, "changed_intervals": len(changed)}
    if not changed:
        return {**out, "decision": "SUPPRESSED_EQUIVALENT", "urgent": False}
    pa = predict(active_actions, state, c, reqs, known)
    pc = predict(candidate_actions, state, c, reqs, known)
    out.update(active_prediction=pa, candidate_prediction=pc)
    fewer_breaches = pc["intervals_below_reserve"] < pa["intervals_below_reserve"]
    less_ev_short = _wh(pa["ev_shortfall_model_kwh"]) - _wh(pc["ev_shortfall_model_kwh"]) >= EV_TOL_WH
    imminent = pa["first_reserve_breach_interval"] is not None and pa["first_reserve_breach_interval"] < now + ACTION_WINDOW
    cap = c["allowance_model_kw"]
    fixes_cap = pa["max_participating_model_kw"] > cap + 1e-9 >= pc["max_participating_model_kw"]
    urgent = (fewer_breaches and imminent) or less_ev_short or fixes_cap
    if urgent:
        return {**out, "decision": "PROPOSE", "urgent": True}
    if changed[0] >= now + ACTION_WINDOW:
        return {**out, "decision": "SUPPRESSED_NOT_ACTIONABLE", "urgent": False}
    if last_accept_interval is not None and now - last_accept_interval < COOLDOWN:
        return {**out, "decision": "DEFERRED_COOLDOWN", "urgent": False}
    better = (fewer_breaches or less_ev_short
              or _wh(pa["advisory_window_energy_model_kwh"]) - _wh(pc["advisory_window_energy_model_kwh"]) >= PEAK_TOL_WH
              or pc["pump_starts"] < pa["pump_starts"])
    return {**out, "decision": "PROPOSE" if better else "SUPPRESSED_NO_IMPROVEMENT", "urgent": False}
