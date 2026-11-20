"""Causal whole-building demand forecast and the demand-aware planning cap (EXPERIMENTAL).

Inputs are only observations available before the decision:
- past days' whole-connection readings, already reduced to NON-FLEXIBLE load (whole connection minus the pump and EV
  branch channels; the whole-connection channel includes those branches, so they are subtracted, never added);
- today's whole-connection readings up to the last closed interval;
- the connection's contract demand (or sanctioned load) and the operator's hard participating-load allowance.

The billing-demand target is a PLANNING target, not an electrical safety limit: the planner's hard allowance and the
plant's protections are unchanged, and the cap only removes candidate pump/EV slots. When history or today's readings
are insufficient the documented fallback is the original advisory-only planning (no demand cap), shown with reduced
confidence. The fallback preserves water and EV service; it does not avoid demand charges.

Parameters are fixed in advance (not tuned on evaluation data):
WINDOW_DAYS history window, MIN_DAYS for a usable interval, SUSTAINED_K consecutive intervals above forecast by more
than SUSTAINED_REL to count as a sustained deviation, STALE_K missing readings in a row that make today's readings stale.
"""
import math
import statistics

WINDOW_DAYS, MIN_DAYS = 7, 3
SUSTAINED_K, SUSTAINED_REL = 4, 0.10
STALE_K = 2
FLEX_PF = 0.85  # lowest flexible-load power factor: converts kVA headroom into a conservative kW cap


def historical_profile(history, n=96, window=WINDOW_DAYS, min_days=MIN_DAYS):
    """Per-interval median of the last `window` days of non-flexible readings ({'kw','kvar'} or None when missing)."""
    days = history[-window:]
    kw, kvar, used = [], [], []
    for i in range(n):
        vals = [d[i] for d in days if d is not None and d[i] is not None]
        used.append(len(vals))
        kw.append(statistics.median(v["kw"] for v in vals) if len(vals) >= min_days else None)
        kvar.append(statistics.median(v["kvar"] for v in vals) if len(vals) >= min_days else None)
    ok = all(x is not None for x in kw)
    return {"kw": kw, "kvar": kvar, "days_available": min(used) if used else 0, "window_days": window,
            "status": "ok" if ok else "unavailable", "method": f"median of the last {window} days per interval (>= {min_days})"}


def kva(kw, kvar):
    return math.hypot(kw, kvar)


def peak_kva(fc, minutes=30, dt=15):
    s = [kva(a, b) for a, b in zip(fc["kw"], fc["kvar"])]
    m = minutes // dt
    return max(sum(s[k:k + m]) / m for k in range(0, len(s) - m + 1, m))


def target_kva(category, fc, contract_demand_kva=None, sanctioned_load_kw=None):
    """Tariff-relevant planning target: demand below it is not billed (HT floor / LT sanctioned load) or is already
    expected from non-flexible load alone."""
    if category == "HT-4":
        return max(0.9 * contract_demand_kva, peak_kva(fc))
    return max(sanctioned_load_kw, max(fc["kw"]))


def cap_kw(fc, target, category="HT-4"):
    """Per-interval participating-load cap (kW) from the target headroom; HT in kVA x FLEX_PF, LT in kW."""
    if category == "HT-4":
        return [round(max(0.0, (target - kva(a, b)) * FLEX_PF), 3) for a, b in zip(fc["kw"], fc["kvar"])]
    return [round(max(0.0, target - a), 3) for a in fc["kw"]]


def decide(history, today, i, category, contract_demand_kva=None, sanctioned_load_kw=None):
    """Planning decision at interval i (intervals < i of `today` are closed; None = reading missing).

    Returns mode 'demand_aware' with a cap, or 'fallback_advisory_only' with no cap, plus confidence and reasons."""
    fc = historical_profile(history)
    out = {"forecast": fc, "reasons": [], "sustained_deviation": False}
    if fc["status"] != "ok":
        out.update(mode="fallback_advisory_only", confidence="unavailable", cap=None, target_kva=None)
        out["reasons"].append(f"whole-building history too short: {fc['days_available']} usable days (< {MIN_DAYS})")
        return out
    recent = today[max(0, i - STALE_K):i]
    if i >= STALE_K and all(x is None for x in recent):
        out.update(mode="fallback_advisory_only", confidence="reduced", cap=None, target_kva=None)
        out["reasons"].append(f"whole-building readings stale: last {STALE_K} intervals missing")
        return out
    adj = {"kw": list(fc["kw"]), "kvar": list(fc["kvar"])}
    last = today[max(0, i - SUSTAINED_K):i]
    if len(last) == SUSTAINED_K and all(x is not None for x in last):
        idx = range(i - SUSTAINED_K, i)
        excess = [x["kw"] - fc["kw"][j] for x, j in zip(last, idx)]
        if all(e > SUSTAINED_REL * fc["kw"][j] for e, j in zip(excess, idx)):
            bump = statistics.mean(e / fc["kw"][j] for e, j in zip(excess, idx))  # relative, carried forward
            for j in range(i, len(adj["kw"])):
                adj["kw"][j] *= 1 + bump
                adj["kvar"][j] *= 1 + bump
            out["sustained_deviation"] = True
            out["reasons"].append(f"sustained deviation: last {SUSTAINED_K} intervals {bump:+.0%} above forecast; "
                                  "forecast raised for the rest of the day")
    tgt = target_kva(category, adj, contract_demand_kva, sanctioned_load_kw)
    out.update(mode="demand_aware", confidence="reduced" if out["sustained_deviation"] else "normal",
               cap=cap_kw(adj, tgt, category), target_kva=round(tgt, 3))
    return out
