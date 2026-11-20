"""Fictional 100-flat REFERENCE study (protocol flats-100-reference-v2; v1 kept as superseded). Extends flats-100-v1 (billing/scenario.py).

Declared before scoring (see REFERENCE below). Everything is synthetic and tariff-dependent; nothing is a measured
apartment fact or a bill. Order of work fixed by the protocol:
  1. competent float-switch baseline with thresholds derived from stated engineering assumptions;
  2. fine-step physics (1-minute sub-steps) with a stability re-run at 15-second sub-steps and a diagnosis of the
     superseded flats-100-v1 reserve crossings;
  3. reference gate: baseline and NivaWatt at 100 % must both meet the service standard and be step-stable;
  4. only then the adherence variants and the whole-connection billing cases.

Run: python -m billing.reference  -> results/billing_scenario/flats-100-reference-v2/summary.json
"""
import copy
import json
import math
import subprocess
from pathlib import Path

from billing import connection
from billing.scenario import FLATS_100
from simulation import experiment, plant

ROOT = Path(__file__).resolve().parent.parent
_A = FLATS_100["apartment_params"]
_DS = FLATS_100["day_start_interval"]


def _clock(c):  # clock interval (0 = 00:00) -> simulated-day interval (day starts 12:00)
    return (c - _DS) % 96


def _hours(a, b):  # clock hours [a, b) -> simulated-day intervals
    return sorted(_clock(c) for c in range(a * 4, b * 4))


DESIGN = {  # engineering assumptions for the float-switch controller (shared hardware for every policy)
    "sensing_min": 1, "actuation_delay_min": 3, "within_interval_burst_factor": 1.5, "safety_factor": 2.0,
    "withdrawal_noise": 0.15, "reserve_l": 8000, "critical_l": 3000,
}


def float_design(a=_A, d=DESIGN, daily_l=FLATS_100["daily_withdrawal_l"]):
    """Start/stop thresholds from response delay x demand envelope, with the checks that justify them."""
    raw = plant.default_withdrawals(96)
    peak_lpm = max(raw) * daily_l / sum(raw) / 15
    envelope = peak_lpm * (1 + d["withdrawal_noise"]) * d["within_interval_burst_factor"]
    flow_lpm = a["pump_inflow_lph"] / 60
    delay = d["sensing_min"] + d["actuation_delay_min"]
    margin = math.ceil(d["safety_factor"] * delay * envelope / 100) * 100
    start = d["reserve_l"] + margin
    overshoot = d["safety_factor"] * delay * flow_lpm  # pump keeps filling (demand taken as zero) until it stops
    stop = math.floor((a["tank_max_l"] - overshoot) / 100) * 100
    band = stop - start
    min_on, min_off = a.get("pump_min_on_intervals", 4) * 15, a.get("pump_min_off_intervals", 2) * 15
    return {
        "peak_interval_demand_l_per_min": round(peak_lpm, 1), "demand_envelope_l_per_min": round(envelope, 1),
        "pump_flow_l_per_min": round(flow_lpm, 1), "response_delay_min": delay,
        "start_margin_l": margin, "start_l": start, "stop_l": stop, "hysteresis_l": band,
        "fill_time_at_envelope_min": round(band / (flow_lpm - envelope)), "drain_time_at_envelope_min": round(band / envelope),
        "min_on_min": min_on, "min_off_min": min_off,
        "min_on_satisfied_by_band": band / (flow_lpm - envelope) >= min_on,
        "min_off_cannot_delay_a_float_start": band / envelope >= min_off,
        "overshoot_allowance_l": round(overshoot), "tank_full_cutoff_l": a["tank_max_l"],
    }


FD = float_design()
FINE = {"substep_min": 1, "sensing_min": DESIGN["sensing_min"], "start_l": FD["start_l"], "stop_l": FD["stop_l"],
        "actuation_delay_min": DESIGN["actuation_delay_min"], "reserve_l": DESIGN["reserve_l"], "critical_l": DESIGN["critical_l"]}

CONN_SPEC = {  # non-flexible load assumptions (expected values; actual = expected x (1 +- noise))
    "flats": 100,
    "flat_kw_by_hour": [((0, 5), 0.35), ((5, 6), 0.5), ((6, 9), 1.0), ((9, 12), 0.55), ((12, 14), 0.65),
                        ((14, 18), 0.55), ((18, 19), 0.9), ((19, 22), 1.35), ((22, 24), 0.7)],
    "stp_kw": 5.5, "misc_kw": 1.0, "lift_kw": 3.0, "lift_peak_kw": 6.0, "lighting_kw": 3.0,
    "noise": 0.10, "noise_seed": 9000,
}
PF = {"flats": 0.95, "common": 0.90, "event": 0.85, "pump": 0.85, "ev": 0.99}


def _forecast_kva(kind):
    out = []
    for i in range(96):
        f, c = connection.nonflex_kw(kind, i, _DS, CONN_SPEC)
        out.append(connection.interval_kva({"flats_kw": f, "common_kw": c, "event_kw": 0.0}, 0.0, 0.0, PF))
    return out


