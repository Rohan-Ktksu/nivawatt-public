"""Itemized electricity bill calculation from sourced KERC rules (billing/tariffs.json). Pure: no DB, no I/O beyond
reading the tariff file.

Status:
  complete       every component that applies was calculated (some may rest on flagged assumptions, listed);
  partial        at least one applicable component is unavailable: the total is NOT a bill and is withheld;
  not_estimable  the inputs cannot support a calculation (unsupported category, missing energy, period problems).

Missing inputs are None, never zero. A partial calculation never reports a total. Energy is kWh; money is computed in
Decimal and each line is rounded half-up to the paisa (as on the reference bill), then the bill to the nearest rupee.
Energy basis: only "bill" or "measured_raw" (unscaled physical meter) can describe a real bill; "simulated" and
"scaled_model" make the result an illustrative scenario.
"""
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

TARIFFS = json.loads((Path(__file__).with_name("tariffs.json")).read_text(encoding="utf-8"))
PAISA = Decimal("0.01")
REAL_BASES = {"bill", "measured_raw"}
ILLUSTRATIVE_BASES = {"simulated", "scaled_model"}


def money(x):
    return Decimal(str(x)).quantize(PAISA, rounding=ROUND_HALF_UP)


def _period(periods, start, last_day):
    """The single tariff period covering [start, last_day]; None if the billing period spans two (not supported)."""
    for p in periods:
        lo, hi = date.fromisoformat(p["valid_from"]), date.fromisoformat(p["valid_to"])
        if lo <= start and last_day <= hi:
            return p
    return None


def _line(component, label, amount, basis, source, formula, note=None):
    return {"component": component, "label": label, "amount_rs": None if amount is None else float(amount),
            "basis": basis, "source": source, "formula": formula, **({"note": note} if note else {})}


AMOUNT_KINDS = {
    "circuit_energy_cost": "Energy-related charges of the participating circuits (pump and EV chargers) only: not the "
                           "apartment's bill",
    "partial_charges": "Some charges of a connection bill; at least one charge is unknown, so this is not a bill total",
    "complete_bill": "A complete bill for one whole connection (every applicable charge calculated)",
}


def calculate(inp):
    """See _calculate. Adds amount_kind so no caller can mistake circuit costs or partial charges for a bill."""
    r = _calculate(inp)
    if r["status"] == "not_estimable":
        kind = None
    elif r.get("scope") == "participating_loads":
        kind = "circuit_energy_cost"
    else:
        kind = "complete_bill" if r["status"] == "complete" else "partial_charges"
    r["amount_kind"] = kind
    r["amount_kind_label"] = AMOUNT_KINDS.get(kind)
    return r


