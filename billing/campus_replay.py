"""Build a four-connection synthetic billing replay from the causal HT-4 study."""
import json
from concurrent.futures import ProcessPoolExecutor

from billing import causal, connection, reference


def building(seed):
    case = causal.CASE
    baseline, baseline_metrics = reference.run_month("uncoordinated", days=causal.DAYS, s=seed)
    coordinated, coordinated_metrics = reference.run_month(
        "measured_replan", days=causal.DAYS, s=seed, provider=causal.WholeConnection(seed, case),
    )
    start = causal.DEV_DAYS
    comparison = reference.compare(
        case["category"], causal._eval(baseline, baseline_metrics), causal._eval(coordinated, coordinated_metrics),
        baseline[start:], coordinated[start:], case, seed, start,
    )
    spec = {**reference.CONN_SPEC, "noise_seed": reference.CONN_SPEC["noise_seed"] + 100 * seed}
    blocks = []
    for day in range(start, causal.DAYS):
        background = connection.nonflex_series(case["load"], day, reference._DS, spec, case["events"])
        traces = []
        for log in (baseline[day], coordinated[day]):
            traces.append([
                connection.interval_kva(nf, row["pump_model_kw"], row["ev_model_kw"], reference.PF)
                for nf, row in zip(background, log)
            ])
        for i in range(0, 96, 2):
            # [baseline kW, coordinated kW, baseline kVA, coordinated kVA], using clock-aligned pairs.
            blocks.append([
                round((trace[i][unit] + trace[i + 1][unit]) / 2, 5)
                for unit in (0, 2) for trace in traces
            ])
    md = comparison["md_30min"]
    return {
        "apartment_id": f"APT_{seed + 1}", "seed": seed, "blocks": blocks,
        "comparable": comparison["comparable"], "why_not": comparison["why_not"],
        "contract_demand_kva": case["contract_demand_kva"],
        "baseline": md["baseline"]["charges"], "coordinated": md["variant"]["charges"],
        "saving_rs": (
            -md["equal_terminal_storage"]["combined_applicable_rs"]
            if comparison["comparable"] else None
        ),
    }


if __name__ == "__main__":
    with ProcessPoolExecutor(max_workers=2) as pool:
        buildings = list(pool.map(building, range(4)))
    out = reference.ROOT / "web" / "campus-billing-data.json"
    out.write_text(json.dumps({
        "source": "flats-100-causal-v1",
        "case": "HT4_A_tight_cd",
        "category": "HT-4",
        "period": reference.REFERENCE["billing"]["tariff_period"],
        "blocks_per_day": 48,
        "start_hour_ist": reference._DS / 4,
        "buildings": buildings,
    }, separators=(",", ":")) + "\n", encoding="utf-8")
    print(out)