def _sizing():
    fk = _forecast_kva("whole_building")
    nf_peak_kva = connection.max_demand([s for _, _, s in fk], 30)
    pump_kva, ev_kva = _A["pump_model_kw"] / PF["pump"], _A["ev_charger_model_kw"] / PF["ev"]
    common_connected_kw = 2 * 7.5 + 4.0 + 7.5 + 2.0 + _A["pump_model_kw"] + _A["ev_charger_model_kw"]
    return {"forecast_nonflex_peak_kva_30min": round(nf_peak_kva, 2),
            "cd_tight_kva": math.ceil(1.05 * nf_peak_kva),
            "cd_generous_kva": math.ceil(1.25 * (nf_peak_kva + pump_kva + ev_kva)),
            "lt_sanctioned_kw": math.ceil(common_connected_kw * 4) / 4,
            "lt_connected_load_basis": "lifts 2 x 7.5 kW + lighting 4 kW + STP 7.5 kW + misc 2 kW + pump 5.5 kW + EV charger 7.4 kW"}


SIZING = _sizing()
CASES = {
    "HT4_A_tight_cd": {"category": "HT-4", "load": "whole_building", "contract_demand_kva": SIZING["cd_tight_kva"],
                       "events": [], "why": "CD sized at 1.05 x the forecast non-flexible 30-minute peak: coincident pump/EV "
                                            "load can push the recorded MD above the 90 % floor and above CD"},
    "HT4_B_generous_cd": {"category": "HT-4", "load": "whole_building", "contract_demand_kva": SIZING["cd_generous_kva"],
                          "events": [], "why": "CD sized at 1.25 x (forecast non-flexible peak + pump + EV kVA): the 90 % "
                                               "billing floor is expected to bind, so scheduling cannot change the demand charge"},
    "HT4_C_fire_pump_test": {"category": "HT-4", "load": "whole_building", "contract_demand_kva": SIZING["cd_tight_kva"],
                             "events": [{"day": 14, "from_interval": _clock(44), "to_interval": _clock(46), "kw": 75.0,
                                         "what": "monthly main fire-pump test, 75 kW for 30 minutes at 11:00 on day 15; "
                                                 "not in the planner's forecast"}],
                             "why": "another building peak: a declared non-flexible event that scheduling does not control"},
    "LT1_common_area": {"category": "LT-1", "load": "common_area", "sanctioned_load_kw": SIZING["lt_sanctioned_kw"],
                        "events": [], "why": "flats metered separately; the common-services connection under LT-1 clause (n) "
                                             "(category ASSUMED, never inferred from equipment); billing load = higher of "
                                             "sanctioned load and recorded MD, no excess penalty stated for LT-1"},
}


def _demand_aware_allowance(case):
    """Per-interval participating cap (kW) that keeps forecast connection demand at or below the target that the
    tariff makes relevant: HT max(90 % of CD, forecast non-flexible peak); LT max(sanctioned load, forecast peak)."""
    fk = _forecast_kva(case["load"])
    if case["category"] == "HT-4":
        target = max(0.9 * case["contract_demand_kva"], connection.max_demand([s for _, _, s in fk], 30))
        # shortcut: kVA headroom x 0.85 (the lowest flexible-load power factor) as a kW cap; conservative
        return [round(max(0.0, (target - s) * PF["pump"]), 3) for _, _, s in fk]
    target = max(case["sanctioned_load_kw"], max(p for p, _, _ in fk))
    return [round(max(0.0, target - p), 3) for p, _, _ in fk]


ADHERENCE = {
    "full": {"mode": "per_episode", "p_accept": 1.0, "p_on_time_given_accept": 1.0, "seed": 4242,
             "lapse_intervals": 4, "reminder_intervals": 4},
    "primary_70": {"mode": "per_episode", "p_accept": 0.8, "p_on_time_given_accept": 0.875, "seed": 4242,
                   "lapse_intervals": 4, "reminder_intervals": 4},
    "secondary_70_retries": {"mode": "per_prompt_with_retries", "p_respond_per_prompt": 0.7, "seed": 4242,
                             "lapse_intervals": 4, "reminder_intervals": 4},
}
FORCED_MISS = {"days": [3, 10, 17, 24], "count": 1}

