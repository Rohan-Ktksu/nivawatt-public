"""Illustrative monthly cost comparison: baseline vs coordinated operation on independently evolving simulated plants.

Synthetic simulation only. It is not an apartment-specific estimate and not a bill. The protocol below was declared
and committed before any comparison was computed. Each policy runs its own plant for PROTOCOL["days"] consecutive days:
the tank level carries over from one day to the next per policy; the external conditions (withdrawal draws, EV
requests, advisory hours) are identical across policies.

billing-scenario-v1 (SUPERSEDED) started every simulated day with the pump switched off and no on/off history.
billing-scenario-v2 carries the pump's on/off state and its run / off-interval history across midnight, so minimum
on/off rules and start counts continue; a pump already running at midnight is not counted as a new start. EV charging
needs no carry-over (each EV leaves at 08:00 and the next arrives at 00:00). Each policy's own planning state (the
advisory plan) restarts daily, as a day-ahead advisory process would.

Costs use billing.calculator in "participating_loads" scope (energy-related charges only). Fixed/demand charges belong
to the whole connection and are not compared. FPPCA has no verified monthly value and is reported as unknown.
"""
import json
import subprocess
from pathlib import Path

from billing.calculator import calculate
from simulation import experiment, plant

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = {
    "id": "billing-scenario-v2",
    "kind": "illustrative synthetic simulation (not apartment-specific, not a bill)",
    "days": 30,
    "chained_days": True,
    "carry_equipment_state": True,
    "policies": {"baseline": "uncoordinated", "coordinated": "measured_replan"},
    "apartment": "simulation/plant.py DEFAULT_PARAMS (10,000 L tank, 3,000 L reserve, 1,000 L critical, 2.2 kW pump "
                 "moving 6,000 L/h, 3.3 kW charger, 6 kW participating-load allowance, min on/off 4/2 intervals)",
    "initial_tank_l": 4500,
    "profile": "simulation/plant.py default_withdrawals (synthetic; identical every day: no weekday/weekend/holiday "
               "differentiation exists in the profile, so none is modelled; each day weighted equally)",
    "advisory_peak_intervals": list(range(40, 52)),
    "advisory_note": "10:00-13:00 IST every day; advisory hours carry no bill value under LT-1 (no time-of-day tariff)",
    "ev": "one EV per day, arriving 00:00, leaving 08:00 (intervals 0-32), requested energy per scenario",
    "scenarios": {
        "low": {"withdrawal_scale": 0.85, "withdrawal_noise": 0.10, "ev_kwh": 7, "p_miss_ev": 0.0},
        "base": {"withdrawal_scale": 1.00, "withdrawal_noise": 0.15, "ev_kwh": 10, "p_miss_ev": 0.0},
        "high": {"withdrawal_scale": 1.15, "withdrawal_noise": 0.30, "ev_kwh": 13, "p_miss_ev": 0.0},
        "base_missed_ev": {"withdrawal_scale": 1.00, "withdrawal_noise": 0.15, "ev_kwh": 10, "p_miss_ev": 0.15},
    },
    "execution": "perfect human execution except base_missed_ev (15% of instructed EV actions skipped, reported done)",
    "seed": "day d of scenario s uses seed 7000 + 100*s + d (s in declaration order)",
    "tariff": {"category": "LT-1", "period_start": "2026-06-01", "period_end": "2026-07-01",
               "scope": "participating_loads", "fppca": "unknown (no verified monthly value)",
               "tax": "9% of the energy charge (unverified assumption)"},
    "comparability": {
        "unmet_withdrawal_l_tolerance": 1.0,
        "reserve_violation_intervals": "must be equal",
        "ev_energy_per_session_tolerance_kwh": 0.05,
        "storage": "final tank difference converted to pump energy at 2.2 kW per 6,000 L/h and reported as a "
                   "storage-adjusted energy difference; raw difference also shown",
    },
}
# The superseded first protocol, kept so its committed result can be reproduced from this code.
PROTOCOL_V1 = {**PROTOCOL, "id": "billing-scenario-v1", "carry_equipment_state": False,
               "status": "SUPERSEDED by billing-scenario-v2 (pump state was reset every midnight)",
               "tariff": {**PROTOCOL["tariff"], "period_start": "2026-09-01", "period_end": "2026-10-01"}}
