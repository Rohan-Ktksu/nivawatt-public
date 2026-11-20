import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from forecasting import data, evaluate, models
from scheduler import rules
from simulation import plant

HAS_DATA = data.RAW.exists()
FS = "2026-08-18T18:30:00Z"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NIVAWATT_DB", str(tmp_path / "t.db"))
    codes = tmp_path / "codes.json"
    codes.write_text(json.dumps({"operator_code": "op", "manager_codes": {"APT_1": "m1", "APT_2": "m2"},
                                 "device_tokens": {"bench-1": "devtok"}}))
    monkeypatch.setenv("NIVAWATT_CODES", str(codes))
    monkeypatch.setenv("NIVAWATT_RESULTS_DIR", str(tmp_path / "exp"))
    from backend import db, main
    db.reset()
    c = TestClient(main.app)

    def login(role, apt=None, code=None):
        r = c.post("/api/v1/demo/session", json={"role": role, "apartment_id": apt, "access_code": code})
        return {"Authorization": "Bearer " + r.json()["token"]}
    c.op, c.mgr, c.mgr2 = login("operator", code="op"), login("manager", "APT_1", "m1"), login("manager", "APT_2", "m2")
    c.dev = {"Authorization": "Bearer devtok"}
    yield c
    db.reset()


def new_run(c, **params):
    r = c.post("/api/v1/runs", headers=c.op, json={"request_id": f"run-{len(params)}-{id(params)}",
                                                   "clock_start_utc": FS, "apartment_params": params})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def synthetic_forecast(c):
    """Insert a synthetic snapshot directly so API tests do not depend on the dataset."""
    from backend import db
    hours = pd.date_range(FS.replace("Z", ""), periods=24, freq="h")
    pts = [{"start": models.iso(t), "end": models.iso(t + models.H), "predicted_mw": 1000 + i} for i, t in enumerate(hours)]
    snap = {"forecast_id": "fc-synth", "source_kind": "synthetic", "geographic_scope": "Synthetic", "points": pts,
            "forecast_start": pts[0]["start"], "forecast_end": pts[-1]["end"], "peak_windows": pts[-3:]}
    with db.LOCK:
        db.ex("INSERT OR IGNORE INTO forecasts VALUES(?,?,?)", "fc-synth", db.dumps(snap), "x")
    return snap


def advise(c, run_id, windows, rid="adv-1", kfi=0):
    return c.post("/api/v1/events", headers=c.op, json={
        "request_id": rid, "run_id": run_id, "kind": "peak_advisory", "known_from_interval": kfi,
        "forecast_id": "fc-synth", "target_apartments": ["APT_1"], "peak_windows": windows,
        "valid_from": FS, "valid_until": "2026-08-19T18:30:00Z"})


def state(c, run_id, h=None):
    return c.get(f"/api/v1/state?run_id={run_id}&apartment_id=APT_1", headers=h or c.mgr).json()


def accept(c, run_id, prop, rid):
    return c.post(f"/api/v1/proposals/{prop['proposal_id']}/decision", headers=c.mgr, json={
        "run_id": run_id, "expected_revision": prop["revision"], "decision": "ACCEPT",
        "option_id": prop["options"][0]["option_id"], "request_id": rid})