REFERENCE = {
    "id": "flats-100-reference-v2",
    "supersedes": "flats-100-reference-v1 (kept; its plant let the minimum-on time override the high-level stop)",
    "kind": "FICTIONAL synthetic reference study; every input is an assumption; tariff-dependent; not a bill",
    "extends": "flats-100-v1 (kept as historical: its baseline 500 L start band was unsuitable at this scale)",
    "days": 30, "seed": "withdrawals: day d seed 7000 + d (as flats-100-v1); non-flexible load noise: 9000 + d; "
                        "behaviour: seed 4242 keyed by (day, device, direction, episode ordinal)",
    "building": FLATS_100["assumptions"],
    "float_switch": {"design_inputs": DESIGN, "derived": FD,
                     "same_for_every_policy": "float switch (start/stop), 1-minute level sensing, 3-minute actuation, "
                                              "minimum on 60 min / off 30 min, tank-full cutoff at 30,000 L inside a "
                                              "sub-step. Under NivaWatt the float switch stays wired as the low-level "
                                              "start and high-level stop; a manual request is ORed with it",
                     "automation_difference": "baseline: the float switch alone runs the pump and the EV charges on "
                                              "plug-in. NivaWatt: the manager switches pump and EV from a rules plan made "
                                              "every 15 minutes from measured tank level and EV energy; the EV charger "
                                              "does not start on plug-in unless switched on; the planner treats the "
                                              "float stop level as the top of the tank"},
    "physics": {"substep_min": 1, "stability_substep_min": 0.25,
                "stability_tolerances": {"participating_kwh_rel": 0.01, "pump_starts_abs_or_rel": [2, 0.05],
                                         "max_15min_participating_kw_rel": 0.02, "md_kva_rel": 0.01,
                                         "service_status": "identical"},
                "sensing_sensitivity": "baseline and NivaWatt 100 % re-run with level sensing every 15 s (reported, not "
                                       "part of the gate: it changes the controller, not the numerics)",
                "demand_within_interval": "uniform (the 1.5 burst factor is a design allowance, not simulated)",
                "reporting": "15-minute intervals"},
    "service_standard": {"unmet_water_l_max": 1.0, "reserve_minutes": 0, "critical_minutes": 0,
                         "ev_shortfall_kwh_per_session_max": 0.05, "overflow_l_max": 1.0},
    "terminal_storage": "raw month energy and charges are reported as simulated. An equal-terminal-storage comparison "
                        "adds, for each run, the pump energy to raise its final tank to the higher of the compared final "
                        "levels; this is an ANALYTICAL NORMALIZATION, not metered energy and not a billed amount, and "
                        "is never applied to demand charges",
    "objectives": {"A": "participating energy in the assumed advisory window 10:00-13:00",
                   "B": "highest 15-minute participating demand inside that window",
                   "C": "highest 15-minute (and 30-minute) participating demand in the month",
                   "D": "rebound: participating energy and highest 15-minute demand in 07:00-10:00 and 13:00-16:00",
                   "E": "whole-connection recorded MD and billing demand for each connection case"},
    "advisory_window": "10:00-13:00 IST, ASSUMED (not produced by the forecasting/advisory pipeline); fixed before any run",
    "advisory_intervals": FLATS_100["advisory_peak_intervals"],
    "rebound_intervals": {"07-10": _hours(7, 10), "13-16": _hours(13, 16)},
    "adherence": {
        "primary_70": "ONE response per action episode: accepted on first request 80 %; of those, executed on time 87.5 % "
                      "(70 % on-time overall) or one interval (15 min) late 12.5 %; not accepted 20 %: the switch stays "
                      "where it is for the whole episode (reminders and regenerated plans do not redraw)",
        "episode": "a requested switch position (device, direction) differing from the switch. Begins at the first such "
                   "request; ends when executed, when the opposite position is requested, or after the request is absent "
                   "for 4 consecutive intervals (1 h). Resumed within 1 h, re-requested by a new plan, or reminded: same "
                   "episode, same response. Proposal or plan IDs never reset it",
        "prompts": "first request, each newly adopted plan that still asks for it, and an hourly reminder while pending",
        "secondary_70_retries": "70 % response probability per prompt, with retries (NOT 70 % action adherence)",
        "bounded_missed_execution": "100 % adherence except the first EV-start episode on days 4, 11, 18 and 25 is "
                                    "reported done but not done; it is carried out only when a plan adopted after the "
                                    "measured deviation asks again (tests measured feedback)",
        "nonresponse": "pump: stays as is; the float switch still starts it at the start level and stops it at the stop "
                       "level. EV: charger stays as is; a vehicle stops drawing when its requested energy is delivered",
        "safety": "safety behaviour is identical whatever the manager does; float-switch starts and tank-full cutoffs are "
                  "reported separately from compliance",
    },
    "billing": {
        "tariff_period": {"period_start": "2026-06-01", "period_end": "2026-07-01"},
        "md_integration_minutes": 30, "md_sensitivity_minutes": 15,
        "power_factor_assumptions": PF, "connection_spec": CONN_SPEC, "sizing": SIZING,
        "cases": {k: {kk: vv for kk, vv in v.items()} for k, v in CASES.items()},
        "rules": "billing/tariffs.json: HT-4 billing demand = max(MD, 90 % CD), excess over CD at 2x, MD rounded to whole "
                 "kVA (0.5 up), PF surcharge below 0.90; LT-1 billing load = max(SL, MD) rounded to 0.25 kW; MD "
                 "integration period 30 min is an UNVERIFIED assumption",
        "report": ["energy-charge difference", "ordinary demand/fixed-charge difference", "excess-demand penalty difference",
                   "combined applicable difference (each component once)"],
        "not_included": "FPPCA (unknown), FY25 true-up (historical, unchanged by scheduling)",
    },
    "scheduler_gap": "the existing NivaWatt rules planner caps only the participating pump+EV load at a fixed allowance "
                     "(13 kW, never binding here) and places EV energy at the earliest non-advisory slots; it does not see "
                     "non-flexible load, so it does not limit whole-connection demand",
    "demand_aware_variant": "SEPARATELY LABELLED: the same planner with a per-interval participating cap = (tariff-relevant "
                            "target - forecast non-flexible kVA) x 0.85, target HT max(0.9 CD, forecast non-flexible "
                            "peak), LT max(SL, forecast peak). It sees the expected profile only (no noise, no events). "
                            "Where the cap never falls below 13 kW it is identical to NivaWatt and is not re-run",
    "gate": "adherence and billing runs only if the competent baseline and NivaWatt at 100 % both meet the service standard "
            "and the stability check passes; otherwise the study stops and says why",
}