PROTOCOLS = {PROTOCOL["id"]: PROTOCOL, PROTOCOL_V1["id"]: PROTOCOL_V1}

# ---------- Fictional 100-flat study (agreed configuration; all inputs are ASSUMPTIONS, not measured apartment facts) ----------
_DAY_START = 48  # simulated day = 12:00 -> 12:00 IST, so the 19:00-07:00 EV window is never split by a day boundary
_ADVISORY_CLOCK = list(range(40, 52))  # 10:00-13:00 IST, the project's advisory hours
FLATS_100 = {
    "id": "flats-100-v1",
    "kind": "FICTIONAL illustrative study (agreed configuration); every input below is an assumption, not a measured "
            "apartment fact; not a bill",
    "days": 30,
    "chained_days": True,
    "carry_equipment_state": True,
    "day_boundary": "Simulated days run 12:00 to 12:00 IST. Real midnight falls inside a simulated day, so tank, pump and "
                    "EV state are continuous across it; tank level and pump on/off state with its minimum on/off history "
                    "carry across the 12:00 boundary, where no EV is connected (window 19:00-07:00).",
    "day_start_interval": _DAY_START,
    "policies": {"baseline": "uncoordinated", "coordinated": "measured_replan"},
    "assumptions": {
        "flats": 100, "residents": 400, "daily_water_demand_l": 54000,
        "water_profile": "the project's synthetic daily shape (simulation/plant.py default_withdrawals) scaled to "
                         "54,000 L/day; morning and evening peaks; identical every day (no weekday/weekend/holiday "
                         "distinction), withdrawal noise +-15% per interval",
        "tank_l": 30000, "pump_kw": 5.5, "pump_delivery_l_per_h": 10000,
        "reserve_levels_l": {"critical": 3000, "ordinary_reserve": 8000, "readiness_target": 12000},
        "initial_conditions": {"tank_l": 18000, "pump": "off", "ev": "none connected (first arrives 19:00 on day 1)"},
        "ev": "one 7.4 kW charger; one EV per night connected 19:00-07:00 needing 15 kWh of metered electrical energy "
              "(charger energy, not battery state of charge)",
        "pump_rules": "minimum on 4 intervals (1 h), minimum off 2 intervals (30 min): demonstration assumptions",
        "participating_load_allowance_kw": 13.0,
        "allowance_note": "set above the 12.9 kW connected participating load so it never binds; it is an operator "
                          "planning limit, NOT sanctioned load, contract demand or the utility's billing demand",
        "level_cutoff": "a running pump stops when the tank reaches full inside a 15-minute step (physical level switch); "
                        "energy and inflow are pro-rated, so no water is pumped into overflow",
        "other_common_loads": "lifts, corridor/stair/parking lighting, sewage treatment, fire-fighting pumps, gates, CCTV "
                              "and flats' own loads are NOT modelled; they would add energy and set the connection's "
                              "maximum demand",
        "billing_boundary": "pump circuit + EV charger circuit, metered electrical energy only (circuit energy cost). "
                            "Not the apartment's bill: fixed/demand charges, other common loads and the FY25 true-up "
                            "are outside the boundary; FPPCA has no verified rate. The connection category is unknown, "
                            "so energy-related charges are shown under LT-1 and HT-4 assumptions.",
    },
    "apartment_params": {"tank_max_l": 30000, "tank_min_l": 8000, "tank_critical_l": 3000, "reserve_margin_l": 4000,
                         "pump_model_kw": 5.5, "pump_inflow_lph": 10000, "ev_charger_model_kw": 7.4,
                         "allowance_model_kw": 13.0, "pump_level_cutoff_within_interval": True},
    "daily_withdrawal_l": 54000,
    "initial_tank_l": 18000,
    "ev_session": {"arrival_interval": (76 - _DAY_START) % 96, "departure_interval": (124 - _DAY_START) % 96 or 96},
    "advisory_peak_intervals": sorted((i - _DAY_START) % 96 for i in _ADVISORY_CLOCK),
    "advisory_note": "10:00-13:00 IST every day; no bill value under LT-1 or HT-4 (no time-of-day tariff)",
    "scenarios": {"agreed": {"withdrawal_scale": 1.0, "withdrawal_noise": 0.15, "ev_kwh": 15, "p_miss_ev": 0.0}},
    "execution": "perfect human execution",
    "seed": "day d uses seed 7000 + d",
    "tariff": {"category": "LT-1", "period_start": "2026-06-01", "period_end": "2026-07-01",
               "scope": "participating_loads", "fppca": "unknown (no verified monthly value)",
               "tax": "9% of the energy charge (unverified assumption)"},
    "tariff_categories": ["LT-1", "HT-4"],
    "comparability": PROTOCOL["comparability"],
}
PROTOCOLS[FLATS_100["id"]] = FLATS_100