def step(c, run_id, rid, **kw):
    r = c.post(f"/api/v1/runs/{run_id}/step", headers=c.op, json={"request_id": rid, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- the P0 loop ----------
def test_p0_loop_false_done_detected_and_revised(api):
    c = api
    snap = synthetic_forecast(c)
    run = new_run(c)
    assert state(c, run)["active_advisory"] is None
    # manager cannot issue an advisory
    r = c.post("/api/v1/events", headers=c.mgr, json={"request_id": "x", "run_id": run, "kind": "peak_advisory"})
    assert r.status_code == 403
    r = advise(c, run, snap["peak_windows"])
    assert r.status_code == 200, r.text
    adv = r.json()
    s = state(c, run)
    assert s["active_advisory"]["advisory_id"] == adv["advisory_id"] and s["forecast_id"] == "fc-synth"
    prop = s["pending_proposals"][0]
    assert prop["advisory_id"] == adv["advisory_id"] and prop["source"] == "rules"
    # accept exact version; retry is idempotent; acceptance does not operate equipment
    a1 = accept(c, run, prop, "acc-1")
    assert a1.status_code == 200
    assert accept(c, run, prop, "acc-1").json()["active_commitment"] == a1.json()["active_commitment"]
    assert accept(c, run, prop, "acc-2").status_code == 409  # already accepted -> stale
    s = state(c, run)
    com = s["active_commitment"]
    assert com and s["execution"]["observation_status"] == "UNOBSERVED"
    # find first interval where the plan wants something ON
    acts = com["option"]["actions"]
    first_on = next(a for a in acts if a["pump"] == "ON" or a["ev"] != "OFF")
    k = first_on["interval_index"]
    for j in range(k):  # follow the plan faithfully until then
        a = acts[j]
        step(c, run, f"s{j}", actual_switches={"pump": a["pump"], "ev": "ON" if a["ev"] != "OFF" else "OFF"})
    kind = "pump" if first_on["pump"] == "ON" else "ev"
    cl = c.post("/api/v1/execution/claims", headers=c.mgr, json={
        "run_id": run, "commitment_id": com["commitment_id"], "interval_index": k,
        "action_id": f"{com['commitment_id']}:{k}:{kind}", "request_id": "claim-1"})
    assert cl.status_code == 200 and cl.json()["status"] == "CLAIMED_EXECUTED"
    # claim does not make anything happen: switches stay OFF
    out = step(c, run, "s-false", actual_switches={"pump": "OFF", "ev": "OFF"})
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION"
    s = out["state"]
    assert s["execution"]["claim"] == "CLAIMED_EXECUTED"  # claim preserved despite contradiction
    assert s["active_commitment"]["commitment_id"] == com["commitment_id"]  # revision not auto-activated
    rev = s["pending_proposals"][0]
    assert rev["reason"] == "CLAIM_WITHOUT_OBSERVED_EXECUTION" and rev["status"] == "PROPOSED"
    # accept the revision, actually perform it, observe a match
    assert accept(c, run, rev, "acc-rev").status_code == 200
    s = state(c, run)
    a = next(x for x in s["active_commitment"]["option"]["actions"] if x["interval_index"] == s["clock"]["interval_index"])
    out = step(c, run, "s-good", actual_switches={"pump": a["pump"], "ev": "ON" if a["ev"] != "OFF" else "OFF"})
    assert out["execution"]["observation_status"] == "OBSERVED_MATCH"
    rep = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()
    assert rep["counts"]["claims_contradicted"] == 1 and rep["counts"]["acceptances"] == 2
    # restart: new connection, same file -> history intact
    from backend import db
    db.reset()
    s2 = state(c, run)
    assert s2["active_commitment"]["commitment_id"] == s["active_commitment"]["commitment_id"]
    assert s2["execution"]["claims"] == s["execution"]["claims"]
    assert c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()["counts"] == rep["counts"]


def test_authorization_and_scope(api):
    c = api
    run = new_run(c)
    assert c.get(f"/api/v1/state?run_id={run}&apartment_id=APT_1").status_code == 401
    assert c.get(f"/api/v1/state?run_id={run}&apartment_id=APT_1", headers=c.mgr2).status_code == 403
    assert c.post("/api/v1/runs", headers=c.mgr, json={"request_id": "r", "clock_start_utc": FS}).status_code == 403
    r = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": "q", "allowance_model_kw": 99})
    assert r.status_code == 403
    assert state(c, run)["apartment"]["allowance_model_kw"] == 6.0
    assert c.post("/api/v1/demo/session", json={"role": "operator", "access_code": "bad"}).status_code == 401


def test_request_id_reuse_with_different_body_conflicts(api):
    c = api
    new_run(c)
    r = c.post("/api/v1/runs", headers=c.op, json={"request_id": "run-0-" , "clock_start_utc": FS})
    r2 = c.post("/api/v1/runs", headers=c.op, json={"request_id": "run-0-", "clock_start_utc": "2026-08-17T18:30:00Z"})
    assert r.status_code == 200 and r2.status_code == 409


def test_advisory_validation(api):
    c = api
    snap = synthetic_forecast(c)
    run = new_run(c)
    bad = [{"start": "2026-08-18T18:45:00Z", "end": "2026-08-18T19:45:00Z"}]
    assert advise(c, run, bad, "a1").status_code == 422  # not a forecast hour
    step(c, run, "s0")
    assert advise(c, run, snap["peak_windows"], "a2", kfi=0).status_code == 422  # retroactive
    # future-known advisory is not visible early
    assert advise(c, run, snap["peak_windows"], "a3", kfi=3).status_code == 200
    assert state(c, run)["active_advisory"] is None
    step(c, run, "s1"); step(c, run, "s2")
    assert state(c, run)["active_advisory"] is not None


def test_stale_revision_and_expiry(api, monkeypatch):
    c = api
    run = new_run(c)
    r1 = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": "q1",
                "ev_sessions": [{"ev_id": "EV_1", "requested_model_kwh": 8}]}).json()["proposal"]
    r2 = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": "q2",
                "ev_sessions": [{"ev_id": "EV_1", "requested_model_kwh": 9}]}).json()["proposal"]
    assert accept(c, run, r1, "a-old").status_code == 409  # superseded
    bad = dict(r2, revision=r2["revision"] - 1)
    assert accept(c, run, bad, "a-badrev").status_code == 409
    other = new_run(c, apartment_id="APT_1")
    resp = c.post(f"/api/v1/proposals/{r2['proposal_id']}/decision", headers=c.mgr, json={
        "run_id": other, "expected_revision": r2["revision"], "decision": "ACCEPT", "option_id": "o1", "request_id": "wr"})
    assert resp.status_code == 409
    from backend import db
    with db.LOCK:
        db.ex("UPDATE proposals SET expires_at='2000-01-01T00:00:00Z' WHERE proposal_id=?", r2["proposal_id"])
    assert accept(c, run, r2, "a-exp").status_code == 409
    assert state(c, run)["active_commitment"] is None


def test_impossible_requirement_shows_shortfall(api):
    c = api
    run = new_run(c)
    p = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": "q",
               "ev_sessions": [{"ev_id": "EV_1", "requested_model_kwh": 500}]}).json()["proposal"]
    assert p["options"][0]["ev_shortfall_model_kwh"]["EV_1"] > 0
    assert any(x["kind"] == "EV_ENERGY" for x in p["infeasibilities"])


def test_pump_power_without_water_is_deviation(api):
    c = api
    run = new_run(c, source_outage_intervals=list(range(96)), tank_init_l=3100)
    p = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": "q", "ev_sessions": []}).json()["proposal"]
    # planner does not know the (unannounced) outage, so it plans pumping
    assert accept(c, run, p, "a").status_code == 200
    acts = state(c, run)["active_commitment"]["option"]["actions"]
    k = next(a["interval_index"] for a in acts if a["pump"] == "ON")
    for j in range(k):
        step(c, run, f"s{j}", actual_switches={"pump": "OFF", "ev": "OFF"})
    out = step(c, run, "sk", actual_switches={"pump": "ON", "ev": "OFF"})
    assert "PUMP_POWER_WITHOUT_WATER_DELIVERY" in out["execution"]["reason"]


def test_stale_observation_is_not_zero(api):
    c = api
    run = new_run(c)
    step(c, run, "s0")
    out = step(c, run, "s1", drop_observation=True)
    s = out["state"]
    assert s["observation"]["quality"] == "STALE" and s["observation"]["aggregate_model_kw"] is None
    assert s["apartment"]["pump_state"] == "UNKNOWN"
    assert any("STALE" in w for w in s["warnings"])


