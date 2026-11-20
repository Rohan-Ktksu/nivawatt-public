"""Paired policy comparison on the synthetic apartment (simulation only; not a bench result).

Policies
- uncoordinated (automatic_local): float-switch pump, EV charges on arrival until full. Context only.
- demand_cap (automatic_local): same, but EV pauses whenever pump+EV would exceed the allowance (water priority).
- fixed_plan (human_advisory): one rules plan at t=0, never revised.
- ack_replan (human_advisory): rules scheduler; tank from observations; EV progress credited from done claims.
- measured_replan (human_advisory): same scheduler and same tank observations; EV progress from measured EV energy.
- aggregate_replan (human_advisory): same scheduler and tank observations, but only one meter on the combined
  participating circuits. EV progress = claimed energy, withdrawn only where the aggregate reading is zero
  (claim contradicted). It never attributes energy to an individual EV from the aggregate (observability variant).

Shared across policies within a seed: plant parameters, withdrawal disturbance, advisory peak intervals, and the
human-response draws (a miss for interval i / action kind applies to whichever policy issues that action).
By default the human claims done for every instructed EV action, including missed ones (false done), and sensors
are ideal. Imperfection knobs (default off, so earlier scenarios are unchanged): p_false_report (chance a skipped
action is still reported done), power_noise_sd, tank_noise_sd_l, p_ev_meter_dropout (EV meter reads zero while
charging), p_reading_late (an interval's reading arrives one interval late). Noise draws come from a separate
per-seed stream and are shared across policies. Policies never see truth.
Automatic policies are a different authority class: they are not subject to human misses.

Run: python -m simulation.experiment   -> results/experiments/<id>/{runs.csv,summary.json}
"""
import copy
import csv
import json
import os
import random
import statistics
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scheduler import revision, rules
from simulation import plant
from simulation.metrics import run_metrics

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ["uncoordinated", "demand_cap", "fixed_plan", "ack_replan", "measured_replan", "aggregate_replan"]
# Electrical meters each policy relies on (tank sensor common to the advisory ones). Prices unknown until the BOM exists.
ELECTRICAL_METERS = {"uncoordinated": 0, "demand_cap": 2, "fixed_plan": 0, "ack_replan": 0, "measured_replan": 2,
                     "aggregate_replan": 1}
SCENARIOS = {
    "perfect": {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "stochastic": False},
    "missed_ev_false_done": {"p_miss_pump": 0, "p_miss_ev": 0.3, "p_drop": 0, "stochastic": True},
    "missed_pump_false_done": {"p_miss_pump": 0.3, "p_miss_ev": 0, "p_drop": 0, "stochastic": True},
    "stale_telemetry": {"p_miss_pump": 0, "p_miss_ev": 0.15, "p_drop": 0.2, "stochastic": True},
    "extra_withdrawal": {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "withdrawal_noise": 0.3, "stochastic": True},
    # Default day has no load in the 10:00-13:00 IST advisory hours; this case exercises shifting and the cap.
    "daytime_ev_tight_allowance": {"p_miss_pump": 0, "p_miss_ev": 0.15, "p_drop": 0, "stochastic": True,
                                   "params": {"allowance_model_kw": 4.0,
                                              "ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 36,
                                                               "departure_interval": 72, "requested_model_kwh": 10}]}},
    # Pump and EV overlap (low tank, daytime EV) so non-zero aggregate readings cannot reveal a missed EV action.
    "missed_ev_overlapping_pump": {"p_miss_pump": 0, "p_miss_ev": 0.3, "p_drop": 0, "stochastic": True,
                                   "params": {"tank_init_l": 3200, "allowance_model_kw": 6.0}},
    # Imperfect reality: honest people, noisy and late meters. Measured replanning can lose or false-alarm here.
    "honest_misses": {"p_miss_pump": 0, "p_miss_ev": 0.3, "p_drop": 0, "p_false_report": 0.0, "stochastic": True},
    "noisy_sensors_mixed_reports": {"p_miss_pump": 0.1, "p_miss_ev": 0.2, "p_drop": 0.05, "p_false_report": 0.5,
                                    "power_noise_sd": 0.05, "tank_noise_sd_l": 30, "p_ev_meter_dropout": 0.05,
                                    "stochastic": True},
    "noisy_late_sensors_false_done": {"p_miss_pump": 0, "p_miss_ev": 0.2, "p_drop": 0, "p_false_report": 1.0,
                                      "power_noise_sd": 0.05, "tank_noise_sd_l": 30, "p_ev_meter_dropout": 0.1,
                                      "p_reading_late": 0.3, "stochastic": True},
    # Judge-style disturbances. Delayed: the person acts one interval late 30 % of the time but still reports done.
    "delayed_execution": {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "p_delay": 0.3, "stochastic": True},
    # Unannounced source outage over the first planned pump run (intervals 19-24); the planner is not told.
    "outage_during_run_protected": {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "withdrawal_noise": 0.1,
                                    "stochastic": True, "params": {"source_outage_intervals": list(range(19, 25)),
                                                                   "pump_dry_run_protection": True}},
    "outage_during_run_unprotected": {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "withdrawal_noise": 0.1,
                                      "stochastic": True, "params": {"source_outage_intervals": list(range(19, 25)),
                                                                     "pump_dry_run_protection": False}},
    "infeasible_ev": {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "stochastic": False,
                      "params": {"ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32,
                                                  "requested_model_kwh": 40}]}},
}
TANK_TOL_L, EV_TOL_KWH = 50.0, 0.05


