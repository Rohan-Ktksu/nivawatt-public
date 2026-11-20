"""Causal demand-aware candidate study (protocol flats-100-causal-v1), EXPERIMENTAL.

Separate from the perfect-mean-profile BENCHMARK (billing/reference.py `_demand_aware_allowance`), which is kept and
labelled. The candidate forecasts whole-building non-flexible load from past whole-connection readings only
(scheduler/demand_forecast.py) and is evaluated chronologically: days 0-29 are the development period (forecast
history and a pre-evaluation error check; no parameter is tuned on them), days 30-59 the evaluation month that is
scored and billed.

Synthetic whole-connection measurement: one channel at the main incomer (meter "connection-1"). It INCLUDES the pump
and EV branches; non-flexible load = whole connection - pump channel - EV channel (kvar of the branches from their
assumed power factors). It is never added to the branch channels, so nothing is counted twice.

Run: python -m billing.causal -> results/billing_scenario/flats-100-causal-v1/summary.json
"""
import json
import statistics
import subprocess

from billing import connection
from billing import reference as R
from scheduler import demand_forecast as DF

CASE = R.CASES["HT4_A_tight_cd"]
DAYS, DEV_DAYS = 60, 30


def _q(p, pf):
    return connection._q(p, pf)


class WholeConnection:
    """Synthetic whole-connection channel plus the causal provider that only reads past and current readings."""

    def __init__(self, s=0, case=CASE, missing_days=(), mode="causal"):
        self.s, self.case, self.missing, self.mode = s, case, set(missing_days), mode
        self.spec = {**R.CONN_SPEC, "noise_seed": R.CONN_SPEC["noise_seed"] + 100 * s}
        self.history, self.day, self.today, self.nf = [], None, None, None
        self.log = []  # per day: forecast at the day start, decisions, realized non-flexible load
        self.bench_cap = R._demand_aware_allowance(case)

    def start_day(self, d):
        self.day, self.today = d, [None] * 96
        self.nf = connection.nonflex_series(self.case["load"], d, R._DS, self.spec, self.case["events"])
        self.log.append({"day": d, "modes": [], "confidence": [], "sustained": 0, "forecast": None,
                         "realized_kw": [], "realized_kvar": []})

    def observe(self, i, rec):
        x = self.nf[i]
        nf_kw = x["flats_kw"] + x["common_kw"] + x["event_kw"]
        nf_kvar = (_q(x["flats_kw"], R.PF["flats"]) + _q(x["common_kw"], R.PF["common"]) + _q(x["event_kw"], R.PF["event"]))
        whole_kw = nf_kw + rec["pump_model_kw"] + rec["ev_model_kw"]
        whole_kvar = nf_kvar + _q(rec["pump_model_kw"], R.PF["pump"]) + _q(rec["ev_model_kw"], R.PF["ev"])
        self.log[-1]["realized_kw"].append(nf_kw)
        self.log[-1]["realized_kvar"].append(nf_kvar)
        if self.day in self.missing:
            return  # reading lost: stays None
        # non-flexible = whole connection minus the branch channels (subtracted, never added)
        self.today[i] = {"kw": whole_kw - rec["pump_model_kw"] - rec["ev_model_kw"],
                         "kvar": whole_kvar - _q(rec["pump_model_kw"], R.PF["pump"]) - _q(rec["ev_model_kw"], R.PF["ev"])}

    def cap(self, i):
        if self.mode == "benchmark":
            return self.bench_cap
        d = DF.decide(self.history, self.today, i, "HT-4", contract_demand_kva=self.case["contract_demand_kva"])
        lg = self.log[-1]
        if lg["forecast"] is None:
            lg["forecast"] = d["forecast"]
        lg["modes"].append(d["mode"])
        lg["confidence"].append(d["confidence"])
        lg["sustained"] += d["sustained_deviation"]
        return d["cap"]

    def end_day(self):
        self.history.append(self.today)