def test_telemetry_duplicate_order_reboot(api):
    c = api
    run = new_run(c)
    base = {"run_id": run, "apartment_id": "APT_1", "device_id": "bench-1", "source_kind": "hardware",
            "voltage_v": 12.0, "pump_power_w": None, "device_monotonic_ms": 1000}
    post = lambda **kw: c.post("/api/v1/telemetry", headers=c.dev, json={**base, **kw}).json()["status"]
    assert post(device_session_id="b1", seq_num=5) == "ACCEPTED"
    assert post(device_session_id="b1", seq_num=5) == "DUPLICATE"
    assert post(device_session_id="b1", seq_num=3) == "OUT_OF_ORDER"
    assert post(device_session_id="b2", seq_num=0) == "NEW_DEVICE_SESSION"
    assert c.post("/api/v1/telemetry", headers=c.mgr, json=base).status_code == 403
    raw = state(c, run)
    assert raw["observation"] is None  # telemetry alone does not fabricate a model observation


# ---------- pure components ----------
def test_plant_mass_balance_and_bounds():
    p = plant.make_params({"tank_init_l": 9900})
    t = plant.initial_truth(p)
    total_in = total_out = 0
    for i in range(96):
        t, rec = plant.step(t, p, i, pump_on=i % 3 == 0, ev_on=True)
        assert 0 <= rec["tank_l"] <= p["tank_max_l"]
        total_in += rec["inflow_l"]
        total_out += rec["withdrawal_l"]
    assert abs(9900 + total_in - t["overflow_l"] - (total_out - t["unmet_water_l"]) - t["tank_l"]) < 1e-6
    assert t["ev_delivered_model_kwh"]["EV_1"] <= 10 + 1e-9


def test_ev_stops_at_departure():
    p = plant.make_params({"ev_sessions": [{"ev_id": "E", "arrival_interval": 0, "departure_interval": 2, "requested_model_kwh": 50}]})
    t = plant.initial_truth(p)
    for i in range(5):
        t, _ = plant.step(t, p, i, False, True)
    assert abs(t["ev_delivered_model_kwh"]["E"] - 2 * 3.3 * 0.25) < 1e-9


def test_scheduler_respects_allowance_and_reserve():
    p = plant.make_params()
    cons = {k: p[k] for k in ("horizon_intervals", "model_dt_minutes", "tank_max_l", "tank_min_l", "pump_model_kw",
                              "ev_charger_model_kw", "pump_inflow_lph", "allowance_model_kw")}
    cons["expected_withdrawal_l"] = p["withdrawal_profile_l"]
    out = rules.plan({"interval_index": 0, "tank_l": 4500, "pump_on": False},
                     {"peak_intervals": list(range(40, 52))}, {"ev_sessions": [{**p["ev_sessions"][0], "remaining_model_kwh": 10}]}, cons)
    o = out["options"][0]
    assert o["predicted_peak_model_kw"] <= p["allowance_model_kw"] + 1e-9
    assert o["water_shortfall"]["intervals_below_reserve"] == 0 and o["ev_shortfall_model_kwh"]["EV_1"] == 0
    assert all(0 <= v <= p["tank_max_l"] for v in o["water_trajectory_model_l"])
    assert o["predicted_peak_window_energy_model_kwh"] == 0  # enough off-peak room here


# ---------- forecasting ----------
@pytest.mark.skipif(not HAS_DATA, reason="run scripts/fetch_kptcl.py")
def test_forecast_uses_no_future_data():
    s, rep = data.load_validated()
    a = evaluate.build_snapshot(s, rep, "ridge_lag_calendar", FS)
    s2 = s.copy()
    s2[s2.index >= pd.Timestamp(FS.replace("Z", "")) - pd.Timedelta(hours=24)] = 99999.0  # poison data after cutoff
    b = evaluate.build_snapshot(s2, rep, "ridge_lag_calendar", FS)
    assert [p["predicted_mw"] for p in a["points"]] == [p["predicted_mw"] for p in b["points"]]
    assert len(a["points"]) == 24 and len(a["peak_windows"]) == 3
    assert "Karnataka" in a["geographic_scope"] and "BESCOM" in a["geographic_scope"]  # explicitly 'Not BESCOM'


@pytest.mark.skipif(not HAS_DATA, reason="run scripts/fetch_kptcl.py")
def test_forecast_replay_api_persists_immutable_snapshot(api):
    c = api
    body = {"request_id": "f1", "dataset_id": "kptcl-load-curve", "model_name": "seasonal_naive", "forecast_start": FS}
    assert c.post("/api/v1/forecasts/replay", headers=c.mgr, json=body).status_code == 403
    f1 = c.post("/api/v1/forecasts/replay", headers=c.op, json=body).json()
    assert f1["model_kind"] == "seasonal_naive" and f1["source_kind"] == "historical_replay"
    assert c.post("/api/v1/forecasts/replay", headers=c.op, json=body).json() == f1  # idempotent
    assert c.get(f"/api/v1/forecasts/{f1['forecast_id']}", headers=c.mgr).json() == f1
    assert c.post("/api/v1/forecasts/replay", headers=c.op, json={**body, "request_id": "f2", "model_name": "lstm"}).status_code == 422
    assert c.post("/api/v1/forecasts/replay", headers=c.op, json={**body, "request_id": "f3",
                  "forecast_start": "2026-08-18T19:00:00Z"}).status_code == 422


def test_top3_tie_breaks_earliest():
    idx, _ = models.top3(np.array([5.0] * 24), pd.Timestamp("2026-01-01 18:30"))
    assert idx == [0, 1, 2]