def feasibility(proto=FLATS_100):
    """Can the stated equipment meet the stated demand at all? Computed from the protocol's own inputs."""
    a = proto["apartment_params"]
    raw = plant.default_withdrawals(96)
    k = proto["daily_withdrawal_l"] / sum(raw)
    peak_lph = max(raw) * k * 4
    ev = proto["ev_session"]
    ev_h_needed = proto["scenarios"]["agreed"]["ev_kwh"] / a["ev_charger_model_kw"]
    window_h = (ev["departure_interval"] - ev["arrival_interval"]) / 4
    pump_h = proto["daily_withdrawal_l"] / a["pump_inflow_lph"]
    checks = {
        "pump_hours_per_day_needed": round(pump_h, 2),
        "pump_capacity_l_per_day": a["pump_inflow_lph"] * 24,
        "peak_hour_demand_l_per_h": round(peak_lph),
        "pump_delivery_exceeds_peak_demand": a["pump_inflow_lph"] > peak_lph,
        "reserve_hours_at_peak_demand": round(a["tank_min_l"] / peak_lph, 2),
        "storage_hours_at_average_demand": round(a["tank_max_l"] / (proto["daily_withdrawal_l"] / 24), 1),
        "ev_hours_needed": round(ev_h_needed, 2), "ev_window_hours": window_h,
        "ev_fits_window": ev_h_needed <= window_h,
        "connected_participating_kw": a["pump_model_kw"] + a["ev_charger_model_kw"],
        "allowance_kw": a["allowance_model_kw"],
        "pump_energy_kwh_per_day": round(pump_h * a["pump_model_kw"], 2),
        "ev_energy_kwh_per_day": proto["scenarios"]["agreed"]["ev_kwh"],
        "initial_tank_between_reserve_and_capacity": a["tank_min_l"] < proto["initial_tank_l"] < a["tank_max_l"],
    }
    checks["feasible"] = (pump_h < 24 and checks["pump_delivery_exceeds_peak_demand"] and checks["ev_fits_window"]
                          and checks["initial_tank_between_reserve_and_capacity"])
    return checks
PUMP_KWH_PER_L = 2.2 / 6000.0


def pct(diff, base):
    """Percent change; None when the baseline is zero (no division by zero, no invented percentage)."""
    return round(100 * diff / base, 2) if base else None


def verdict(comparable, kwh_diff_excluding_overflow, tol=0.01):
    """Coordinated minus baseline: a positive kWh difference is a cost increase (negative saving)."""
    if not comparable:
        return "not estimable (service not comparable)"
    if abs(kwh_diff_excluding_overflow) < tol:
        return "zero within rounding: the whole difference is baseline overflow, a 15-minute modelling artefact"
    kind = "negative" if kwh_diff_excluding_overflow > 0 else "positive"
    return f"{kind} saving of {abs(kwh_diff_excluding_overflow):.3f} kWh after removing overflow"


def _day(policy, params, w, d, scen, peaks, initial=None):
    m = experiment.run_policy(policy, params, w, d, scen, peaks, initial=initial)
    log = m["truth_log"]
    dt_h = params["model_dt_minutes"] / 60
    kwh = sum(r["pump_model_kw"] + r["ev_model_kw"] for r in log) * dt_h
    peak = sum(r["pump_model_kw"] + r["ev_model_kw"] for r in log if r["interval_index"] in peaks) * dt_h
    req = {s["ev_id"]: s["requested_model_kwh"] for s in params["ev_sessions"]}
    delivered = {e: req[e] - (m["ev_shortfall_at_departure_model_kwh"][e] or 0.0) for e in req}
    return {"kwh": kwh, "peak_kwh": peak, "peak_kw": m["peak_participating_model_kw"], "final_tank_l": m["final_tank_l"], "unmet_l": m["unmet_withdrawal_l"],
            "reserve_violations": m["reserve_violation_intervals"], "ev_delivered": delivered,
            "ev_shortfall": sum(v or 0 for v in m["ev_shortfall_at_departure_model_kwh"].values()),
            "pump_starts": m["pump_starts"], "overflow_l": log[-1]["overflow_l"],
            "equipment": m["final_equipment_state"], "pump_on_at_midnight": bool(log[-1]["pump_model_kw"] > 0)}


