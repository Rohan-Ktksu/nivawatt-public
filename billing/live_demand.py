"""Same-input demand comparison for an operator's synthetic live run."""
from billing import connection, reference
from simulation import experiment


def compare_completed(p, observations, elapsed, day_start, seed):
    """Compare normal controls with readings from the live switch-driven run."""
    scenario = {"stochastic": False, "p_drop": 0}
    draws, water = experiment.draws_for(seed, p["horizon_intervals"], scenario, p["withdrawal_profile_l"])
    baseline = experiment.run_policy("uncoordinated", p, water, draws, scenario, [])
    wc = p.get("whole_connection")
    spec = wc["spec"] if wc else {**reference.CONN_SPEC, "noise_seed": seed}
    background = connection.nonflex_series(
        wc["load"] if wc else "whole_building", wc["day_index"] if wc else 0, day_start, spec,
    )
    seen = {o["interval_index"]: o for o in observations if o["quality"] in ("VALID", "LATE")}
    points = []
    for i in range(min(elapsed, len(background))):
        nf, normal = background[i], baseline["truth_log"][i]
        base_kw, _, _ = connection.interval_kva(nf, normal["pump_model_kw"], normal["ev_model_kw"], reference.PF)
        o = seen.get(i, {})
        if wc:
            actual_kw = (o.get("whole_connection") or {}).get("average_power_model_kw")
        elif o.get("aggregate_model_kw") is not None:
            actual_kw = nf["flats_kw"] + nf["common_kw"] + nf["event_kw"] + o["aggregate_model_kw"]
        else:
            actual_kw = None
        points.append([round(base_kw, 4), None if actual_kw is None else round(actual_kw, 4)])
    return points