def _base_params(fine=FINE):
    raw = plant.default_withdrawals(96)
    k = FLATS_100["daily_withdrawal_l"] / sum(raw)
    raw = [x * k for x in raw]
    raw = raw[_DS:] + raw[:_DS]
    return [round(x, 1) for x in raw], fine


def run_month(policy, fine=FINE, adherence=None, forced=None, daa=None, days=30, s=0, participation=None,
              provider=None):
    profile, fp = _base_params(fine)
    scen = {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "withdrawal_noise": DESIGN["withdrawal_noise"], "stochastic": True,
            "adherence": adherence, "forced_miss": forced, "demand_aware_allowance_kw": daa,
            "participation": participation, "demand_cap_provider": provider}
    tank, eq, logs, ms = float(FLATS_100["initial_tank_l"]), None, [], []
    for d in range(days):
        p = plant.make_params({**_A, "fine_physics": fp, "withdrawal_profile_l": profile, "tank_init_l": round(tank, 3),
                               "ev_sessions": [{"ev_id": "EV_1", **FLATS_100["ev_session"], "requested_model_kwh": 15}]})
        draws, w = experiment.draws_for(7000 + 100 * s + d, 96, scen, profile)  # s = 0: the reference seeds
        if provider is not None:
            provider.start_day(d)
        m = experiment.run_policy(policy, p, w, draws, {**scen, "day": d}, FLATS_100["advisory_peak_intervals"], initial=eq)
        tank, eq = m["final_tank_l"], m["final_equipment_state"]
        if provider is not None:
            provider.end_day()
        logs.append(m["truth_log"])
        ms.append(m)
    return logs, ms


def _sum_fine(logs, key):
    return round(sum(r["fine"][key] for log in logs for r in log), 3)