def run_scenario(name, s_index, cfg, protocol=PROTOCOL):
    PROTOCOL = protocol  # noqa: N806  (the protocol this run follows)
    overrides = PROTOCOL.get("apartment_params", {})
    base = plant.make_params(overrides)
    raw = plant.default_withdrawals(base["horizon_intervals"])
    if PROTOCOL.get("daily_withdrawal_l"):  # scale the synthetic shape to the stated daily demand
        k = PROTOCOL["daily_withdrawal_l"] / sum(raw)
        raw = [x * k for x in raw]
    shift = PROTOCOL.get("day_start_interval", 0)  # simulated day starts at this clock interval
    raw = raw[shift:] + raw[:shift]
    profile = [round(x * cfg["withdrawal_scale"], 1) for x in raw]
    pump_kwh_per_l = base["pump_model_kw"] / base["pump_inflow_lph"]
    ev = PROTOCOL.get("ev_session", {"arrival_interval": 0, "departure_interval": 32})
    scen = {"p_miss_pump": 0, "p_miss_ev": cfg["p_miss_ev"], "p_drop": 0, "withdrawal_noise": cfg["withdrawal_noise"],
            "stochastic": True}
    peaks = PROTOCOL["advisory_peak_intervals"]
    tanks = {k: float(PROTOCOL["initial_tank_l"]) for k in PROTOCOL["policies"]}
    assert all(t <= base["tank_max_l"] for t in tanks.values()), "initial tank above capacity"
    equipment = {k: None for k in PROTOCOL["policies"]}  # None: the first day starts with the pump off
    days = {k: [] for k in PROTOCOL["policies"]}
    session_mismatch = 0
    for d in range(PROTOCOL["days"]):
        seed = 7000 + 100 * s_index + d
        res = {}
        for role, policy in PROTOCOL["policies"].items():
            p = plant.make_params({**overrides, "withdrawal_profile_l": profile,
                                   "tank_init_l": round(min(max(tanks[role], 0.0), base["tank_max_l"]), 1),
                                   "ev_sessions": [{"ev_id": "EV_1", **ev, "requested_model_kwh": cfg["ev_kwh"]}]})
            draws, w = experiment.draws_for(seed, p["horizon_intervals"], scen, p["withdrawal_profile_l"])
            res[role] = _day(policy, p, w, draws, scen, peaks,
                             initial=equipment[role] if PROTOCOL["carry_equipment_state"] else None)
            tanks[role] = res[role]["final_tank_l"]
            equipment[role] = res[role]["equipment"]
            days[role].append(res[role])
        b, c = res["baseline"]["ev_delivered"], res["coordinated"]["ev_delivered"]
        session_mismatch += sum(abs(b[e] - c[e]) > PROTOCOL["comparability"]["ev_energy_per_session_tolerance_kwh"] for e in b)
    out = {"scenario": name, "config": cfg}
    for role in PROTOCOL["policies"]:
        ds = days[role]
        out[role] = {"policy": PROTOCOL["policies"][role], "kwh": round(sum(x["kwh"] for x in ds), 3),
                     "peak_window_kwh": round(sum(x["peak_kwh"] for x in ds), 3),
                     "max_15min_circuit_kw": max(x["peak_kw"] for x in ds),
                     "level_cutoff_model": bool(overrides.get("pump_level_cutoff_within_interval")),
                     "unmet_withdrawal_l": round(sum(x["unmet_l"] for x in ds), 1),
                     "reserve_violation_intervals": sum(x["reserve_violations"] for x in ds),
                     "ev_shortfall_kwh": round(sum(x["ev_shortfall"] for x in ds), 3),
                     "pump_starts": sum(x["pump_starts"] for x in ds),
                     "overflow_l": round(sum(x["overflow_l"] for x in ds), 1),
                     "pump_on_at_midnight_days": sum(x["pump_on_at_midnight"] for x in ds),
                     "initial_tank_l": PROTOCOL["initial_tank_l"], "final_tank_l": ds[-1]["final_tank_l"]}
    b, c = out["baseline"], out["coordinated"]
    raw_diff = round(c["kwh"] - b["kwh"], 3)
    storage_kwh = round((c["final_tank_l"] - b["final_tank_l"]) * pump_kwh_per_l, 3)
    adj_diff = round(raw_diff - storage_kwh, 3)  # coordinated minus baseline, after crediting water left in the tank
    tol = PROTOCOL["comparability"]
    reasons = []
    if abs(c["unmet_withdrawal_l"] - b["unmet_withdrawal_l"]) > tol["unmet_withdrawal_l_tolerance"]:
        reasons.append("unmet water differs")
    if c["reserve_violation_intervals"] != b["reserve_violation_intervals"]:
        reasons.append(f"intervals below reserve differ ({b['reserve_violation_intervals']} vs {c['reserve_violation_intervals']})")
    if session_mismatch:
        reasons.append(f"EV energy at departure differs by more than {tol['ev_energy_per_session_tolerance_kwh']} kWh "
                       f"on {session_mismatch} day(s)")
    if overrides.get("pump_level_cutoff_within_interval") and max(b["overflow_l"], c["overflow_l"]) > 1.0:
        reasons.append("overflow present although the level cutoff is modelled (accounting error)")
    comparable = not reasons
    costs = {}
    for role, kwh in (("baseline", b["kwh"]), ("coordinated", c["kwh"]), ("coordinated_storage_adjusted", b["kwh"] + adj_diff)):
        r = calculate({"category": "LT-1", "period_start": PROTOCOL["tariff"]["period_start"],
                       "period_end": PROTOCOL["tariff"]["period_end"], "energy_kwh": max(kwh, 0.0),
                       "energy_basis": "simulated", "scope": "participating_loads", "fppca_paise_per_kwh": None})
        known = round(sum(x["amount_rs"] for x in r["lines"] if x["amount_rs"] is not None), 2)
        costs[role] = {"status": r["status"], "known_components_rs": known,
                       "components": {x["component"]: x["amount_rs"] for x in r["lines"]}, "unavailable": r["unavailable"]}
    by_cat = {}
    for cat in PROTOCOL.get("tariff_categories", ["LT-1"]):
        vals = {}
        for role, kwh in (("baseline", b["kwh"]), ("coordinated_storage_adjusted", b["kwh"] + adj_diff)):
            r = calculate({"category": cat, "period_start": PROTOCOL["tariff"]["period_start"],
                           "period_end": PROTOCOL["tariff"]["period_end"], "energy_kwh": max(kwh, 0.0),
                           "energy_basis": "simulated", "scope": "participating_loads", "fppca_paise_per_kwh": None})
            vals[role] = round(sum(x["amount_rs"] for x in r["lines"] if x["amount_rs"] is not None), 2)
        by_cat[cat] = {"known_circuit_energy_charges_rs": vals, "status": "partial (FPPCA unknown)",
                       "difference_rs": round(vals["coordinated_storage_adjusted"] - vals["baseline"], 2) if comparable else None}
    diff_rs = round(costs["coordinated_storage_adjusted"]["known_components_rs"] - costs["baseline"]["known_components_rs"], 2)
    base_rs = costs["baseline"]["known_components_rs"]
    out["comparison"] = {
        "comparability": "comparable (equal water and EV service; storage difference credited)" if comparable
        else "NOT comparable: " + "; ".join(reasons),
        "comparable": comparable,
        "energy_kwh_raw_difference": raw_diff,
        "storage_difference_l": round(c["final_tank_l"] - b["final_tank_l"], 1),
        "storage_equivalent_kwh": storage_kwh,
        "energy_kwh_storage_adjusted_difference": adj_diff,
        "costs": costs,
        "cost_difference_rs_known_components": diff_rs if comparable else None,
        "cost_difference_pct": pct(diff_rs, base_rs) if comparable else None,
        "time_shifting_effect_rs": 0.0,
        "time_shifting_note": "LT-1 has one flat energy rate (no time-of-day tariff): moving the same kWh between hours "
                              "changes the energy charge by exactly zero.",
        "energy_use_effect_rs": diff_rs if comparable else None,
        "fppca_effect": "unknown: storage-adjusted kWh difference x the month's FPPCA rate (no verified value)",
        "demand_charge_effect": "not estimable: needs the whole connection's recorded maximum demand; participating-load "
                                "meters cannot supply it and the participating-load allowance is not sanctioned load or "
                                "contract demand",
        "peak_window_kwh_difference": round(c["peak_window_kwh"] - b["peak_window_kwh"], 3),
        "max_15min_circuit_kw": {"baseline": b["max_15min_circuit_kw"], "coordinated": c["max_15min_circuit_kw"],
                                 "note": "pump + EV circuits only; not the connection's billing demand"},
        "costs_by_category": by_cat,
    }
    # Post-hoc diagnosis (added after the first run; protocol unchanged): pumped water lost as tank overflow. In this
    # model the float switch acts on 15-minute steps, so its last step of a fill can overshoot the full tank. Energy
    # spent on overflow is a modelling artefact of the baseline controller, not an equal-service saving.
    overflow_kwh = round((c["overflow_l"] - b["overflow_l"]) * pump_kwh_per_l, 3)
    excl = round(adj_diff - overflow_kwh, 3)
    out["comparison"]["overflow_l"] = {"baseline": b["overflow_l"], "coordinated": c["overflow_l"]}
    out["comparison"]["overflow_energy_kwh_difference"] = overflow_kwh
    out["comparison"]["energy_kwh_difference_excluding_overflow"] = excl
    rate = sum(x for x in costs["baseline"]["components"].values() if x is not None) / b["kwh"] if b["kwh"] else None
    out["comparison"]["cost_difference_rs_excluding_overflow"] = round(excl * rate, 2) if (comparable and rate) else None
    out["comparison"]["verdict"] = verdict(comparable, excl)
    return out