# ---------- comparison experiment ----------
def test_experiment_fairness_properties():
    from simulation import experiment
    rows, summary, _ = experiment.run(n_seeds=5)
    pm = lambda s, m: summary[s]["paired_measured_minus_ack"][m]
    # perfect truthful execution: measured and acknowledgement replanning tie
    assert all(v["ties"] == v["measured_better_runs"] + v["baseline_better_runs"] + v["ties"]
               for v in summary["perfect"]["paired_measured_minus_ack"].values())
    # pump misses are visible to both through the same tank observations
    assert pm("missed_pump_false_done", "reserve_violation_intervals")["ties"] == 5
    # aggregate-only metering never beats branch metering on EV shortfall (it can only see less)
    assert summary["missed_ev_overlapping_pump"]["paired_branch_minus_aggregate"]["ev_shortfall_model_kwh"]["baseline_better_runs"] == 0
    # false done on EV: acknowledgement never beats measurement on EV shortfall
    assert pm("missed_ev_false_done", "ev_shortfall_model_kwh")["baseline_better_runs"] == 0
    # honest reporting: acknowledgement already knows about the miss, so measurement adds nothing
    assert pm("honest_misses", "ev_shortfall_model_kwh")["ties"] == 5
    # imperfect sensing has a visible cost: replans that follow no missed action are counted, not hidden
    assert any(r["replans_without_missed_action"] > 0 for r in rows if r["scenario"] == "noisy_sensors_mixed_reports")
    # infeasible request: shortfall visible for every policy, no fake completion
    assert all(r["ev_shortfall_model_kwh"] > 0 for r in rows if r["scenario"] == "infeasible_ev")
    # physical bounds in every run
    assert all(0 <= r["final_tank_l"] <= 10000 for r in rows)


def test_experiment_api(api):
    c = api
    body = {"request_id": "e1", "scenarios": ["perfect", "missed_ev_false_done"], "seeds_per_scenario": 3}
    assert c.post("/api/v1/experiments", headers=c.mgr, json=body).status_code == 403
    r = c.post("/api/v1/experiments", headers=c.op, json=body).json()
    doc = c.get(f"/api/v1/experiments/{r['experiment_id']}", headers=c.op).json()
    assert doc["status"] == "COMPLETED" and doc["source_kind"] == "synthetic_simulation" and len(doc["runs"]) == r["runs"] == 24
    assert c.post("/api/v1/experiments", headers=c.op, json={**body, "request_id": "e2", "policies": ["magic"]}).status_code == 422


# ---------- sensing scope and freshness (SMART_METER_EVIDENCE cases A-D) ----------
def accepted_run(c, **params):
    run = new_run(c, **params)
    p = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": f"q-{run}", "ev_sessions": []}).json()["proposal"]
    assert accept(c, run, p, f"a-{run}").status_code == 200
    return run, state(c, run)["active_commitment"]["option"]["actions"]


def test_aggregate_only_is_ambiguous_and_gives_no_ev_credit(api):
    c = api
    run, acts = accepted_run(c, observation_scope="aggregate")
    a0 = acts[0]
    assert a0["ev"] != "OFF"  # default EV is present from interval 0
    out = step(c, run, "s0", actual_switches={"pump": a0["pump"], "ev": "ON"})
    assert out["execution"]["observation_status"] == "AMBIGUOUS"
    s = out["state"]
    assert s["observation"]["branch_attribution"] == "AMBIGUOUS" and s["observation"]["ev_model_kw"] is None
    assert s["readiness"]["verified_ev_delivered_model_kwh"] is None
    assert s["readiness"]["verified_ev_unavailable_reason"].startswith("AGGREGATE_ONLY")
    assert s["readiness"]["charging_status"] == "UNVERIFIED"
    assert not s["pending_proposals"]  # ambiguity is not treated as a deviation
    # zero aggregate while something is instructed is a supported deviation
    out = step(c, run, "s1", actual_switches={"pump": "OFF", "ev": "OFF"})
    if acts[1]["pump"] == "ON" or acts[1]["ev"] != "OFF":
        assert out["execution"]["reason"] == "NO_PARTICIPATING_LOAD_OBSERVED"


def test_aggregate_pump_without_water_uses_tank_sensor(api):
    c = api
    run, acts = accepted_run(c, observation_scope="aggregate", source_outage_intervals=list(range(96)), tank_init_l=3100,
                             ev_sessions=[])
    k = next(a["interval_index"] for a in acts if a["pump"] == "ON")
    for j in range(k):
        step(c, run, f"s{j}", actual_switches={"pump": "OFF", "ev": "OFF"})
    out = step(c, run, "sk", actual_switches={"pump": "ON", "ev": "OFF"})
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION"
    assert out["execution"]["reason"] == "PUMP_WATER_DELIVERY_NOT_OBSERVED"


def test_late_reading_keeps_gap_then_arrives_marked_late(api):
    c = api
    run, acts = accepted_run(c)
    out = step(c, run, "s0", delay_observation=True, actual_switches={"pump": "OFF", "ev": "OFF"})
    assert out["execution"]["observation_status"] == "STALE"
    assert out["state"]["observation"]["quality"] == "STALE" and out["state"]["observation"]["aggregate_model_kw"] is None
    out = step(c, run, "s1")
    late = out["late_execution"]
    assert late["interval_index"] == 0 and "(late reading)" in late["reason"]
    from backend import db
    o0 = json.loads(db.one("SELECT body FROM observations WHERE run_id=? AND interval_index=0", run)["body"])
    assert o0["quality"] == "LATE" and o0["received_at_interval"] == 1 and o0["interval_index"] == 0
    assert c.post(f"/api/v1/runs/{run}/step", headers=c.op,
                  json={"request_id": "bad", "drop_observation": True, "delay_observation": True}).status_code == 422


def test_concurrent_authorized_reads_do_not_fail(api):
    """Regression: unlocked reads on the shared SQLite connection gave false 401/403/500 under concurrency."""
    from concurrent.futures import ThreadPoolExecutor
    c = api
    run = new_run(c)
    get = lambda _: c.get(f"/api/v1/operator/state?run_id={run}", headers=c.op).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(get, range(400)))
    assert codes.count(200) == 400, {k: codes.count(k) for k in set(codes)}