def summarize(logs, ms):
    flat = [r for log in logs for r in log]
    part = [r["pump_model_kw"] + r["ev_model_kw"] for r in flat]
    n = len(logs)
    idx = [(d, r["interval_index"]) for d, log in enumerate(logs) for r in log]
    adv = set(REFERENCE["advisory_intervals"])
    reb = {k: set(v) for k, v in REFERENCE["rebound_intervals"].items()}
    shortfalls = [v or 0.0 for m in ms for v in m["ev_shortfall_at_departure_model_kwh"].values()]
    withdrawn = sum(r["withdrawal_l"] for r in flat)
    unmet = sum(log[-1]["unmet_water_l"] for log in logs)
    eps = {}
    for m in ms:
        for e in m["episodes"]:
            eps[experiment.k_of(e)] = e  # a carried-over episode's last state wins
    eps = list(eps.values())
    executed = [e for e in eps if e["executed_interval"] is not None]
    night = [e for e in eps if (e["interval"] + _DS) % 96 >= 88 or (e["interval"] + _DS) % 96 < 24]
    out = {
        "service": {"water_withdrawn_l": round(withdrawn, 1), "water_delivered_l": round(withdrawn - unmet, 1),
                    "unmet_water_l": round(unmet, 1), "overflow_l": round(sum(log[-1]["overflow_l"] for log in logs), 1),
                    "reserve_minutes": _sum_fine(logs, "reserve_min"), "reserve_deficit_l_min": _sum_fine(logs, "reserve_deficit_lmin"),
                    "critical_minutes": _sum_fine(logs, "critical_min"), "critical_deficit_l_min": _sum_fine(logs, "critical_deficit_lmin"),
                    "min_tank_l": round(min(r["fine"]["min_tank_l"] for r in flat), 1),
                    "ev_sessions": len(shortfalls), "ev_delivered_kwh": round(15 * len(shortfalls) - sum(shortfalls), 3),
                    "ev_sessions_short": sum(s > 0.05 for s in shortfalls), "ev_shortfall_kwh": round(sum(shortfalls), 3),
                    "initial_tank_l": FLATS_100["initial_tank_l"], "final_tank_l": round(ms[-1]["final_tank_l"], 1)},
        "equipment": {"pump_starts": int(_sum_fine(logs, "starts")), "float_switch_starts": int(_sum_fine(logs, "float_starts")),
                      "float_only_run_min": _sum_fine(logs, "float_only_run_min"), "tank_full_cutoffs": int(_sum_fine(logs, "cutoffs")),
                      "runs_shortened_by_level_protection": int(_sum_fine(logs, "protection_short_runs")),
                      "start_blocked_by_min_off_min": _sum_fine(logs, "start_blocked_min"),
                      "min_on_off_violations": "0 by construction (enforced); runs shortened only by level protection "
                                               "(high-level stop or tank-full cutoff), counted above"},
        "energy": {"participating_kwh": round(sum(part) / 4, 3), "pump_kwh": round(sum(r["pump_model_kw"] for r in flat) / 4, 3),
                   "ev_kwh": round(sum(r["ev_model_kw"] for r in flat) / 4, 3)},
        "objectives": {
            "A_advisory_window_kwh": round(sum(x for x, (_, i) in zip(part, idx) if i in adv) / 4, 3),
            "B_advisory_window_max_15min_kw": round(max([x for x, (_, i) in zip(part, idx) if i in adv], default=0.0), 3),
            "C_month_max_15min_kw": round(max(part), 3), "C_month_max_30min_kw": round(connection.max_demand(part, 30), 3),
            "D_rebound": {k: {"kwh": round(sum(x for x, (_, i) in zip(part, idx) if i in v) / 4, 3),
                              "max_15min_kw": round(max([x for x, (_, i) in zip(part, idx) if i in v], default=0.0), 3)}
                          for k, v in reb.items()},
        },
        "manager": {"plan_adoptions": sum(m["manager_acceptances"] for m in ms), "action_episodes": len(eps),
                    "prompts": sum(e["prompts"] for e in eps),
                    "repeat_prompts": sum(max(0, e["prompts"] - 1) for e in eps),
                    "first_request_accepted": sum(e["outcome"] in ("on_time", "late", "delayed") for e in eps),
                    "execution_delay_histogram_min": {str(k * 15): v for k, v in sorted(
                        _count((e["executed_abs"] - e["t0"]) for e in executed).items())},
                    "first_request_on_time": sum(e["outcome"] == "on_time" and e.get("executed_prompt", 1) == 1 for e in eps),
                    "eventually_executed": len(executed),
                    "mean_execution_delay_min": round(sum((e["executed_abs"] - e["t0"]) * 15 for e in executed)
                                                      / len(executed), 1) if executed else None,
                    "night_episodes_22_06": len(night), "episodes_per_day": round(len(eps) / n, 2),
                    "missed_reported_done": sum(e["outcome"] == "missed_reported_done" for e in eps),
                    "deviations_detected": sum(m["deviations_detected"] for m in ms)},
    }
    mg = out["manager"]
    if eps:
        mg.update(first_request_acceptance_rate=round(mg["first_request_accepted"] / len(eps), 3),
                  on_time_rate_first_request=round(mg["first_request_on_time"] / len(eps), 3),
                  eventual_execution_rate=round(len(executed) / len(eps), 3))
    return out


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def meets_standard(s):
    st, sv = REFERENCE["service_standard"], s["service"]
    fails = [k for k, bad in (("unmet water", sv["unmet_water_l"] > st["unmet_water_l_max"]),
                              ("reserve", sv["reserve_minutes"] > st["reserve_minutes"]),
                              ("critical", sv["critical_minutes"] > st["critical_minutes"]),
                              ("EV deadline", sv["ev_sessions_short"] > 0),
                              ("overflow", sv["overflow_l"] > st["overflow_l_max"])) if bad]
    return not fails, fails


def stability(a, b):
    t = REFERENCE["physics"]["stability_tolerances"]
    rel = lambda x, y: abs(x - y) / max(abs(x), 1e-9)  # noqa: E731
    da, db = a["energy"]["participating_kwh"], b["energy"]["participating_kwh"]
    sa, sb = a["equipment"]["pump_starts"], b["equipment"]["pump_starts"]
    ka, kb = a["objectives"]["C_month_max_15min_kw"], b["objectives"]["C_month_max_15min_kw"]
    checks = {"participating_kwh": rel(da, db) <= t["participating_kwh_rel"],
              "pump_starts": abs(sa - sb) <= max(t["pump_starts_abs_or_rel"][0], t["pump_starts_abs_or_rel"][1] * sa),
              "max_15min_kw": rel(ka, kb) <= t["max_15min_participating_kw_rel"],
              "service_status": meets_standard(a)[0] == meets_standard(b)[0]}
    return {"values_1min": [da, sa, ka], "values_15s": [db, sb, kb], "checks": checks, "stable": all(checks.values())}


def costs(logs, case, tariff, md_min, s=0, d0=0):
    spec = {**CONN_SPEC, "noise_seed": CONN_SPEC["noise_seed"] + 100 * s}
    nf_days = [connection.nonflex_series(case["load"], d0 + d, _DS, spec, case["events"]) for d in range(len(logs))]
    conn = connection.connection_month(case, nf_days, logs, PF)
    return conn, connection.bill_components(case, conn, tariff, md_min)