def run(protocol_id=PROTOCOL["id"]):
    proto = PROTOCOLS[protocol_id]
    rows = [run_scenario(n, i, cfg, proto) for i, (n, cfg) in enumerate(proto["scenarios"].items())]
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "billing", "simulation", "scheduler"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    ok = [r for r in rows if r["comparison"]["comparable"]]
    headline = [r["comparison"]["cost_difference_rs_excluding_overflow"] for r in ok]
    with_artefact = [r["comparison"]["cost_difference_rs_known_components"] for r in ok]
    feas = feasibility(proto) if proto is FLATS_100 else None
    return {"protocol": proto, "feasibility": feas, "software_commit": sha + ("-dirty" if dirty else ""), "scenarios": rows,
            "headline": {
                "what": "coordinated minus baseline, known circuit energy charges, equal service only, EXCLUDING the "
                        "baseline's coarse-step overflow artefact; negative = saving",
                "comparable_scenarios": len(ok), "of": len(rows),
                "cost_difference_rs": {"min": min(headline), "max": max(headline)} if headline else None,
                "verdicts": {r["scenario"]: r["comparison"]["verdict"] for r in rows},
                "not_included": ["FPPCA (no verified monthly rate)", "fixed/demand charges (whole connection)",
                                 "demand-charge effect (needs whole-connection maximum demand)"]},
            "including_overflow_artefact_rs": {"min": min(with_artefact), "max": max(with_artefact),
                                               "note": "NOT a saving: includes water the baseline pumped past a full "
                                                       "tank at 15-minute steps"} if with_artefact else None,
            "range_note": "min/base/max are scenario assumptions, not statistical confidence intervals"}


if __name__ == "__main__":
    import sys
    pid = sys.argv[1] if len(sys.argv) > 1 else PROTOCOL["id"]
    out = run(pid)
    d = ROOT / "results" / "billing_scenario" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for r in out["scenarios"]:
        c = r["comparison"]
        print(pid, r["scenario"], "| kWh b/c", r["baseline"]["kwh"], r["coordinated"]["kwh"],
              "| excl-overflow kWh", c["energy_kwh_difference_excluding_overflow"], "Rs", c["cost_difference_rs_excluding_overflow"],
              "| starts b/c", r["baseline"]["pump_starts"], r["coordinated"]["pump_starts"],
              "| midnight-on days b/c", r["baseline"]["pump_on_at_midnight_days"], r["coordinated"]["pump_on_at_midnight_days"],
              "|", c["comparability"])