def test_sonia_followups_scoping_history_listing(api):
    c = api
    assert c.post("/api/v1/runs", headers=c.op, json={"request_id": "bad-param", "clock_start_utc": FS,
                  "apartment_params": {"observation_scop": "aggregate"}}).status_code == 422  # typo is rejected, not ignored
    run, acts = accepted_run(c, observation_scope="aggregate")
    assert all(e["remaining_model_kwh"] is None for e in state(c, run)["apartment"]["ev_sessions"])
    com = state(c, run)["active_commitment"]
    c.post("/api/v1/execution/claims", headers=c.mgr, json={"run_id": run, "commitment_id": com["commitment_id"],
           "interval_index": 0, "action_id": f"{com['commitment_id']}:0:ev", "request_id": "cl0"})
    step(c, run, "s0", delay_observation=True, actual_switches={"pump": "OFF", "ev": "OFF"})
    step(c, run, "s1")
    h = state(c, run)["execution_history"]
    first = next(x for x in h if x["interval_index"] == 0)
    assert first["claims"] and first["quality"] == "LATE" and first["received_at_interval"] == 1
    mrep = c.get(f"/api/v1/runs/{run}/report", headers=c.mgr).json()
    assert "peak_participating_model_kw" not in mrep and "ev_delivered_model_kwh" not in mrep and "counts" in mrep
    assert "peak_participating_model_kw" in c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()
    assert run in [x["run_id"] for x in c.get("/api/v1/runs", headers=c.op).json()["runs"]]
    assert c.get("/api/v1/runs", headers=c.mgr).status_code == 403


# ---------- device emulator + telemetry-driven observations ----------
def test_emulator_physics():
    from simulation.device_emulator import DeviceEmulator, load_profile
    e = DeviceEmulator(load_profile(), "r")
    e.pump_on = True
    t0 = e.tank_l
    s = e.interval_samples(10)
    assert e.tank_l > t0 + 1000 and s[-1]["pump_power_w"] > 2000 and s[-1]["source_kind"] == "synthetic"
    e.dry, t1 = True, e.tank_l
    s = e.interval_samples(10)
    assert e.tank_l < t1 and s[-1]["pump_power_w"] > 2000  # power without water
    seq = s[-1]["seq_num"]
    e.reboot()
    assert e.sample(11)["seq_num"] == 1 < seq and e.boot == 2
    assert DeviceEmulator(load_profile(), "r").boot_prefix != e.boot_prefix  # new process, new boot id


def telemetry_run(c, quiet=False, ev_kwh=None):
    """quiet: the emulator's power noise is set to 0 so energy figures are exact (deterministic thresholds)."""
    from simulation.device_emulator import DeviceEmulator, load_profile
    pf = load_profile()
    if quiet:
        pf["sensor_noise"]["power_fraction_sd"] = 0.0
    extra = {} if ev_kwh is None else {"ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32,
                                                         "requested_model_kwh": ev_kwh}]}
    run = new_run(c, observation_source="telemetry", pump_inflow_lph=pf["pump"]["flow_lpm"] * 60,
                  meter_clock="simulated", **extra)
    p = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": f"q-{run}", "ev_sessions": []}).json()["proposal"]
    assert accept(c, run, p, f"a-{run}").status_code == 200
    emu = DeviceEmulator(pf, run, device_id="bench-1", clock_start_utc=FS)
    if ev_kwh is not None:
        emu.ev_requested_kwh = ev_kwh
    return run, emu


def send(c, emu, i, skip=False, dup=False):
    sent = [] if skip else emu.interval_samples(i)
    for s in sent:
        assert c.post("/api/v1/telemetry", headers=c.dev, json=s).status_code == 200
    if dup and sent:
        assert c.post("/api/v1/telemetry", headers=c.dev, json=sent[-1]).json()["status"] == "DUPLICATE"
    return step(c, emu.run_id, f"t{i}-{emu.seq}-{emu.boot}")


def test_telemetry_run_detects_false_done_and_dry_pump(api):
    c = api
    # Noise-free emulator and a 10.5 kWh request: a one-interval miss leaves a 600 Wh predicted shortfall, well above
    # the revision gate's 100 Wh rule, so a revision must be proposed (see test_revision_materiality_threshold).
    run, emu = telemetry_run(c, quiet=True, ev_kwh=10.5)
    assert c.post(f"/api/v1/runs/{run}/step", headers=c.op,
                  json={"request_id": "x", "actual_switches": {"pump": "ON"}}).status_code == 422
    assert state(c, run)["observation_source"] == "telemetry"
    assert c.get(f"/api/v1/operator/state?run_id={run}", headers=c.op).json()["observation_source"] == "telemetry"
    assert next(x for x in c.get("/api/v1/runs", headers=c.op).json()["runs"] if x["run_id"] == run)["observation_source"] == "telemetry"
    acts = state(c, run)["active_commitment"]["option"]["actions"]
    # interval 0: follow plan exactly -> measured match from device readings
    emu.pump_on, emu.ev_on = acts[0]["pump"] == "ON", acts[0]["ev"] != "OFF"
    out = send(c, emu, 0, dup=True)
    assert out["execution"]["observation_status"] == "OBSERVED_MATCH", out["execution"]
    o = out["state"]["observation"]
    assert o["source_kind"] == "synthetic" and o["measurement_scope"] == "branch"
    rb = o["raw_bench"]
    assert rb["sample_count"] == 15 and rb["excluded_samples"] == 0 and abs(rb["duration_s"] - 14) < 1e-9  # duplicate never stored
    if acts[0]["ev"] != "OFF":
        assert abs(rb["ev_load_wh"] - rb["mean_ev_load_w"] * 14 / 3600) < 0.01  # Wh from actual device seconds
        assert o["ev_id"] == "EV_1"
    # interval 1: manager reports done, person left everything off -> contradicted, revision proposed
    com = state(c, run)["active_commitment"]
    a1 = acts[1]
    kind = "pump" if a1["pump"] == "ON" else "ev"
    c.post("/api/v1/execution/claims", headers=c.mgr, json={"run_id": run, "commitment_id": com["commitment_id"],
           "interval_index": 1, "action_id": f"{com['commitment_id']}:1:{kind}", "request_id": "tc1"})
    emu.pump_on = emu.ev_on = False
    out = send(c, emu, 1)
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION"
    assert out["state"]["pending_proposals"][0]["reason"] == "CLAIM_WITHOUT_OBSERVED_EXECUTION"
    # interval 2: readings lost -> STALE, never zero
    out = send(c, emu, 2, skip=True)
    assert out["execution"]["observation_status"] == "STALE" and out["state"]["observation"]["aggregate_model_kw"] is None
    # interval 3: device reboots; readings from the new session are used, no elapsed time across sessions
    emu.reboot()
    out = send(c, emu, 3)
    assert out["state"]["observation"]["raw_bench"]["device_sessions"] == [f"{emu.boot_prefix}-2"]
    rep = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()
    assert rep["provenance"]["plant"].startswith("device telemetry from the emulator") and "measured by" not in rep["provenance"]["plant"]
    assert rep["unmet_withdrawal_l"] is None
    assert rep["observation_coverage"]["valid"] == 3 and rep["observation_coverage"]["intervals_elapsed"] == 4