def compare(name, base, var, base_logs, var_logs, case, s=0, d0=0):
    tariff = REFERENCE["billing"]["tariff_period"]
    out = {}
    ok_b, f_b = meets_standard(base)
    ok_v, f_v = meets_standard(var)
    comparable = ok_b and ok_v
    for md in (REFERENCE["billing"]["md_integration_minutes"], REFERENCE["billing"]["md_sensitivity_minutes"]):
        cb, bb = costs(base_logs, case, tariff, md, s, d0)
        cv, bv = costs(var_logs, case, tariff, md, s, d0)
        rate = bb["energy_related_rs"] / cb["energy_kwh"]  # energy-related Rs per kWh (energy + P&G + tax assumption)
        kwh_per_l = _A["pump_model_kw"] / _A["pump_inflow_lph"]
        # storage CHANGE over the period (equal starting tanks: the same as comparing final tanks)
        init0 = FLATS_100["initial_tank_l"]
        dv = var["service"]["final_tank_l"] - var["service"].get("initial_tank_l", init0)
        db_ = base["service"]["final_tank_l"] - base["service"].get("initial_tank_l", init0)
        norm = round((db_ - dv) * kwh_per_l * rate, 2)
        d = {k: round(bv[k] - bb[k], 2) for k in ("energy_related_rs", "ordinary_demand_rs", "excess_demand_rs", "pf_surcharge_rs")}
        raw_total = round(sum(d.values()), 2)
        out[f"md_{md}min"] = {
            "baseline": {"connection": cb, "charges": bb}, "variant": {"connection": cv, "charges": bv},
            "differences_raw_rs": {**d, "combined_applicable_rs": raw_total,
                                   "label": "simulated month as billed (partial: FPPCA unknown); NOT an equal-service "
                                            "saving unless comparable"},
            "equal_terminal_storage": {
                "energy_related_rs": round(d["energy_related_rs"] + norm, 2) if comparable else None,
                "combined_applicable_rs": round(raw_total + norm, 2) if comparable else None,
                "normalization_rs": norm,
                "label": "ANALYTICAL NORMALIZATION of the energy term to equal final storage; not metered, not billed"},
        }
    return {"case": name, "comparable": comparable,
            "why_not": None if comparable else {"baseline": f_b, "variant": f_v},
            "equivalent_service_saving": "available" if comparable else "unavailable (service not comparable)", **out}


def diagnose_old_controller():
    """flats-100-v1 baseline thresholds (start 8,500, stop 29,500, no delay) on fine physics, sensed every 15 min vs
    every minute: separates coarse sampling from controller design."""
    out = {}
    for sense in (15, 1):
        fp = {**FINE, "start_l": 8500, "stop_l": 29500, "actuation_delay_min": 0, "sensing_min": sense}
        logs, ms = run_month("uncoordinated", fine=fp)
        s = summarize(logs, ms)
        out[f"sensing_{sense}min"] = {"reserve_minutes": s["service"]["reserve_minutes"],
                                      "min_tank_l": s["service"]["min_tank_l"], "pump_starts": s["equipment"]["pump_starts"]}
    return out


def run():
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "billing", "simulation", "scheduler"], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip())
    res = {"protocol": REFERENCE, "software_commit": sha + ("-dirty" if dirty else "")}
    res["old_controller_diagnosis"] = diagnose_old_controller()
    runs = {"baseline": run_month("uncoordinated"), "nivawatt_100": run_month("measured_replan", adherence=ADHERENCE["full"])}
    summ = {k: summarize(*v) for k, v in runs.items()}
    fine_q = {**FINE, "substep_min": 0.25}
    stab = {k: stability(summ[k], summarize(*run_month(pol, fine=fine_q, adherence=adh)))
            for k, pol, adh in (("baseline", "uncoordinated", None), ("nivawatt_100", "measured_replan", ADHERENCE["full"]))}
    res["stability"] = stab
    fine_s = {**FINE, "substep_min": 0.25, "sensing_min": 0.25}
    res["sensing_15s_sensitivity"] = {
        k: stability(summ[k], summarize(*run_month(pol, fine=fine_s, adherence=adh)))
        for k, pol, adh in (("baseline", "uncoordinated", None), ("nivawatt_100", "measured_replan", ADHERENCE["full"]))}
    gate = {k: meets_standard(summ[k]) for k in ("baseline", "nivawatt_100")}
    passed = all(ok for ok, _ in gate.values()) and all(s["stable"] for s in stab.values())
    res["gate"] = {"passed": passed, "service": {k: {"meets": ok, "fails": f} for k, (ok, f) in gate.items()},
                   "stable": {k: s["stable"] for k, s in stab.items()}}
    if passed:
        runs["nivawatt_70"] = run_month("measured_replan", adherence=ADHERENCE["primary_70"])
        runs["nivawatt_70_retries_secondary"] = run_month("measured_replan", adherence=ADHERENCE["secondary_70_retries"])
        runs["nivawatt_missed_bounded"] = run_month("measured_replan", adherence=ADHERENCE["full"], forced=FORCED_MISS)
        da = {}
        for cname, case in CASES.items():
            cap = _demand_aware_allowance(case)
            key = "demand_aware_" + ("ht_tight" if case["category"] == "HT-4" and case["contract_demand_kva"] == SIZING["cd_tight_kva"]
                                     else cname)
            da[cname] = {"variant_prefix": key, "min_cap_kw": min(cap), "binds": min(cap) < _A["allowance_model_kw"]}
            if da[cname]["binds"] and key + "_100" not in runs:
                runs[key + "_100"] = run_month("measured_replan", adherence=ADHERENCE["full"], daa=cap)
                runs[key + "_70"] = run_month("measured_replan", adherence=ADHERENCE["primary_70"], daa=cap)
        res["demand_aware"] = da
        summ.update({k: summarize(*v) for k, v in runs.items() if k not in summ})
    else:
        res["adherence_study"] = "paused: reference gate failed"
    res["runs"] = summ
    if passed:
        cmp = {}
        for cname, case in CASES.items():
            variants = ["nivawatt_100", "nivawatt_70", "nivawatt_70_retries_secondary", "nivawatt_missed_bounded"]
            if res["demand_aware"][cname]["binds"]:
                pre = res["demand_aware"][cname]["variant_prefix"]
                variants += [pre + "_100", pre + "_70"]
            cmp[cname] = {v: compare(cname, summ["baseline"], summ[v], runs["baseline"][0], runs[v][0], case) for v in variants}
        res["billing"] = cmp
    return res