def constraints(p):
    c = {k: p[k] for k in ("horizon_intervals", "model_dt_minutes", "tank_max_l", "tank_min_l", "pump_model_kw",
                           "ev_charger_model_kw", "pump_inflow_lph", "allowance_model_kw", "pump_min_on_intervals",
                           "pump_min_off_intervals", "reserve_margin_l", "tank_critical_l",
                           "verified_electrical_limit_model_kw")}
    c["expected_withdrawal_l"] = p["withdrawal_profile_l"]  # planner's estimate (truth may differ)
    if p.get("fine_physics"):  # the float switch's stop level is the usable top of the tank for every policy
        c["tank_max_l"] = p["fine_physics"]["stop_l"]
    return c


def _u(adh, *key):
    """Behaviour draw keyed by (seed, day, device, direction, episode ordinal, ...): identical for every policy, so the
    k-th pump-start episode of a day gets the same response whichever policy produced it."""
    return random.Random("|".join(str(x) for x in (adh["seed"],) + key)).random()


def k_of(e):
    return (e["day"], e["device"], int(e["want"]), e["ordinal"])


def _episode_delay(adh, day, device, want, ordinal):
    """Distribution model: ONE draw per episode gives its delay in intervals (0 = on time) or None (never executed)."""
    u, acc = _u(adh, day, device, int(want), ordinal), 0.0
    for p, delay in adh["distribution"]:
        acc += p
        if u < acc:
            return delay
    return None


def _episode_outcome(adh, day, device, want, ordinal):
    """Primary model: ONE response per action episode (never redrawn for reminders or regenerated proposals)."""
    u = _u(adh, day, device, int(want), ordinal)
    if u < adh["p_accept"] * adh["p_on_time_given_accept"]:
        return "on_time"
    return "late" if u < adh["p_accept"] else "not_accepted"