def test_telemetry_report_before_first_reading_is_unknown(api):
    c = api
    run, _ = telemetry_run(c)
    rep = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()
    assert rep["unmet_withdrawal_l"] is None and "synthetic" in rep["provenance"]["plant"]


def test_telemetry_wrong_apartment_rejected(api):
    c = api
    run, emu = telemetry_run(c)
    s = emu.sample(0)
    s["apartment_id"] = "APT_2"
    assert c.post("/api/v1/telemetry", headers=c.dev, json=s).status_code == 422


def test_telemetry_dry_pump_is_deviation(api):
    c = api
    run, emu = telemetry_run(c)
    acts = state(c, run)["active_commitment"]["option"]["actions"]
    k = next(a["interval_index"] for a in acts if a["pump"] == "ON")
    for j in range(k):
        emu.pump_on, emu.ev_on = acts[j]["pump"] == "ON", acts[j]["ev"] != "OFF"
        send(c, emu, j)
    emu.pump_on, emu.ev_on, emu.dry = True, acts[k]["ev"] != "OFF", True
    out = send(c, emu, k)
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION"
    assert out["execution"]["reason"] == "PUMP_POWER_WITHOUT_WATER_DELIVERY"


def _plan(overrides=None, tank=None, pump_on=False):
    from simulation import experiment
    p = plant.make_params(overrides or {})
    return rules.plan({"interval_index": 0, "tank_l": p["tank_init_l"] if tank is None else tank, "pump_on": pump_on},
                      {"peak_intervals": list(range(40, 52))},
                      {"ev_sessions": [{**s, "remaining_model_kwh": s["requested_model_kwh"]} for s in p["ev_sessions"]]},
                      experiment.constraints(p))["options"][0], p


def test_scheduler_plans_hour_long_pump_runs_with_reserve_margin():
    o, p = _plan()
    on = [a["interval_index"] for a in o["actions"] if a["pump"] == "ON"]
    runs, cur = [], []
    for i in on:
        if cur and i != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(i)
    runs.append(cur)
    assert o["pump_starts"]["planned"] == len(runs) <= 3  # was 8 single-interval starts
    assert all(len(r) >= rules.MIN_RUN for r in runs)
    assert o["predicted_min_tank_model_l"] > p["tank_min_l"]  # low point above the reserve, not on it
    assert o["water_shortfall"]["intervals_below_reserve"] == 0 and o["ev_shortfall_model_kwh"]["EV_1"] == 0
    assert o["predicted_peak_window_energy_model_kwh"] == 0 and o["predicted_peak_model_kw"] <= p["allowance_model_kw"]


def test_scheduler_runs_never_overflow_and_fall_back_near_full():
    o, p = _plan(tank=9800)  # nearly full: a 1-hour run would overflow, so planning must not
    assert max(o["water_trajectory_model_l"]) <= p["tank_max_l"]
    assert o["water_shortfall"]["intervals_below_reserve"] == 0
    o2, _ = _plan(tank=3050, pump_on=True)  # pump running now: extending it is preferred over a later start
    assert o2["actions"][0]["pump"] == "ON" and o2["pump_starts"]["planned"] <= 3


# ---------- pump duration rules (demonstration assumptions) ----------
def test_plant_restart_lockout_short_runs_and_safety_precedence():
    p = plant.make_params({"tank_init_l": 5000, "source_outage_intervals": [2], "pump_dry_run_protection": True})
    t = plant.initial_truth(p)
    t, r0 = plant.step(t, p, 0, True, False)
    t, r1 = plant.step(t, p, 1, True, False)
    t, r2 = plant.step(t, p, 2, True, False)  # outage with protection: safety stop although the minimum run is not met
    assert r2["pump_model_kw"] == 0 and r2["pump_safety_stop"] == "NO_SOURCE_WATER" and r2["inflow_l"] == 0
    t, r3 = plant.step(t, p, 3, True, False)  # the safety stop started the off-timer: restart is locked out
    assert r3["pump_model_kw"] == 0 and r3["pump_restart_lockout"]
    t, r4 = plant.step(t, p, 4, True, False)
    assert r4["pump_model_kw"] > 0
    t, r5 = plant.step(t, p, 5, False, False)  # manual OFF after one interval is honoured, counted as short
    assert r5["pump_model_kw"] == 0 and r5["pump_short_run_stop"]
    full = plant.make_params({"tank_init_l": 10000})
    _, rf = plant.step(plant.initial_truth(full), full, 0, True, False)
    assert rf["pump_model_kw"] == 0 and rf["pump_safety_stop"] == "TANK_FULL_CUTOFF"
    unprot = plant.make_params({"source_outage_intervals": [0]})  # no protection: pump runs dry, draws power
    _, ru = plant.step(plant.initial_truth(unprot), unprot, 0, True, False)
    assert ru["pump_model_kw"] > 0 and ru["inflow_l"] == 0 and ru["pump_safety_stop"] is None