BEHAVIOUR = {
    "id": "flats-100-behaviour-v1",
    "kind": "SEPARATE behavioural SENSITIVITY scenario on the flats-100-reference-v2 plant, baseline, cases and billing; "
            "declared before it was run. The percentages are sensitivity ASSUMPTIONS, not a description of actual "
            "Bengaluru residents or managers",
    "relation_to_v2": "flats-100-reference-v2 is kept unchanged; its episode-level 70 % run stays as the STRESS case (an "
                      "ignored action may stay unexecuted until its deadline)",
    "adherence": {"mode": "per_episode_distribution", "seed": 4242, "lapse_intervals": 4, "reminder_intervals": 4,
                  "distribution": [[0.70, 0], [0.15, 1], [0.10, 4], [0.05, None]]},
    "adherence_text": "per unique action episode, drawn ONCE (same episode definition and matched behaviour keys as v2): "
                      "70 % executed on time; 15 % executed 15 minutes late; 10 % executed 60 minutes late; 5 % not "
                      "executed in that episode. Reminders and replans never redraw or improve it. A delayed action is "
                      "carried out only if its episode is still open when the delay has passed",
    "variants": ["nivawatt", "demand_aware_ht_tight"],
    "report": "for each variant and connection case: realized on-time and eventual execution, repeated prompts, execution "
              "delays, EV sessions below target and shortfall, water service and reserve, peak relief and rebound, "
              "manager burden, raw energy and cost, and service comparability; an equivalent-service saving only where "
              "service is comparable",
}


def run_behaviour():
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    adh = BEHAVIOUR["adherence"]
    runs = {"baseline": run_month("uncoordinated"),
            "nivawatt_behaviour": run_month("measured_replan", adherence=adh),
            "demand_aware_ht_tight_behaviour": run_month("measured_replan", adherence=adh,
                                                         daa=_demand_aware_allowance(CASES["HT4_A_tight_cd"]))}
    summ = {k: summarize(*v) for k, v in runs.items()}
    cmp = {}
    for cname, case in CASES.items():
        vs = ["nivawatt_behaviour"] + (["demand_aware_ht_tight_behaviour"] if case["category"] == "HT-4"
                                       and case["contract_demand_kva"] == SIZING["cd_tight_kva"] else [])
        cmp[cname] = {v: compare(cname, summ["baseline"], summ[v], runs["baseline"][0], runs[v][0], case) for v in vs}
    return {"protocol": BEHAVIOUR, "reference_protocol": REFERENCE["id"], "software_commit": sha, "runs": summ,
            "service": {k: {"meets": meets_standard(v)[0], "fails": meets_standard(v)[1]} for k, v in summ.items()},
            "billing": cmp}