def run_policy(policy, base_p, truth_withdrawal, draws, scen, peaks, initial=None):
    p_plan = base_p
    p_true = {**copy.deepcopy(base_p), "withdrawal_profile_l": truth_withdrawal}
    n, dt_h = base_p["horizon_intervals"], base_p["model_dt_minutes"] / 60
    t = plant.initial_truth(p_true)
    # Optional equipment state carried in from a previous simulated day (multi-day runs): pump on/off and its
    # run / off-interval history, so minimum on/off rules and start counts continue across midnight.
    start_pump_on = bool((initial or {}).get("pump_on", False))
    if initial:
        t.update({k: copy.deepcopy(initial[k]) for k in ("pump_on", "pump_run_intervals", "pump_off_intervals", "fine")
                  if k in initial})
    fine = bool(base_p.get("fine_physics"))
    adh = scen.get("adherence")  # per-episode adherence model (see billing/scenario.py protocol); None: legacy knobs
    manual = dict((initial or {}).get("manual", {"pump": False, "ev": False}))  # switch positions the manager set
    # open episode per device, carried across days so an ignored request is not redrawn after midnight/noon
    ep_now = copy.deepcopy((initial or {}).get("episodes_open", {"pump": None, "ev": None}))
    episodes, ordinals = [e for e in ep_now.values() if e is not None], {}  # carried episodes report here too
    forced = dict(scen.get("forced_miss") or {})
    forced_left = forced.get("count", 0) if scen.get("day") in forced.get("days", []) else 0
    cons = constraints(p_plan)
    # Schedule participation (separate model): ONE draw per predefined episode = (day, device). A device that does not
    # participate that day runs in competent normal operation: pump on the float switch alone (no manual command, so
    # never an indefinite ON), EV charging on plug-in.
    part = scen.get("participation")
    takes_part = ({dev: _u(part, scen.get("day"), dev) < part["p"] for dev in ("pump", "ev")} if part is not None
                  else None)
    part_ops = 0
    if scen.get("demand_aware_allowance_kw") is not None:
        cons["demand_aware_allowance_kw"] = scen["demand_aware_allowance_kw"]
    log, plans, deviations_seen = [], 0, 0
    obs_tank, credited = base_p["tank_init_l"], {s["ev_id"]: 0.0 for s in base_p["ev_sessions"]}
    plan_opt, missed_ev_actions = None, 0
    pfr = scen.get("p_false_report", 1.0)
    late_credit = {}  # ev_id -> energy whose reading arrives next interval
    replans_without_miss, prev_missed = 0, False
    prev_want, last_sw, switch_ops = (False, False), (False, False), 0
    # evidence gate and revision gate state
    last_fresh, last_accept, gate = True, None, {}
    unverified = {s["ev_id"]: 0.0 for s in base_p["ev_sessions"]}  # planned EV energy in intervals without readings
    unverified_intervals = 0
    for i in range(n):
        if policy == "uncoordinated" and fine:
            pump, ev = False, True  # the hardware float switch inside the plant runs the pump; EV charges on arrival
        elif policy in ("uncoordinated", "demand_cap"):
            pump = (t["tank_l"] < base_p["tank_min_l"] + 500
                    or (t["pump_on"] and t["tank_l"] < base_p["tank_max_l"] - 500)
                    or (t["pump_on"] and t["pump_run_intervals"] < base_p["pump_min_on_intervals"]))  # controller min run
            ev = True
            if policy == "demand_cap" and pump and base_p["pump_model_kw"] + base_p["ev_charger_model_kw"] > base_p["allowance_model_kw"]:
                ev = False
        else:
            need_plan = plan_opt is None
            provider = scen.get("demand_cap_provider")  # causal demand-aware candidate: cap from observations so far
            if provider is not None:
                cap = provider.cap(i)
                if cap is None:
                    cons.pop("demand_aware_allowance_kw", None)  # documented fallback: advisory-only planning
                else:
                    cons["demand_aware_allowance_kw"] = cap
            # evidence gate: only a fresh reading can reveal a deviation; a missing or late reading never does
            if policy != "fixed_plan" and plan_opt is not None and i > plan_opt["effective_from_interval"] and last_fresh:
                k = i - 1 - plan_opt["effective_from_interval"]
                exp_tank = plan_opt["water_trajectory_model_l"][k]
                exp_ev = {e: plan_opt["ev_energy_trajectory_model_kwh"][e][k] + plan_base[e]
                          for e in plan_opt["ev_energy_trajectory_model_kwh"]}
                # planned energy in unverified intervals is neither credited nor counted as missing
                if abs(obs_tank - exp_tank) > TANK_TOL_L or \
                        any(abs(credited[e] + unverified[e] - exp_ev[e]) > EV_TOL_KWH for e in exp_ev):
                    need_plan = True
                    deviations_seen += 1
                    replans_without_miss += not prev_missed  # nothing was skipped last interval: a false alarm or disturbance
            if need_plan:
                # pump switch state and its run/off length: same switch-state approximation all advisory policies used before
                st = {"interval_index": i, "tank_l": obs_tank, "pump_on": t["pump_on"],
                      "pump_run_intervals": t["pump_run_intervals"] if t["pump_on"] else None,
                      "pump_off_intervals": t["pump_off_intervals"] if not t["pump_on"] else None}
                # energy planned in unread intervals is pending verification: neither credited nor treated as missing
                reqs = {"ev_sessions": [{**s, "remaining_model_kwh": max(0.0, s["requested_model_kwh"] - credited[s["ev_id"]]
                                                                         - unverified[s["ev_id"]])}
                                        for s in base_p["ev_sessions"]]}
                known = {"peak_intervals": peaks}
                cand = rules.plan(st, known, reqs, cons)["options"][0]
                if plan_opt is None:
                    decision = {"decision": "PROPOSE"}
                else:
                    decision = revision.assess(plan_opt["actions"], cand["actions"], st, cons, reqs, known,
                                               last_accept)
                if decision["decision"] == "PROPOSE":  # the manager accepts it (one acceptance per adopted plan)
                    plan_opt, plans, last_accept = cand, plans + 1, i
                    plan_base = {e: credited[e] + unverified[e] for e in credited}
                else:
                    gate[decision["decision"]] = gate.get(decision["decision"], 0) + 1
            a = plan_opt["actions"][i - plan_opt["effective_from_interval"]] if i >= plan_opt["effective_from_interval"] else None
            want_pump = bool(a and a["pump"] == "ON")
            want_ev = bool(a and a["ev"] != "OFF")
            # delayed execution: the person still carries out the previous interval's instruction
            do_pump, do_ev = prev_want if draws["delay"][i] < scen.get("p_delay", 0) else (want_pump, want_ev)
            prev_want = (want_pump, want_ev)
            pump = do_pump and draws["pump"][i] >= scen["p_miss_pump"]
            ev = do_ev and draws["ev"][i] >= scen["p_miss_ev"]
            if adh is not None:
                # Action episode = a requested switch position (device, direction) that differs from the switch.
                # It begins at the first such request; it ends when executed, when the opposite position is
                # requested, or when the request lapses for adh["lapse_intervals"] consecutive intervals. A request
                # resumed within the lapse window, a reminder or a regenerated proposal (new plan, new proposal ID)
                # continues the same episode and keeps its response.
                for dev, w in (("pump", want_pump), ("ev", want_ev)):
                    e = ep_now[dev]
                    if e is not None and e["want"] != manual[dev] and w == manual[dev]:  # request not present now
                        e["lapsed"] += 1
                        if e["lapsed"] >= adh["lapse_intervals"]:
                            ep_now[dev] = e = None
                        continue
                    if w == manual[dev]:
                        ep_now[dev] = None
                        continue
                    if e is None or e["want"] != w:
                        k = (scen.get("day"), dev, int(w))
                        ordinals[k] = ordinals.get(k, 0) + 1
                        delay = None
                        if adh["mode"] == "per_episode":
                            out = _episode_outcome(adh, *k, ordinals[k])
                        elif adh["mode"] == "per_episode_distribution":
                            delay = _episode_delay(adh, *k, ordinals[k])
                            out = "not_accepted" if delay is None else "on_time" if delay == 0 else "delayed"
                        else:
                            out = "pending"
                        if dev == "ev" and w and forced_left > 0:
                            out, forced_left = "missed_reported_done", forced_left - 1
                        e = ep_now[dev] = {"device": dev, "day": scen.get("day"), "interval": i, "want": w,
                                           "ordinal": ordinals[k], "outcome": out, "lapsed": 0, "prompts": 0,
                                           "last_prompt": None, "plan_at_prompt": None, "executed_interval": None,
                                           "first_plan": plans, "delay_intervals": delay,
                                           "t0": (scen.get("day") or 0) * n + i}  # absolute start: delays may cross days
                        episodes.append(e)
                    e["lapsed"] = 0
                    # a prompt: the first request, a newly adopted plan that still asks for it, or an hourly reminder
                    if (e["last_prompt"] is None or plans != e["plan_at_prompt"]
                            or i - e["last_prompt"] >= adh["reminder_intervals"]):
                        e["prompts"] += 1
                        e["last_prompt"], e["plan_at_prompt"] = i, plans
                        if adh["mode"] == "per_prompt_with_retries" and e["outcome"] in ("pending", "not_accepted"):
                            # SECONDARY model: a fresh response chance at every prompt (not action adherence)
                            e["outcome"] = "on_time" if _u(adh, *k_of(e), e["prompts"]) < adh["p_respond_per_prompt"]                                 else "not_accepted"
                            e["executed_prompt"] = e["prompts"] if e["outcome"] == "on_time" else None
                    now_abs = (scen.get("day") or 0) * n + i
                    if (e["outcome"] == "on_time" or (e["outcome"] == "late" and now_abs > e["t0"])
                            or (e["outcome"] == "delayed" and now_abs - e["t0"] >= e["delay_intervals"])
                            # bounded miss: reported done but not done; carried out once a plan adopted after the
                            # measured deviation asks again
                            or (e["outcome"] == "missed_reported_done" and plans != e["first_plan"])):
                        manual[dev] = w
                        e["executed_interval"], e["executed_abs"] = i, now_abs
                pump, ev = manual["pump"], manual["ev"]
            if takes_part is not None:
                if not takes_part["pump"]:
                    pump = False  # fallback: float switch alone
                if not takes_part["ev"]:
                    ev = True  # fallback: charge on plug-in until the requested energy is delivered
                part_ops += (takes_part["pump"] and pump != last_sw[0]) + (takes_part["ev"] and ev != last_sw[1])
            switch_ops += (pump != last_sw[0]) + (ev != last_sw[1])
            last_sw = (pump, ev)
            missed_ev_actions += want_ev and not ev
            prev_missed = (want_pump and not pump) or (want_ev and not ev)
            claimed_ev = want_ev and (ev or draws["claim"][i] < pfr)  # honest people admit some misses
        t, rec = plant.step(t, p_true, i, pump, ev)
        if scen.get("demand_cap_provider") is not None:
            scen["demand_cap_provider"].observe(i, rec)
        rec["allowance_model_kw"] = base_p["allowance_model_kw"]
        log.append(rec)
        # observations available to policies (identical tank observations for ack and measured)
        dropped = draws["drop"][i] < scen["p_drop"]
        if not dropped:
            obs_tank = max(0.0, rec["tank_l"] + draws["tank_noise"][i] * scen.get("tank_noise_sd_l", 0))
        late_now = False
        if policy == "measured_replan":
            for e, kwh in late_credit.items():  # readings delayed from the previous interval arrive now
                credited[e] += kwh
            late_credit = {}
            meter_zero = draws["ev_dropout"][i] < scen.get("p_ev_meter_dropout", 0)
            # The EV meter channel measures the charger group, not a vehicle. Its energy is credited to an EV only when
            # exactly one EV session is declared present (known requirements), never from the simulator's own record
            # of which vehicle charged. With more than one present the energy stays uncredited.
            present = [s["ev_id"] for s in base_p["ev_sessions"] if s["arrival_interval"] <= i < s["departure_interval"]]
            ev_meter = present[0] if len(present) == 1 and rec["ev_model_kw"] > 1e-6 else None
            if not dropped and ev_meter and not meter_zero:  # no credit without a measurement
                kwh = max(0.0, rec["ev_model_kw"] * (1 + draws["power_noise"][i] * scen.get("power_noise_sd", 0)) * dt_h)
                if draws["late"][i] < scen.get("p_reading_late", 0):
                    late_credit[ev_meter] = late_credit.get(ev_meter, 0) + kwh
                    late_now = True
                else:
                    credited[ev_meter] += kwh
            if (dropped or meter_zero) and a and a["ev"] != "OFF" and plan_opt is not None:
                k = i - plan_opt["effective_from_interval"]
                tr = plan_opt["ev_energy_trajectory_model_kwh"].get(a["ev"])
                if tr is not None and 0 <= k < len(tr):
                    unverified[a["ev"]] += tr[k] - (tr[k - 1] if k > 0 else 0.0)
        elif policy == "aggregate_replan" and plan_opt is not None:
            agg_seen = rec["aggregate_model_kw"] * (1 + draws["power_noise"][i] * scen.get("power_noise_sd", 0))
            if claimed_ev and (dropped or agg_seen > 0.05):  # a near-zero reading contradicts the claim
                e = a["ev"]
                sess = next(s for s in base_p["ev_sessions"] if s["ev_id"] == e)
                credited[e] = min(sess["requested_model_kwh"], credited[e] + base_p["ev_charger_model_kw"] * dt_h)
        elif policy in ("ack_replan", "fixed_plan") and plan_opt is not None:
            if claimed_ev:  # credit what the person reported
                e = a["ev"]
                sess = next(s for s in base_p["ev_sessions"] if s["ev_id"] == e)
                credited[e] = min(sess["requested_model_kwh"], credited[e] + base_p["ev_charger_model_kw"] * dt_h)
        if policy not in ("uncoordinated", "demand_cap"):
            last_fresh = not dropped and not late_now
            unverified_intervals += not last_fresh
    m = run_metrics(p_true, log, set(peaks), initial_pump_on=start_pump_on)
    m.update({"revisions_suppressed_equivalent": gate.get("SUPPRESSED_EQUIVALENT", 0),
              "revisions_suppressed_not_actionable": gate.get("SUPPRESSED_NOT_ACTIONABLE", 0),
              "revisions_deferred_cooldown": gate.get("DEFERRED_COOLDOWN", 0),
              "revisions_suppressed_no_improvement": gate.get("SUPPRESSED_NO_IMPROVEMENT", 0),
              "unverified_intervals": unverified_intervals,
              "ev_energy_unverified_model_kwh": round(sum(unverified.values()), 4),
              "pump_energy_model_kwh": round(sum(r["pump_model_kw"] for r in log) * dt_h, 4)})
    # every new plan or revision needs a manager acceptance in the advisory workflow; automatic control needs none
    m.update({"manager_acceptances": 0 if policy in ("uncoordinated", "demand_cap") else plans,
              "manual_switch_operations": 0 if policy in ("uncoordinated", "demand_cap") else switch_ops})
    m.update({"plans_computed": plans, "deviations_detected": deviations_seen, "missed_ev_actions": missed_ev_actions,
              "replans_without_missed_action": replans_without_miss,
              "electrical_meters": ELECTRICAL_METERS[policy],
              "authority": "automatic_local" if policy in ("uncoordinated", "demand_cap") else "human_advisory",
              "truth_log": log, "episodes": episodes,
              "participation": None if takes_part is None else {**takes_part, "manager_switch_operations": part_ops},
              "final_equipment_state": {**{k: copy.deepcopy(t[k]) for k in ("pump_on", "pump_run_intervals",
                                                                             "pump_off_intervals", "fine") if k in t},
                                        "manual": dict(manual), "episodes_open": copy.deepcopy(ep_now)}})  # evaluator-only; lets callers score against intervals the policy never saw
    return m