def forecast_errors(log, days):
    """Day-ahead (start-of-day) forecast vs realized non-flexible load, per evaluated day."""
    rows = [x for x in log if x["day"] in days and x["forecast"] and x["forecast"]["status"] == "ok"]
    if not rows:
        return None
    ape, bias, peak_err = [], [], []
    for x in rows:
        f, a = x["forecast"]["kw"], x["realized_kw"]
        ape += [abs(fi - ai) / ai for fi, ai in zip(f, a)]
        bias += [(fi - ai) / ai for fi, ai in zip(f, a)]
        real = {"kw": a, "kvar": x["realized_kvar"]}
        peak_err.append((DF.peak_kva(x["forecast"]) - DF.peak_kva(real)) / DF.peak_kva(real))
    return {"days": len(rows), "mape_pct": round(100 * statistics.mean(ape), 2), "bias_pct": round(100 * statistics.mean(bias), 2),
            "peak_30min_kva_error_pct": {"mean": round(100 * statistics.mean(peak_err), 2),
                                         "min": round(100 * min(peak_err), 2), "max": round(100 * max(peak_err), 2)}}


def decision_counts(log, days):
    rows = [x for x in log if x["day"] in days]
    modes = [m for x in rows for m in x["modes"]]
    conf = [c for x in rows for c in x["confidence"]]
    return {"planning_decisions": len(modes), "fallback_decisions": modes.count("fallback_advisory_only"),
            "reduced_confidence_decisions": conf.count("reduced"), "unavailable_decisions": conf.count("unavailable"),
            "sustained_deviation_decisions": sum(x["sustained"] for x in rows),
            "days_with_sustained_deviation": sum(x["sustained"] > 0 for x in rows)}


PROTOCOL = {
    "id": "flats-100-causal-v1",
    "kind": "EXPERIMENTAL causal demand-aware candidate on the flats-100-reference-v2 plant, declared before it was run; "
            "synthetic and tariff-dependent",
    "periods": {"development_days": [0, DEV_DAYS - 1], "evaluation_days": [DEV_DAYS, DAYS - 1],
                "note": "chronological split. Development days supply forecast history and a pre-evaluation error check; "
                        "no forecaster or planner parameter is tuned on them. Only the evaluation month is scored and "
                        "billed (as June 2026, HT-4, tight CD 168 kVA)"},
    "policies": {
        "baseline": "competent float switch + EV on plug-in (no NivaWatt)",
        "original_100 / original_70": "original advisory-only scheduler, 100 % / 70 % schedule participation",
        "benchmark_100 / benchmark_70": "demand-aware with the PERFECT-MEAN-PROFILE cap (generator's expected profile): "
                                        "a labelled BENCHMARK, not deployable",
        "causal_100 / causal_70": "demand-aware CANDIDATE: cap from scheduler/demand_forecast.py using only past and "
                                  "current whole-connection readings"},
    "inputs_at_decision_time": {
        "all NivaWatt policies": "measured tank level and EV energy up to the last closed interval; EV session window and "
                                 "request; the expected water-withdrawal profile (the synthetic generator's mean, shared "
                                 "by every NivaWatt policy: a known limitation); assumed advisory window; hard allowance",
        "causal candidate adds": "past days' whole-connection readings minus branch channels (non-flexible history) and "
                                 "today's readings up to the last closed interval; contract demand",
        "benchmark adds": "the generator's expected non-flexible profile (not available to a real system)"},
    "forecaster": {"method": "per-interval median of the last 7 days (>= 3 usable days)",
                   "sustained_deviation": "4 consecutive intervals > 10 % above forecast: forecast raised by the mean "
                                          "relative excess for the rest of the day; confidence reduced",
                   "stale": "2 consecutive missing whole-connection readings, or < 3 usable history days: fallback to "
                            "the original advisory-only planning; confidence reduced / unavailable",
                   "target": "planning target = max(90 % of CD, forecast non-flexible 30-minute peak kVA); cap = "
                             "(target - forecast kVA) x 0.85 kW per interval, never above the hard 13 kW allowance. "
                             "NOT an electrical safety limit"},
    "whole_connection_channel": "synthetic meter connection-1 at the main incomer; includes the pump and EV branches; "
                                "non-flexible = whole - pump - EV; never added to the branch channels",
    "participation": {"p": 0.7, "seed": "5000 + 100 s", "model": "flats-100-participation-v1 (one draw per day and "
                                                                  "device; fallback to float switch / EV on plug-in)"},
    "matched_seeds": [0, 1, 2, 3, 4],
    "sensitivities_seed_0": {
        "load_growth": "flats' load 10 % above normal on evaluation days 45-51 (not forecast): baseline, original_100, "
                       "benchmark_100, causal_100",
        "stale_readings": "whole-connection readings lost on days 40-41: causal_100 (fallback exercised)"},
    "billing": "HT-4 tight CD (168 kVA), 30-minute MD (Conditions of Supply cl. 2.38); ordinary, excess and energy "
               "charges itemized; equal-service comparison only when service is comparable; storage normalized on the "
               "evaluation period's storage change (analytical, labelled)",
    "not_estimated": "utility procurement benefit",
}