PARTICIPATION = {
    "id": "flats-100-participation-v1",
    "kind": "SEPARATE simplified schedule-participation scenario on the flats-100-reference-v2 plant and billing rules; "
            "declared before it was run. Not an action-adherence model; the 70 % is a sensitivity ASSUMPTION",
    "relation": "flats-100-reference-v2 and flats-100-behaviour-v1 are unchanged; their 70 % runs remain action-adherence "
                "stress/sensitivity cases",
    "participation": {"p": 0.7, "seed": 5000,
                      "episode": "predefined: each (simulated day, device). The pump episode is the day's pump schedule "
                                 "(12:00 to 12:00); the EV episode is that night's charging schedule (19:00-07:00)",
                      "draw": "once per episode, keyed by (seed + 100 x matched seed, day, device); never redrawn",
                      "participating": "the manager executes NivaWatt's instructions for that device on time",
                      "fallback": "pump: float switch alone (no manual command, so never an indefinite ON); EV: charges "
                                  "on plug-in until its 15 kWh is delivered. Safety protections unchanged"},
    "policies": {"baseline": "competent float switch + EV on plug-in",
                 "demand_aware_100": "demand-aware planner, every episode participates",
                 "demand_aware_70": "demand-aware planner, 70 % schedule participation"},
    "connection_case": "HT4_A_tight_cd (the only case where the demand-aware cap binds); other cases are reported in "
                       "flats-100-reference-v2 and are not re-run",
    "matched_seeds": [0, 1, 2, 3, 4],
    "seeds": "seed s: withdrawals 7000 + 100 s + d, non-flexible noise 9000 + 100 s + d, participation 5000 + 100 s; "
             "s = 0 reproduces the reference seeds",
    "md_integration": "30 minutes, now verified: Conditions of Supply cl. 2.38 (see billing/tariffs.json demand_rules)",
    "forecast": "the demand-aware cap uses the synthetic generator's EXPECTED non-flexible profile (the same mean the "
                "truth is drawn around), fixed before the month; no realized or future values, no noise, no events. "
                "This is a PERFECT-MEAN-PROFILE BENCHMARK, not deployable performance: a deployable forecast would be "
                "estimated from history and carry error",
    "report": "per seed and across seeds: whole-building billing demand, advisory relief, rebound, service, final storage, "
              "pump starts, manager actions, rejected schedules and itemized cost differences",
    "utility_benefit": "NOT estimated here: this table covers the apartment's bill only. Advisory-window relief may be a "
                       "utility procurement benefit; that study has not been done",
}


def run_participation():
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    case = CASES["HT4_A_tight_cd"]
    cap = _demand_aware_allowance(case)
    out = {"protocol": PARTICIPATION, "reference_protocol": REFERENCE["id"], "software_commit": sha, "seeds": {}}
    for s in PARTICIPATION["matched_seeds"]:
        part = {k: v for k, v in PARTICIPATION["participation"].items() if k in ("p", "seed")}
        part = {**part, "seed": part["seed"] + 100 * s}
        runs = {"baseline": run_month("uncoordinated", s=s),
                "demand_aware_100": run_month("measured_replan", daa=cap, s=s),
                "demand_aware_70": run_month("measured_replan", daa=cap, s=s, participation=part)}
        summ = {k: summarize(*v) for k, v in runs.items()}
        for k, (_, ms) in runs.items():
            if ms[0]["participation"] is not None:
                summ[k]["participation"] = {
                    "pump_days_not_participating": [d for d, m in enumerate(ms) if not m["participation"]["pump"]],
                    "ev_nights_not_participating": [d for d, m in enumerate(ms) if not m["participation"]["ev"]],
                    "episodes": 2 * len(ms),
                    "participated": sum(m["participation"]["pump"] + m["participation"]["ev"] for m in ms),
                    "manager_switch_operations": sum(m["participation"]["manager_switch_operations"] for m in ms)}
                summ[k]["participation"]["realized_rate"] = round(summ[k]["participation"]["participated"]
                                                                  / summ[k]["participation"]["episodes"], 3)
            else:
                summ[k]["manager"]["switch_operations"] = sum(m["manual_switch_operations"] for m in ms)
        out["seeds"][s] = {"runs": summ, "service": {k: meets_standard(v)[0] for k, v in summ.items()},
                           "billing": {v: compare("HT4_A_tight_cd", summ["baseline"], summ[v], runs["baseline"][0],
                                                  runs[v][0], case, s) for v in ("demand_aware_100", "demand_aware_70")}}
    agg = {}
    for v in ("demand_aware_100", "demand_aware_70"):
        rows = [out["seeds"][s]["billing"][v] for s in PARTICIPATION["matched_seeds"]]
        vals = [r["md_30min"]["equal_terminal_storage"]["combined_applicable_rs"] for r in rows]
        agg[v] = {"comparable_seeds": sum(r["comparable"] for r in rows), "of": len(rows),
                  "combined_equal_service_rs": {"min": min(x for x in vals if x is not None),
                                                "max": max(x for x in vals if x is not None)}
                  if any(x is not None for x in vals) else None,
                  "per_seed": vals}
    out["across_seeds"] = agg
    return out


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "reference"
    behaviour = mode == "behaviour"
    if mode == "participation":
        out = run_participation()
        d = ROOT / "results" / "billing_scenario" / PARTICIPATION["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        print("across seeds", out["across_seeds"])
        sys.exit(0)
    out = run_behaviour() if behaviour else run()
    d = ROOT / "results" / "billing_scenario" / (BEHAVIOUR if behaviour else REFERENCE)["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print("service" if behaviour else "gate", out["service"] if behaviour else out["gate"])