def draws_for(seed, n, scen, base_withdrawal):
    rng = random.Random(seed)
    d = {k: [rng.random() for _ in range(n)] for k in ("pump", "ev", "drop")}
    noise = scen.get("withdrawal_noise", 0)
    w = [round(x * (1 + rng.uniform(-noise, noise)), 1) for x in base_withdrawal] if noise else list(base_withdrawal)
    # separate stream so adding imperfection never changes the draws of earlier scenarios
    r2 = random.Random(seed * 1_000_003 + 7)
    d.update({k: [r2.random() for _ in range(n)] for k in ("claim", "ev_dropout", "late")})
    d.update({k: [r2.gauss(0, 1) for _ in range(n)] for k in ("power_noise", "tank_noise")})
    d["delay"] = [r2.random() for _ in range(n)]  # drawn last so earlier scenarios keep identical draws
    return d, w


def run(scenarios=None, policies=None, n_seeds=20, peaks=None, params=None):
    scenarios, policies = scenarios or list(SCENARIOS), policies or POLICIES
    unknown = (set(scenarios) - set(SCENARIOS)) | (set(policies) - set(POLICIES))
    if unknown:
        raise ValueError(f"unknown scenario/policy {sorted(unknown)}")
    peaks = list(range(40, 52)) if peaks is None else peaks
    rows = []
    for sname in scenarios:
        scen = SCENARIOS[sname]
        p = plant.make_params({**(params or {}), **scen.get("params", {})})
        seeds = list(range(n_seeds)) if scen["stochastic"] else [0]
        for seed in seeds:
            d, w = draws_for(seed, p["horizon_intervals"], scen, p["withdrawal_profile_l"])
            for pol in policies:
                m = run_policy(pol, p, w, d, scen, peaks)
                rows.append({"scenario": sname, "seed": seed, "policy": pol, "authority": m["authority"],
                             "electrical_meters": m["electrical_meters"],
                             "peak_participating_model_kw": m["peak_participating_model_kw"],
                             "allowance_exceedance_model_kwh": m["allowance_exceedance_model_kwh"],
                             "peak_window_energy_model_kwh": m["peak_window_energy_model_kwh"],
                             "total_participating_energy_model_kwh": m["total_participating_energy_model_kwh"],
                             "reserve_violation_intervals": m["reserve_violation_intervals"],
                             "unmet_withdrawal_l": m["unmet_withdrawal_l"], "final_tank_l": m["final_tank_l"],
                             "ev_shortfall_model_kwh": sum(v or 0 for v in m["ev_shortfall_at_departure_model_kwh"].values()),
                             "pump_starts": m["pump_starts"], "plans_computed": m["plans_computed"],
                             "deviations_detected": m["deviations_detected"], "missed_ev_actions": m["missed_ev_actions"],
                             "replans_without_missed_action": m["replans_without_missed_action"],
                             "manager_acceptances": m["manager_acceptances"],
                             "manual_switch_operations": m["manual_switch_operations"],
                             "restart_lockouts": m["restart_lockouts"], "safety_stops": m["safety_stops"],
                             "short_pump_runs": m["short_pump_runs"],
                             "revisions_suppressed_equivalent": m["revisions_suppressed_equivalent"],
                             "revisions_suppressed_not_actionable": m["revisions_suppressed_not_actionable"],
                             "revisions_deferred_cooldown": m["revisions_deferred_cooldown"],
                             "revisions_suppressed_no_improvement": m["revisions_suppressed_no_improvement"],
                             "unverified_intervals": m["unverified_intervals"],
                             "critical_level_violation_intervals": m["critical_level_violation_intervals"],
                             "ev_energy_unverified_model_kwh": m["ev_energy_unverified_model_kwh"],
                             "pump_energy_model_kwh": m["pump_energy_model_kwh"]})
    return rows, summarize(rows), {"peak_intervals": peaks, "seeds_per_stochastic_scenario": n_seeds,
                                   "scenarios": {s: SCENARIOS[s] for s in scenarios}, "policies": policies}