def test_scheduler_respects_min_on_min_off_and_safety():
    o, p = _plan()
    on = [a["interval_index"] for a in o["actions"] if a["pump"] == "ON"]
    runs = [[on[0]]]
    for i in on[1:]:
        runs[-1].append(i) if i == runs[-1][-1] + 1 else runs.append([i])
    assert all(len(r) >= p["pump_min_on_intervals"] for r in runs) and not o["pump_duration_exceptions"]
    assert all(b[0] - a[-1] - 1 >= p["pump_min_off_intervals"] for a, b in zip(runs, runs[1:]))
    assert o["pump_duration_rules"]["basis"].startswith("demonstration")
    from simulation import experiment
    cons = experiment.constraints(p)
    reqs = {"ev_sessions": []}
    # pump observed running for 1 interval: it stays planned ON to the minimum run...
    run = rules.plan({"interval_index": 10, "tank_l": 4000, "pump_on": True, "pump_run_intervals": 1}, {}, reqs, cons)
    assert [a["pump"] for a in run["options"][0]["actions"][:3]] == ["ON"] * 3
    # ...unless a known outage stops it (safety takes precedence over the minimum run)
    cut = rules.plan({"interval_index": 10, "tank_l": 4000, "pump_on": True, "pump_run_intervals": 1},
                     {"source_water_available": {11: False}}, reqs, cons)
    assert [a["pump"] for a in cut["options"][0]["actions"][:2]] == ["ON", "OFF"]
    # pump stopped one interval ago: no start inside the minimum off time
    lock = rules.plan({"interval_index": 10, "tank_l": 3010, "pump_on": False, "pump_off_intervals": 1}, {}, reqs, cons)
    assert lock["options"][0]["actions"][0]["pump"] == "OFF"


def test_judge_scenarios_pass(tmp_path):
    import os
    import subprocess
    import sys
    env = {**os.environ, "NIVAWATT_JUDGE_OUT": str(tmp_path)}
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run([sys.executable, "scripts/judge_scenarios.py"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    res = json.loads((tmp_path / "summary.json").read_text())["scenarios"]
    assert {s["scenario"] for s in res} >= {"normal", "false_done_pump_left_off", "delayed_pump_start", "missing_readings",
                                            "outage_during_run_protected", "outage_during_run_unprotected"}


# ---------- revision gate and readiness margin ----------
def _gate_case():
    from simulation import experiment
    p = plant.make_params({"reserve_margin_l": 0})
    c = experiment.constraints(p)
    reqs = {"ev_sessions": [{**s, "remaining_model_kwh": s["requested_model_kwh"]} for s in p["ev_sessions"]]}
    st = {"interval_index": 0, "tank_l": p["tank_init_l"], "pump_on": False}
    base = rules.plan(st, {}, reqs, c)["options"][0]["actions"]
    return p, c, reqs, st, base


def test_revision_gate_decisions():
    from scheduler import revision
    p, c, reqs, st, base = _gate_case()
    assert revision.assess(base, base, st, c, reqs, {}, 0)["decision"] == "SUPPRESSED_EQUIVALENT"
    far = [dict(a) for a in base]
    far[60] = {**far[60], "pump": "OFF" if far[60]["pump"] == "ON" else "ON"}  # change only 15 h ahead
    assert revision.assess(base, far, st, c, reqs, {}, None)["decision"] in ("SUPPRESSED_NOT_ACTIONABLE",)
    near = [dict(a) for a in base]
    near[2] = {**near[2], "pump": "ON"}  # actionable, no service gain, inside cooldown
    assert revision.assess(base, near, st, c, reqs, {}, 0)["decision"] == "DEFERRED_COOLDOWN"
    # active plan with no pumping breaches the reserve soon: the fix is urgent even inside the cooldown
    low = {**st, "tank_l": 3300}
    dry = [{**a, "pump": "OFF"} for a in base]
    fix = rules.plan(low, {}, reqs, c)["options"][0]["actions"]
    g = revision.assess(dry, fix, low, c, reqs, {}, 0)
    assert g["decision"] == "PROPOSE" and g["urgent"]
    # EV energy that would be missed is urgent too
    no_ev = [{**a, "ev": "OFF"} for a in base]
    assert revision.assess(no_ev, base, st, c, reqs, {}, 0)["urgent"]


def test_missing_readings_alone_do_not_create_acceptance_burden():
    from simulation import experiment
    rows, _, _ = experiment.run(scenarios=["stale_telemetry"], policies=["ack_replan", "measured_replan"], n_seeds=5)
    ack = [r["manager_acceptances"] for r in rows if r["policy"] == "ack_replan"]
    meas = [r["manager_acceptances"] for r in rows if r["policy"] == "measured_replan"]
    assert max(ack) == 1, ack  # acknowledgement-only: missing readings alone never ask again
    assert sum(meas) / len(meas) <= 6, meas  # was about 48 per day before the evidence and revision gates
    assert all(r["unverified_intervals"] > 0 for r in rows)  # uncertainty stays visible


def test_readiness_margin_and_constraint_report():
    o0, p = _plan({"reserve_margin_l": 0})
    o1, _ = _plan({"reserve_margin_l": 1000})
    assert o1["predicted_min_tank_model_l"] >= p["tank_min_l"] + 1000 > o0["predicted_min_tank_model_l"]
    assert sum(a["pump"] == "ON" for a in o1["actions"]) == sum(a["pump"] == "ON" for a in o0["actions"])
    classes = [r["class"] for r in o1["constraint_report"]]
    assert classes == ["physical_safety", "equipment_limits", "allowance", "service_water_critical",
                       "service_water_reserve", "service_ev_deadline", "advisory_windows", "readiness_margin"]
    o2, _ = _plan({"reserve_margin_l": 1000, "tank_init_l": 3200})  # cannot reach the margin early: reported
    rm = next(r for r in o2["constraint_report"] if r["class"] == "readiness_margin")
    assert rm["status"] in ("not_met", "dropped_to_protect_service") and o2["water_shortfall"]["intervals_below_reserve"] == 0


def test_allowance_binding_rescheduling_and_operator_intervention():
    ev_day = {"ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 36, "departure_interval": 72, "requested_model_kwh": 10}]}
    o, _ = _plan({**ev_day, "allowance_model_kw": 4.0})  # conflict resolved by rescheduling within the cap
    al = next(r for r in o["constraint_report"] if r["class"] == "allowance")
    assert al["status"] == "binding_resolved_by_rescheduling" and o["operator_intervention"] is None
    assert o["predicted_peak_model_kw"] <= 4.0 and o["ev_shortfall_model_kwh"]["EV_1"] == 0
    o, _ = _plan({"allowance_model_kw": 3.0})  # charger 3.3 kW can never run under a 3 kW cap
    oi = o["operator_intervention"]
    assert o["predicted_peak_model_kw"] <= 3.0  # the plan never exceeds the cap
    assert oi["reason"] == "ALLOWANCE_BLOCKS_SERVICE" and oi["binding_constraint"] == "allowance"
    assert oi["allowance_that_would_reduce_shortfall_model_kw"] == 3.3
    assert not o["requires_operator_exception"]  # contract meaning: the option itself never exceeds the cap
    assert oi["shortfall_within_allowance"]["ev_shortfall_model_kwh"] == 10 and "NOT VERIFIED" in oi["physical_check"]
    o, _ = _plan({"allowance_model_kw": 3.0, "verified_electrical_limit_model_kw": 3.0})  # never above a verified limit
    assert o["operator_intervention"]["reason"] == "SERVICE_INFEASIBLE_WITHIN_LIMITS"
    assert o["operator_intervention"]["allowance_that_would_reduce_shortfall_model_kw"] is None
    o, _ = _plan({"ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32,
                                   "requested_model_kwh": 40}]})  # not fixable by any allowance
    assert o["operator_intervention"]["reason"] == "SERVICE_INFEASIBLE_WITHIN_LIMITS" and not o["requires_operator_exception"]


def _false_done_first_ev(c, kwh, tag):
    ev = [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32, "requested_model_kwh": kwh}]
    run = new_run(c, ev_sessions=ev)
    prop = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": f"q-{tag}",
                                                               "ev_sessions": []}).json()["proposal"]
    com = accept(c, run, prop, f"a-{tag}").json()["active_commitment"]
    assert com["option"]["actions"][0]["ev"] == "EV_1"
    c.post("/api/v1/execution/claims", headers=c.mgr, json={"run_id": run, "commitment_id": com["commitment_id"],
           "interval_index": 0, "action_id": f"{com['commitment_id']}:0:ev", "request_id": f"cl-{tag}"})
    out = step(c, run, f"s-{tag}", actual_switches={"pump": "OFF", "ev": "OFF"})
    return run, com, out


