"""Causal demand-aware candidate: forecast causality, fallback, sustained deviation and the whole-connection channel."""
import copy

from billing import causal, connection
from billing import reference as R
from scheduler import demand_forecast as DF


def _day(kw=100.0, kvar=30.0):
    return [{"kw": kw, "kvar": kvar} for _ in range(96)]


def test_forecast_uses_only_past_days_and_needs_enough_history():
    assert DF.historical_profile([_day(), _day()])["status"] == "unavailable"  # 2 < 3 usable days
    fc = DF.historical_profile([_day(90), _day(100), _day(110), _day(500)] + [_day(100)] * 3)
    assert fc["status"] == "ok" and fc["kw"][0] == 100  # median of the last 7 days: robust to one odd day
    gappy = [_day(), None, _day(), None, None, None, None]
    assert DF.historical_profile(gappy)["status"] == "unavailable"  # only 2 usable days inside the window


def test_decision_ignores_the_future():
    hist = [_day()] * 7
    today = [{"kw": 100.0, "kvar": 30.0}] * 10 + [None] * 86
    a = DF.decide(hist, today, 10, "HT-4", contract_demand_kva=168)
    fut = copy.deepcopy(today)
    fut[10:] = [{"kw": 999.0, "kvar": 999.0}] * 86  # readings after the decision interval
    b = DF.decide(hist, fut, 10, "HT-4", contract_demand_kva=168)
    assert a["cap"] == b["cap"] and a["mode"] == b["mode"] == "demand_aware"


def test_fallbacks_are_advisory_only_with_reduced_confidence():
    short = DF.decide([_day()] * 2, [None] * 96, 0, "HT-4", contract_demand_kva=168)
    assert short["mode"] == "fallback_advisory_only" and short["cap"] is None and short["confidence"] == "unavailable"
    today = [{"kw": 100.0, "kvar": 30.0}] * 20 + [None, None] + [None] * 74
    stale = DF.decide([_day()] * 7, today, 22, "HT-4", contract_demand_kva=168)
    assert stale["mode"] == "fallback_advisory_only" and stale["confidence"] == "reduced"


def test_sustained_deviation_raises_forecast_and_tightens_cap():
    hist = [_day()] * 7
    normal = DF.decide(hist, [{"kw": 100.0, "kvar": 30.0}] * 8 + [None] * 88, 8, "HT-4", contract_demand_kva=100)
    high = DF.decide(hist, [{"kw": 100.0, "kvar": 30.0}] * 4 + [{"kw": 120.0, "kvar": 36.0}] * 4 + [None] * 88, 8,
                     "HT-4", contract_demand_kva=100)
    assert high["sustained_deviation"] and high["confidence"] == "reduced"
    assert high["target_kva"] > normal["target_kva"]  # the target follows the higher expected peak; a planning target only
    blip = DF.decide(hist, [{"kw": 100.0, "kvar": 30.0}] * 7 + [{"kw": 130.0, "kvar": 40.0}] + [None] * 88, 8,
                     "HT-4", contract_demand_kva=100)
    assert not blip["sustained_deviation"]  # one high interval is not sustained


def test_planning_target_is_not_a_safety_limit_and_hard_allowance_stays():
    fc = DF.historical_profile([_day(150, 50)] * 7)
    t = DF.target_kva("HT-4", fc, contract_demand_kva=168)
    assert t == max(0.9 * 168, DF.peak_kva(fc))
    cap = DF.cap_kw(fc, t)
    assert all(c >= 0 for c in cap)  # the planner applies min(hard allowance, cap); protections are in the plant


def test_whole_connection_channel_includes_branches_and_derived_nonflex_is_not_double_counted():
    w = causal.WholeConnection(s=0)
    w.start_day(3)
    rec = {"pump_model_kw": 5.5, "ev_model_kw": 7.4}
    w.observe(10, rec)
    x = w.nf[10]
    nf_kw = x["flats_kw"] + x["common_kw"] + x["event_kw"]
    assert abs(w.today[10]["kw"] - nf_kw) < 1e-9  # whole (nf + 12.9) minus branches: exactly the non-flexible load
    assert abs(w.log[-1]["realized_kw"][0] - nf_kw) < 1e-9
    m = causal.WholeConnection(s=0, missing_days=(3,))
    m.start_day(3)
    m.observe(10, rec)
    assert m.today[10] is None  # a lost reading stays missing, never zero


def test_causal_provider_falls_back_until_history_exists():
    prov = causal.WholeConnection(s=0)
    logs, ms = R.run_month("measured_replan", days=4, provider=prov)
    assert set(prov.log[0]["modes"]) == {"fallback_advisory_only"}  # day 0: no history
    assert "demand_aware" in prov.log[3]["modes"]  # day 3: three usable past days
    assert len(prov.history) == 4


def test_storage_normalization_uses_storage_change():
    case = R.CASES["HT4_B_generous_cd"]
    log = [{"pump_model_kw": 0.0, "ev_model_kw": 0.0} for _ in range(96)]
    svc = lambda init, fin: {"service": {"unmet_water_l": 0, "reserve_minutes": 0, "critical_minutes": 0,  # noqa: E731
                                         "ev_sessions_short": 0, "overflow_l": 0, "initial_tank_l": init,
                                         "final_tank_l": fin}}
    c = R.compare("x", svc(20000, 20000), svc(19000, 19000), [log], [log], case)  # same change: no adjustment
    assert c["md_30min"]["equal_terminal_storage"]["normalization_rs"] == 0.0
    assert connection.max_demand([1, 1], 30) == 1