METRICS = ["peak_participating_model_kw", "allowance_exceedance_model_kwh", "peak_window_energy_model_kwh",
           "reserve_violation_intervals", "unmet_withdrawal_l", "ev_shortfall_model_kwh", "pump_starts",
           "plans_computed", "replans_without_missed_action", "manager_acceptances", "manual_switch_operations",
           "restart_lockouts", "safety_stops", "short_pump_runs", "revisions_suppressed_equivalent",
           "revisions_suppressed_not_actionable", "revisions_deferred_cooldown", "revisions_suppressed_no_improvement",
           "unverified_intervals", "critical_level_violation_intervals"]


def summarize(rows):
    out = {}
    for s in sorted({r["scenario"] for r in rows}):
        out[s] = {}
        for pol in sorted({r["policy"] for r in rows}, key=POLICIES.index):
            rs = [r for r in rows if r["scenario"] == s and r["policy"] == pol]
            out[s][pol] = {"runs": len(rs), **{m: {"mean": round(statistics.mean(r[m] for r in rs), 4),
                                                   "sd": round(statistics.pstdev(r[m] for r in rs), 4)} for m in METRICS}}
        for name, base in (("paired_measured_minus_ack", "ack_replan"), ("paired_branch_minus_aggregate", "aggregate_replan")):
            a = {r["seed"]: r for r in rows if r["scenario"] == s and r["policy"] == base}
            b = {r["seed"]: r for r in rows if r["scenario"] == s and r["policy"] == "measured_replan"}
            if a and b:
                diffs = {m: [b[k][m] - a[k][m] for k in a if k in b] for m in METRICS}
                # negative = measured_replan (branch meters) lower, i.e. better for these lower-is-better metrics
                out[s][name] = {m: {"mean": round(statistics.mean(v), 4), "min": round(min(v), 4), "max": round(max(v), 4),
                                    "measured_better_runs": sum(x < -1e-9 for x in v), "ties": sum(abs(x) <= 1e-9 for x in v),
                                    "baseline_better_runs": sum(x > 1e-9 for x in v)} for m, v in diffs.items()}
    return out