def _eval(logs, ms):
    s = R.summarize(logs[DEV_DAYS:], ms[DEV_DAYS:])
    s["service"]["initial_tank_l"] = round(ms[DEV_DAYS - 1]["final_tank_l"], 1)
    return s


def _participation(ms):
    if ms[0]["participation"] is None:
        return {"manager_switch_operations": sum(m["manual_switch_operations"] for m in ms)}
    return {"pump_days_not_participating": [d for d, m in enumerate(ms, DEV_DAYS) if not m["participation"]["pump"]],
            "ev_nights_not_participating": [d for d, m in enumerate(ms, DEV_DAYS) if not m["participation"]["ev"]],
            "realized_rate": round(sum(m["participation"]["pump"] + m["participation"]["ev"] for m in ms) / (2 * len(ms)), 3),
            "manager_switch_operations": sum(m["participation"]["manager_switch_operations"] for m in ms)}


def run_seed(s, case=CASE, policies=None, missing_days=()):
    part = {"p": PROTOCOL["participation"]["p"], "seed": 5000 + 100 * s}
    spec = {
        "baseline": dict(policy="uncoordinated"),
        "original_100": dict(policy="measured_replan"),
        "original_70": dict(policy="measured_replan", participation=part),
        "benchmark_100": dict(policy="measured_replan", provider="benchmark"),
        "benchmark_70": dict(policy="measured_replan", provider="benchmark", participation=part),
        "causal_100": dict(policy="measured_replan", provider="causal"),
        "causal_70": dict(policy="measured_replan", provider="causal", participation=part),
    }
    out, raw = {}, {}
    for name in (policies or spec):
        kw = dict(spec[name])
        mode = kw.pop("provider", None)
        prov = WholeConnection(s, case, missing_days, mode) if mode else None
        logs, ms = R.run_month(kw.pop("policy"), days=DAYS, s=s, provider=prov, **kw)
        summ = _eval(logs, ms)
        summ["participation"] = _participation(ms[DEV_DAYS:])
        if mode == "causal":
            summ["forecast"] = {"development": forecast_errors(prov.log, range(0, DEV_DAYS)),
                                "evaluation": forecast_errors(prov.log, range(DEV_DAYS, DAYS)),
                                "decisions_evaluation": decision_counts(prov.log, range(DEV_DAYS, DAYS))}
        out[name], raw[name] = summ, logs[DEV_DAYS:]
    bill = {v: R.compare(case.get("name", "HT4_A_tight_cd"), out["baseline"], out[v], raw["baseline"], raw[v], case, s, DEV_DAYS)
            for v in out if v != "baseline"} if "baseline" in out else {}
    return {"runs": out, "service": {k: R.meets_standard(v)[0] for k, v in out.items()}, "billing": bill}


def run():
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=R.ROOT, capture_output=True, text=True).stdout.strip()
    res = {"protocol": PROTOCOL, "software_commit": sha, "seeds": {s: run_seed(s) for s in PROTOCOL["matched_seeds"]}}
    grow = {**CASE, "events": [{"day": d, "from_interval": 0, "to_interval": 96, "flats_scale": 0.10} for d in range(45, 52)]}
    res["sensitivity_load_growth_seed0"] = run_seed(0, grow, ["baseline", "original_100", "benchmark_100", "causal_100"])
    res["sensitivity_stale_readings_seed0"] = run_seed(0, CASE, ["baseline", "causal_100"], missing_days=(40, 41))
    agg = {}
    for v in ("original_100", "original_70", "benchmark_100", "benchmark_70", "causal_100", "causal_70"):
        rows = [res["seeds"][s]["billing"][v] for s in PROTOCOL["matched_seeds"]]
        eq = [r["md_30min"]["equal_terminal_storage"]["combined_applicable_rs"] for r in rows]
        rawc = [r["md_30min"]["differences_raw_rs"]["combined_applicable_rs"] for r in rows]
        agg[v] = {"comparable_seeds": sum(r["comparable"] for r in rows), "of": len(rows),
                  "combined_equal_service_rs_per_seed": eq, "combined_raw_rs_per_seed": rawc}
    res["across_seeds"] = agg
    return res


if __name__ == "__main__":
    out = run()
    d = R.ROOT / "results" / "billing_scenario" / PROTOCOL["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(json.dumps(out["across_seeds"]))
