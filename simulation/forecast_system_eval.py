"""Downstream effect of real held-out forecast errors on simulated apartments (simulation only).

For each held-out Karnataka day, each forecast's top-3 predicted hours become the advisory peak intervals.
Independent synthetic apartments (seeded per day and variant, shared by every policy for that pair) run
the existing rules scheduler with measured replanning and perfect human execution, so differences come
from the forecast. Outcomes are scored against the ACTUAL top-3 Karnataka hours of that day.

Truth withdrawals differ from the planning estimate (+-30 %). An oracle that knows the actual hours is an
unachievable upper bound. Apartment loads are model units: they are not BESCOM, feeder or real-building data.

Run (after python -m forecasting.candidate):  python -m simulation.forecast_system_eval
"""
import json
import random
import statistics
import time
from pathlib import Path

import pandas as pd

from simulation import experiment, plant

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "results" / "forecast_candidate" / "heldout_predictions.csv"
OUT = ROOT / "results" / "forecast_candidate"
FORECASTS = ["seasonal_naive", "ridge_lag_calendar", "hgb_profile_calendar"]
POLICIES = ["uncoordinated", "rules_no_advisory"] + [f"advisory_{m}" for m in FORECASTS] + ["advisory_oracle_actual"]
VARIANTS = 5
SCEN = {"p_miss_pump": 0, "p_miss_ev": 0, "p_drop": 0, "withdrawal_noise": 0.3}
METRICS = ["actual_peak_energy_kwh", "reserve_violation_intervals", "unmet_withdrawal_l", "ev_shortfall_kwh",
           "day_peak_hourly_kw", "rebound_hourly_kw", "pump_starts"]


def top3(values):
    return sorted(sorted(range(24), key=lambda h: (-values[h], h))[:3])


def intervals(hours):
    return [4 * h + q for h in hours for q in range(4)]


def apartment(date, k):
    """One synthetic apartment day. Daytime EV so advisories can matter; all values are assumptions."""
    rng = random.Random(f"{date}/{k}")
    arrive = rng.randint(24, 52)  # 06:00-13:00 IST
    return plant.make_params({
        "tank_init_l": round(rng.uniform(3500, 7000)), "allowance_model_kw": rng.choice([4.0, 6.0]),
        "ev_sessions": [{"ev_id": "EV_1", "arrival_interval": arrive,
                         "departure_interval": min(96, arrive + rng.randint(16, 40)),
                         "requested_model_kwh": round(rng.uniform(5, 14), 1)}]})


def outcomes(log, actual_hours):
    kw = [r["aggregate_model_kw"] for r in log]
    hourly = [sum(kw[4 * h:4 * h + 4]) / 4 for h in range(24)]
    after = {h for a in actual_hours for h in (a + 1, a + 2) if h < 24} - set(actual_hours)
    return {"actual_peak_energy_kwh": sum(hourly[h] for h in actual_hours),
            "day_peak_hourly_kw": max(hourly), "rebound_hourly_kw": max([hourly[h] for h in after], default=0.0)}


def simulate(date, actual, preds, k):
    p = apartment(date, k)
    seed = int(date.replace("-", "")) * 10 + k
    d, w = experiment.draws_for(seed, p["horizon_intervals"], SCEN, p["withdrawal_profile_l"])
    a_hours = top3(actual)
    peaks = {"uncoordinated": [], "rules_no_advisory": [], "advisory_oracle_actual": intervals(a_hours),
             **{f"advisory_{m}": intervals(top3(preds[m])) for m in FORECASTS}}
    rows = []
    for pol in POLICIES:
        m = experiment.run_policy("uncoordinated" if pol == "uncoordinated" else "measured_replan", p, w, d, SCEN, peaks[pol])
        rows.append({"date": date, "variant": k, "policy": pol, **outcomes(m["truth_log"], a_hours),
                     "reserve_violation_intervals": m["reserve_violation_intervals"],
                     "unmet_withdrawal_l": m["unmet_withdrawal_l"],
                     "ev_shortfall_kwh": sum(v or 0 for v in m["ev_shortfall_at_departure_model_kwh"].values()),
                     "pump_starts": m["pump_starts"]})
    return rows