def _calculate(inp):
    """inp keys: category, period_start, period_end (ISO dates; end = next reading date, so days = end - start),
    energy_kwh, energy_basis ('bill'|'measured_raw'|'simulated'|'scaled_model'), sanctioned_load_kw (LT),
    recorded_md_kw (LT, optional), contract_demand_kva (HT), recorded_md_kva (HT, whole connection),
    fppca_paise_per_kwh (optional), include_tax (default True), fy2024_25_kwh (optional, for the FY25 true-up),
    scope ('connection' default | 'participating_loads': energy-related charges of the participating circuits only)."""
    out = {"category": inp.get("category"), "status": None, "kind": None, "lines": [], "total_rs": None,
           "rounding_adjustment_rs": None, "historical_true_up": None, "assumptions": [], "unavailable": [],
           "exclusions": [], "errors": []}
    cat = TARIFFS["categories"].get(inp.get("category"))
    if cat is None:
        why = TARIFFS["unsupported_categories"].get(inp.get("category"), "category not in the verified tariff set")
        out.update(status="not_estimable", errors=[f"Unsupported category {inp.get('category')!r}: {why}"])
        return out
    out["category_label"] = cat["label"]
    basis = inp.get("energy_basis")
    if basis not in REAL_BASES | ILLUSTRATIVE_BASES:
        out.update(status="not_estimable", errors=["energy_basis must be bill, measured_raw, simulated or scaled_model"])
        return out
    out["kind"] = "bill_reconstruction" if basis in REAL_BASES else "illustrative"
    scope = inp.get("scope", "connection")
    try:
        start, end = date.fromisoformat(inp["period_start"]), date.fromisoformat(inp["period_end"])
    except (KeyError, TypeError, ValueError):
        out.update(status="not_estimable", errors=["period_start and period_end must be ISO dates"])
        return out
    days = (end - start).days
    if days <= 0:
        out.update(status="not_estimable", errors=["period_end must be after period_start"])
        return out
    last_day = date.fromordinal(end.toordinal() - 1)
    tp = _period(cat["periods"], start, last_day)
    pg = _period(TARIFFS["pg_surcharge"]["periods"], start, last_day)
    if tp is None or pg is None:
        out.update(status="not_estimable", errors=[
            "The billing period is outside the verified tariff years or spans two of them; splitting a period across "
            "tariff years is not supported (the applicable rule for such a bill is unresolved)."])
        return out
    kwh = inp.get("energy_kwh")
    if kwh is None or not isinstance(kwh, (int, float)) or kwh < 0:
        out.update(status="not_estimable", errors=["energy_kwh is required (a non-negative number of kWh)"])
        return out
    out.update(period_start=start.isoformat(), period_end=end.isoformat(), billing_days=days,
               tariff_period={"valid_from": tp["valid_from"], "valid_to": tp["valid_to"]}, energy_kwh=kwh,
               energy_basis=basis, scope=scope)
    k = Decimal(str(kwh))
    lines, missing = out["lines"], out["unavailable"]

    energy_rate = Decimal(tp["energy_paise_per_kwh"]) / 100
    energy = money(k * energy_rate)
    lines.append(_line("energy", "Energy charge", energy, "verified", cat["source"],
                       f"{kwh} kWh x Rs.{energy_rate} per kWh"))

    # Fixed / demand charge: a property of the whole connection, so excluded for participating-load scope.
    if scope == "participating_loads":
        out["exclusions"].append("Fixed/demand charges belong to the whole connection and are not changed by scheduling "
                                 "the participating loads; they are excluded, not zero.")
    elif cat["kind"] == "fixed_per_kw":
        sl, md = inp.get("sanctioned_load_kw"), inp.get("recorded_md_kw")
        if sl is None:
            lines.append(_line("fixed", "Fixed charge", None, "unavailable", cat["source"], "needs sanctioned load"))
            missing.append("Sanctioned load in kW (needed for the fixed charge)")
        else:
            billing_kw = Decimal(str(max(sl, md))) if md is not None else Decimal(str(sl))
            # D1 6.15.7: LT billing kW rounded to the nearest quarter kW, minimum 1 kW
            billing_kw = max(Decimal(1), (billing_kw * 4).quantize(Decimal(1), rounding=ROUND_HALF_UP) / 4)
            if md is None:
                out["assumptions"].append("No recorded maximum demand given: billing load taken as the sanctioned load "
                                          "(the rule uses the higher of sanctioned load and recorded MD or rated load).")
            rate = Decimal(tp["fixed_rs_per_kw_month"])
            fixed = money(rate * billing_kw * Decimal(days) / Decimal(30))
            lines.append(_line("fixed", "Fixed charge", fixed, "verified rate; pro-rating observed on one bill",
                               f"{cat['source']}; {cat['billing_load_rule']}",
                               f"Rs.{rate}/kW/month x {billing_kw} kW x {days}/30 days"))
            out["assumptions"].append("Fixed charge pro-rated by billing days / 30: observed on one bill, rule text not found.")
    else:  # demand_per_kva (HT-4)
        cd, md = inp.get("contract_demand_kva"), inp.get("recorded_md_kva")
        if cd is None or md is None:
            lines.append(_line("demand", "Demand charge", None, "unavailable", cat["source"],
                               "needs contract demand and the whole connection's recorded maximum demand"))
            missing.append("Contract demand and the whole connection's recorded maximum demand in kVA (needed for the "
                           "demand charge); participating-load meters cannot supply them")
        else:
            rate = Decimal(tp["demand_rs_per_kva_month"])
            cd_d, md_d = Decimal(str(cd)), Decimal(str(md))
            # D1 HT billing condition 1(D): a fraction of 0.5 kVA or more rounds up to the next kVA; less is ignored
            md_d = md_d.quantize(Decimal(1), rounding=ROUND_HALF_UP)
            billing = max(md_d, Decimal("0.9") * cd_d)
            normal = min(billing, cd_d)
            excess = max(Decimal(0), md_d - cd_d)
            demand = money(rate * normal + 2 * rate * excess)
            out["demand_breakdown"] = {"recorded_md_kva_rounded": float(md_d), "billing_demand_kva": float(billing),
                                       "ordinary_rs": float(money(rate * normal)), "excess_rs": float(money(2 * rate * excess)),
                                       "excess_kva": float(excess)}
            lines.append(_line("demand", "Demand charge", demand, "verified", f"{cat['source']}; {cat['billing_demand_rule']}",
                               f"Rs.{rate}/kVA x {normal} kVA + 2 x Rs.{rate}/kVA x {excess} kVA excess"
                               f" (billing demand = max(MD {md_d}, 90% of CD {cd_d}))"))
            out["assumptions"].append("HT demand charge not pro-rated for billing days (no rule found); monthly basis assumed.")

    fp = inp.get("fppca_paise_per_kwh")
    if fp is None:
        lines.append(_line("fppca", "Fuel and power purchase cost adjustment", None, "unavailable",
                           TARIFFS["fppca"]["note"], "per-kWh rate printed on each bill"))
        missing.append("Fuel cost adjustment (FPPCA) rate for the month: it varies monthly and no verified value is held")
    else:
        fr = Decimal(str(fp)) / 100
        lines.append(_line("fppca", "Fuel and power purchase cost adjustment", money(k * fr), "input (from the bill)",
                           TARIFFS["fppca"]["note"], f"{kwh} kWh x Rs.{fr} per kWh"))

    pgr = Decimal(pg["paise_per_kwh"]) / 100
    lines.append(_line("pg_surcharge", TARIFFS["pg_surcharge"]["label"], money(k * pgr), "verified",
                       TARIFFS["pg_surcharge"]["source"], f"{kwh} kWh x Rs.{pgr} per kWh"))

    if inp.get("include_tax", True):
        t = TARIFFS["electricity_tax"]
        lines.append(_line("tax", t["label"], money(energy * Decimal(str(t["rate"]))), "assumption (unverified)",
                           t["note"], f"{t['rate'] * 100:g}% x energy charge Rs.{energy}"))
        out["assumptions"].append(f"Electricity tax {t['rate'] * 100:g}% of the energy charge: rate secondary-sourced, "
                                  "base observed on one bill.")
    else:
        out["exclusions"].append("Electricity tax excluded at the caller's request.")

    # FY25 true-up: a historical charge, reported separately from anything scheduling could change.
    tu = TARIFFS["fy25_true_up"]
    # D7: from the first meter reading on or after 1 May 2026 to the reading ending 30 April 2027 -> the reading date
    # (period_end) decides, not the period's last day.
    in_window = date.fromisoformat(tu["recovery_from"]) <= end <= date.fromisoformat(tu["recovery_to"])
    if scope == "participating_loads":
        out["exclusions"].append("FY25 true-up is a historical per-consumer charge, unaffected by scheduling; excluded.")
    elif in_window:
        fy = inp.get("fy2024_25_kwh")
        if fy is None:
            out["historical_true_up"] = {"amount_rs": None, "note": "Unknown: the consumer's FY2024-25 consumption was not given.",
                                         "source": tu["source"]}
            missing.append("The consumer's FY2024-25 consumption (needed for the FY25 true-up instalment)")
        else:
            inst = money(Decimal(str(fy)) * Decimal(tu["paise_per_kwh_of_fy2024_25_consumption"]) / 100 / tu["instalments"])
            out["historical_true_up"] = {"amount_rs": float(inst), "source": tu["source"],
                                         "formula": f"{fy} kWh x Rs.0.56 / {tu['instalments']} instalments"}
            lines.append(_line("fy25_true_up", tu["label"], inst, "verified", tu["source"],
                               f"{fy} kWh (FY2024-25) x Rs.0.56 / {tu['instalments']}"))

    if missing:
        out["status"] = "partial"  # never present a partial subtotal as a bill
        return out
    subtotal = sum(Decimal(str(x["amount_rs"])) for x in lines)
    if scope == "participating_loads":  # a cost component, not a bill: no bill rounding
        out.update(status="complete", subtotal_rs=float(subtotal), total_rs=float(subtotal), rounding_adjustment_rs=0.0)
        return out
    total = subtotal.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    out.update(status="complete", subtotal_rs=float(subtotal), total_rs=float(total),
               rounding_adjustment_rs=float(total - subtotal))
    out["assumptions"].append("Bill rounded to the nearest rupee: observed on one bill.")
    return out
