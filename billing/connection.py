"""Synthetic whole-connection load and maximum demand for the fictional 100-flat reference study.

Everything here is a declared ASSUMPTION (see billing/scenario.py REFERENCE protocol), not a measured building.
Non-flexible loads are added to the simulated pump and EV circuits to form the connection's load; maximum demand is
the highest average over the demand-integration period (30 minutes assumed; 15 minutes reported as a sensitivity).
kVA uses per-load power factors and vector sums: kW is never taken as kVA.
"""
import math
import random

from billing.calculator import calculate


def _hour(i, day_start):
    return ((i + day_start) % 96) / 4


def nonflex_kw(kind, i, day_start, spec):
    """Expected (forecast) non-flexible load at interval i: (flats_kw, common_kw)."""
    h = _hour(i, day_start)
    common = spec["stp_kw"] + spec["misc_kw"] + (spec["lift_peak_kw"] if (6 <= h < 10 or 18 <= h < 22) else spec["lift_kw"]) \
        + (spec["lighting_kw"] if (h >= 18 or h < 6) else 0.0)
    if kind == "common_area":  # flats separately metered: the connection carries common services only
        return 0.0, common
    flat = next(v for (a, b), v in spec["flat_kw_by_hour"] if a <= h < b)
    return flat * spec["flats"], common


def nonflex_series(kind, day, day_start, spec, events=()):
    """Actual non-flexible load for one simulated day: forecast x per-interval noise (own seed stream) + events."""
    rng = random.Random(spec["noise_seed"] + day)
    out = []
    for i in range(96):
        f, c = nonflex_kw(kind, i, day_start, spec)
        z = 1 + rng.uniform(-spec["noise"], spec["noise"])
        hit = [e for e in events if e["day"] == day and e["from_interval"] <= i < e["to_interval"]]
        ev = sum(e.get("kw", 0.0) for e in hit)
        fs = 1 + sum(e.get("flats_scale", 0.0) for e in hit)  # e.g. 0.10: flats' load 10 % higher (unforecast)
        out.append({"flats_kw": f * z * fs, "common_kw": c * z, "event_kw": ev})
    return out


def _q(p, pf):
    return p * math.tan(math.acos(pf))


def interval_kva(nf, pump_kw, ev_kw, pf):
    """Apparent power of the whole connection for one 15-minute interval (mean powers)."""
    p = nf["flats_kw"] + nf["common_kw"] + nf["event_kw"] + pump_kw + ev_kw
    q = (_q(nf["flats_kw"], pf["flats"]) + _q(nf["common_kw"], pf["common"]) + _q(nf["event_kw"], pf["event"])
         + _q(pump_kw, pf["pump"]) + _q(ev_kw, pf["ev"]))
    return p, q, math.hypot(p, q)


def max_demand(series, minutes):
    """Highest mean over consecutive clock-aligned blocks of `minutes` (series of 15-minute means)."""
    n = minutes // 15
    return max(sum(series[k:k + n]) / n for k in range(0, len(series) - n + 1, n))


def connection_month(case, nf_days, logs, pf):
    """Whole-connection energy, kVAh, power factor and MD for one policy run (lists of per-day records)."""
    kw, kva, kvarh = [], [], 0.0
    for nf_day, log in zip(nf_days, logs):
        for nf, r in zip(nf_day, log):
            p, q, s = interval_kva(nf, r["pump_model_kw"], r["ev_model_kw"], pf)
            kw.append(p)
            kva.append(s)
            kvarh += q / 4
    kwh, kvah = sum(kw) / 4, sum(kva) / 4
    return {"energy_kwh": round(kwh, 3), "kvah": round(kvah, 3), "power_factor": round(kwh / kvah, 3),
            "md_kva": {m: round(max_demand(kva, m), 3) for m in (30, 15)},
            "md_kw": {m: round(max_demand(kw, m), 3) for m in (30, 15)}}


def bill_components(case, conn, tariff, md_minutes):
    """Calculator lines split into energy-related, ordinary demand/fixed and excess demand (no double counting)."""
    inp = {"category": case["category"], "period_start": tariff["period_start"], "period_end": tariff["period_end"],
           "energy_kwh": conn["energy_kwh"], "energy_basis": "simulated", "fppca_paise_per_kwh": None}
    if case["category"] == "HT-4":
        inp.update(contract_demand_kva=case["contract_demand_kva"], recorded_md_kva=conn["md_kva"][md_minutes])
    else:  # LT: billing on kW (assumption: the meter's MD in kW is used for LT-1)
        inp.update(sanctioned_load_kw=case["sanctioned_load_kw"], recorded_md_kw=conn["md_kw"][md_minutes])
    r = calculate(inp)
    amt = {x["component"]: x["amount_rs"] for x in r["lines"]}
    energy = round(sum(amt.get(k) or 0.0 for k in ("energy", "pg_surcharge", "tax")), 2)
    if case["category"] == "HT-4":
        b = r["demand_breakdown"]
        ordinary, excess, billing = b["ordinary_rs"], b["excess_rs"], b["billing_demand_kva"]
        pf_short = max(0, round((0.90 - conn["power_factor"]) * 100))
        pf_rs = round(conn["energy_kwh"] * 0.03 * pf_short, 2)  # D1 HT condition 2: 3 paise/kWh per 0.01 below 0.90
    else:
        ordinary, excess, billing, pf_rs = amt["fixed"], 0.0, max(case["sanctioned_load_kw"], conn["md_kw"][md_minutes]), 0.0
    return {"energy_related_rs": energy, "ordinary_demand_rs": ordinary, "excess_demand_rs": excess,
            "pf_surcharge_rs": pf_rs, "billing_demand": billing, "status": "partial (FPPCA unknown)",
            "known_total_rs": round(energy + ordinary + excess + pf_rs, 2)}