def commit():
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "simulation", "scheduler"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return (sha + "-dirty" if dirty else sha) or None
    except OSError:
        return None


def export(rows, summary, config, exp_id=None):
    exp_id = exp_id or f"exp-{uuid.uuid4().hex[:8]}"
    d = Path(os.environ.get("NIVAWATT_RESULTS_DIR", ROOT / "results" / "experiments")) / exp_id
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "runs.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    doc = {"experiment_id": exp_id, "source_kind": "synthetic_simulation", "software_commit": commit(),
           "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "config": config,
           "summary": summary, "notes": __doc__.split("Run:")[0].strip(),
           "limitations": ["synthetic plant and human-response model; not field or bench evidence",
                           "automatic_local policies have different control authority than human_advisory ones",
                           "initial seed set, not a significance test"]}
    (d / "summary.json").write_text(json.dumps(doc, indent=2))
    return exp_id, d


if __name__ == "__main__":
    rows, summary, cfg = run()
    eid, d = export(rows, summary, cfg, "exp-default")
    print(f"wrote {d} ({len(rows)} runs)")
    for s, v in summary.items():
        pm = v.get("paired_measured_minus_ack", {}).get("ev_shortfall_model_kwh")
        print(s, {pol: (v[pol]["ev_shortfall_model_kwh"]["mean"], v[pol]["reserve_violation_intervals"]["mean"],
                        v[pol]["peak_window_energy_model_kwh"]["mean"]) for pol in POLICIES if pol in v}, "paired EV:", pm)