def paired(df, a, b, metric):
    x = df[df.policy == a].set_index(["date", "variant"])[metric]
    y = df[df.policy == b].set_index(["date", "variant"])[metric]
    diff = (x - y).round(6)  # lower is better for every metric here
    return {"better": int((diff < 0).sum()), "tie": int((diff == 0).sum()), "worse": int((diff > 0).sum()),
            "mean_difference": round(float(diff.mean()), 4)}


def run():
    t0 = time.perf_counter()
    src = pd.read_csv(SRC)
    out = {"generated_at": pd.Timestamp.now("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "simulation only",
           "setup": {"variants_per_day": VARIANTS, "scenario": SCEN, "execution": "perfect (no human misses)",
                     "replanning": "measured_replan from simulation.experiment (rules scheduler, shared tank observations)",
                     "advisory": "top-3 forecast hours -> 12 model intervals", "scored_against": "actual top-3 Karnataka hours",
                     "rebound_hourly_kw": "max hourly participating kW in the 2 h after actual peak hours",
                     "units": "apartment model units; synthetic plant"}, "periods": {}}
    all_rows = []
    for period, g in src.groupby("period", sort=False):
        rows = []
        for date, day in g.groupby("ist_date"):
            actual = day[day.model == FORECASTS[0]].sort_values("hour_ist").actual_mw.tolist()
            preds = {m: day[day.model == m].sort_values("hour_ist").predicted_mw.tolist() for m in FORECASTS}
            for k in range(VARIANTS):
                rows += simulate(date, actual, preds, k)
        df = pd.DataFrame(rows)
        all_rows += [{"period": period, **r} for r in rows]
        means = df.groupby("policy")[METRICS].mean().round(3).reindex(POLICIES)
        hgb, ridge = "advisory_hgb_profile_calendar", "advisory_ridge_lag_calendar"
        x = df[df.policy == hgb].set_index(["date", "variant"]).actual_peak_energy_kwh
        y = df[df.policy == ridge].set_index(["date", "variant"]).actual_peak_energy_kwh
        loss = (x - y).groupby("date").mean().sort_values(ascending=False)
        worst = []
        for date in loss.index[:5]:
            if loss[date] <= 0:
                break
            day = g[g.ist_date == date]
            hrs = lambda m, col="predicted_mw": top3(day[day.model == m].sort_values("hour_ist")[col].tolist())
            worst.append({"ist_date": date, "hgb_minus_ridge_peak_kwh": round(float(loss[date]), 3),
                          "actual_top3_ist_hours": hrs(FORECASTS[0], "actual_mw"),
                          "ridge_top3": hrs("ridge_lag_calendar"), "hgb_top3": hrs("hgb_profile_calendar")})
        out["periods"][period] = {
            "days": int(df.date.nunique()), "runs": int(len(df)),
            "mean_by_policy": means.to_dict(orient="index"),
            "hgb_vs_current_ridge": {m: paired(df, hgb, ridge, m) for m in METRICS},
            "ridge_vs_naive_peak_energy": paired(df, ridge, "advisory_seasonal_naive", "actual_peak_energy_kwh"),
            "days_hgb_worse_than_ridge_on_peak_energy": int((loss > 1e-9).sum()),
            "days_hgb_better_than_ridge_on_peak_energy": int((loss < -1e-9).sum()),
            "largest_hgb_losses": worst,
        }
    out["runtime_s"] = round(time.perf_counter() - t0, 1)
    (OUT / "system_evaluation.json").write_text(json.dumps(out, indent=2, default=lambda o: o.item()))
    pd.DataFrame(all_rows).to_csv(OUT / "system_runs.csv", index=False)
    return out


if __name__ == "__main__":
    r = run()
    for period, v in r["periods"].items():
        print(period, v["days"], "days", v["runs"], "runs")
        for pol, m in v["mean_by_policy"].items():
            print(f"  {pol:32s}", {k: round(x, 2) for k, x in m.items()})
        print("  hgb vs ridge:", json.dumps(v["hgb_vs_current_ridge"]))
        print("  days hgb worse/better:", v["days_hgb_worse_than_ridge_on_peak_energy"], v["days_hgb_better_than_ridge_on_peak_energy"])
        print("  worst:", v["largest_hgb_losses"][:3])
    print("runtime", r["runtime_s"], "s")