def test_discrepancy_states_minor_miss_vs_material_miss(api):
    c = api
    # A: 9.95 kWh -> plan books 12 full + 1 partial interval; one missed interval leaves ~50 Wh predicted shortfall
    run, com, out = _false_done_first_ev(c, 9.95, "A")
    ds = out["state"]["discrepancy_status"]
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION"
    assert ds["execution_discrepancy"]["detected"] and ds["execution_discrepancy"]["reason"] == "EV_CHARGING_NOT_OBSERVED"
    assert not out["state"]["pending_proposals"] and ds["revision"]["status"] in ("DEFERRED", "NOT_NEEDED")
    assert ds["revision"]["explanation"]  # the UI can say why no acceptance is requested
    assert out["state"]["active_commitment"]["commitment_id"] == com["commitment_id"]
    # B: 10 kWh -> the same miss leaves exactly 100 Wh predicted shortfall: material under the whole-Wh rule
    run, com, out = _false_done_first_ev(c, 10, "B")
    ds = out["state"]["discrepancy_status"]
    assert ds["execution_discrepancy"]["detected"] and ds["revision"]["status"] == "AWAITING_MANAGER_ACCEPTANCE"
    assert ds["revision"]["current_plan_predicted"]["ev_shortfall_model_kwh"] == 0.1
    assert out["state"]["active_commitment"]["commitment_id"] == com["commitment_id"]  # not active until accepted


@pytest.mark.parametrize("kwh,gain_wh,proposed", [(9.95, 50, False), (10.0, 100, True), (10.5, 600, True)])
def test_revision_materiality_threshold(api, kwh, gain_wh, proposed):
    """Deterministic (simulator, no noise): one missed EV interval; the gate proposes only at >= 100 Wh predicted gain.
    The discrepancy is detected and recorded on both sides of the threshold."""
    c = api
    run = new_run(c, ev_sessions=[{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32, "requested_model_kwh": kwh}])
    p = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": f"q-{kwh}", "ev_sessions": []}).json()["proposal"]
    assert accept(c, run, p, f"a-{kwh}").status_code == 200
    acts = state(c, run)["active_commitment"]["option"]["actions"]
    assert acts[3]["ev"] != "OFF"
    for i in range(4):
        a = acts[i]
        ev = "OFF" if i == 3 else ("ON" if a["ev"] != "OFF" else "OFF")
        out = c.post(f"/api/v1/runs/{run}/step", headers=c.op,
                     json={"request_id": f"s-{kwh}-{i}", "actual_switches": {"pump": a["pump"], "ev": ev}}).json()
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION" and out["execution"]["reason"] == "EV_CHARGING_NOT_OBSERVED"
    s = out["state"]
    d = s["discrepancy_status"]["execution_discrepancy"]
    assert d["detected"] and d["interval_index"] == 3
    g = s["revision_gate"]
    gain = round(g["active_prediction"]["ev_shortfall_model_kwh"] * 1000) - round(g["candidate_prediction"]["ev_shortfall_model_kwh"] * 1000)
    assert gain == gain_wh
    if proposed:
        assert g["decision"] == "PROPOSE" and [x["reason"] for x in s["pending_proposals"]] == ["OBSERVED_DEVIATION"]
        assert s["discrepancy_status"]["revision"]["status"] == "AWAITING_MANAGER_ACCEPTANCE"
    else:
        assert g["decision"] != "PROPOSE" and not s["pending_proposals"]
        assert s["discrepancy_status"]["revision"]["status"] in ("DEFERRED", "NOT_NEEDED")
        assert s["discrepancy_status"]["revision"]["explanation"]
