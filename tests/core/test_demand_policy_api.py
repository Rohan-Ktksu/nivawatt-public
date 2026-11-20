"""Demand-aware causal policy as the default for NEW runs: explicit selection overrides, documented fallback, causal
planning with sufficient history, existing runs and accepted plans unchanged, default survives a restart."""
import json

from fastapi.testclient import TestClient

from tests.core.test_core import FS, api, step  # noqa: F401  (api is a fixture)


def _run(c, rid, **body):
    r = c.post("/api/v1/runs", headers=c.op, json={"request_id": rid, "clock_start_utc": FS, **body})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _state(c, run):
    r = c.get(f"/api/v1/state?run_id={run}&apartment_id=APT_1", headers=c.op)
    assert r.status_code == 200, r.text
    return r.json()


def _plan(c, run, rid, kfi=0):
    ev = c.post("/api/v1/events", headers=c.op, json={"request_id": rid, "run_id": run, "kind": "allowance",
                                                     "allowance_model_kw": 13.0, "known_from_interval": kfi})
    assert ev.status_code == 200, ev.text
    return _state(c, run)["pending_proposals"][0]


def test_1_omitted_policy_selects_causal_demand_aware(api):
    c = api
    made = c.post("/api/v1/runs", headers=c.op, json={"request_id": "d1", "clock_start_utc": FS, "apartment_params": {}}).json()
    assert made["scheduling_policy"] == "demand_aware_causal_experimental" and made["scheduling_policy_source"] == "default"
    s = _state(c, made["run_id"])["scheduling"]
    assert s["requested_policy"] == "demand_aware_causal_experimental" and s["requested_source"] == "default"
    assert "Experimental: validated in synthetic scenarios" in s["requested_policy_label"]
    assert s["default_policy_for_new_runs"] == "demand_aware_causal_experimental"


def test_2_explicit_advisory_only_is_respected(api):
    c = api
    s = _state(c, _run(c, "a1", apartment_params={"scheduling_policy": "advisory_only"}))["scheduling"]
    assert s["requested_policy"] == "advisory_only" and s["requested_source"] == "explicit"
    assert s["effective_mode"] == "advisory_only" and "status" not in s
    p = _state(c, _run(c, "a2", scenario="flats100_advisory_only"))["scheduling"]
    assert p["requested_policy"] == "advisory_only" and p["requested_source"] == "explicit"
    bad = c.post("/api/v1/runs", headers=c.op, json={"request_id": "a3", "clock_start_utc": FS,
                                                     "apartment_params": {"scheduling_policy": "perfect_mean_benchmark"}})
    assert bad.status_code == 422  # the benchmark is research-only: no API selection


def test_3_missing_or_stale_whole_building_inputs_use_the_documented_fallback(api):
    c = api
    run = _run(c, "m1", apartment_params={})  # default policy, but no whole-building measurement on this run
    s = _state(c, run)["scheduling"]
    assert s["effective_mode"] == "fallback_advisory_only" and s["status"]["confidence"] == "unavailable"
    assert s["status"]["reasons"] == ["no whole-building measurement is configured for this run"]
    out = step(c, run, "m1-s")
    assert "whole_connection" not in out["state"]["observation"]  # nothing invented
    prop = _plan(c, run, "m1-p", 1)
    assert prop["demand_policy"]["mode"] == "fallback_advisory_only"
    stale = _run(c, "m2", scenario="flats100_demand_aware_experimental")
    step(c, stale, "m2-a", drop_whole_connection=True)
    step(c, stale, "m2-b", drop_whole_connection=True)
    st = _state(c, stale)["scheduling"]
    assert st["effective_mode"] == "fallback_advisory_only" and st["status"]["confidence"] == "reduced"
    assert any("stale" in x for x in st["status"]["reasons"])
    assert "does not guarantee avoiding demand charges" in st["fallback_note"]


def test_4_sufficient_fresh_history_enables_causal_planning(api):
    c = api
    run = _run(c, "h1", scenario="flats100_demand_aware_experimental")
    s = _state(c, run)["scheduling"]
    assert s["effective_mode"] == "demand_aware" and s["status"]["confidence"] == "normal"
    assert s["status"]["forecast"]["history_days_available"] == 7
    out = step(c, run, "h1-s", actual_switches={"pump": "ON", "ev": "OFF"})
    o = out["state"]["observation"]
    assert abs(o["whole_connection"]["average_power_model_kw"] - o["whole_connection"]["non_flexible_model_kw"]
               - o["pump_model_kw"] - o["ev_model_kw"]) < 1e-3  # incomer minus branches, never plus
    prop = _plan(c, run, "h1-p", 1)
    assert prop["demand_policy"]["mode"] == "demand_aware" and prop["demand_policy"]["target_kva"] >= 0.9 * 168
    peak = range(19 * 4, 22 * 4)  # building evening peak: no pump/EV slots planned under the causal cap
    assert all(a["pump"] == "OFF" and a["ev"] == "OFF" for a in prop["options"][0]["actions"] if a["interval_index"] in peak)


def test_5_existing_runs_and_accepted_plans_are_unchanged(api):
    c = api
    from backend import db
    old = _run(c, "o1", apartment_params={})
    row = db.one("SELECT params FROM runs WHERE run_id=?", old)
    p = json.loads(row["params"])
    p.pop("scheduling_policy")
    p.pop("policy_provenance")  # a run stored before the default changed
    db.ex("UPDATE runs SET params=? WHERE run_id=?", json.dumps(p), old)
    s = _state(c, old)["scheduling"]
    assert s["requested_policy"] == "advisory_only" and s["requested_source"] == "legacy" and "status" not in s
    prop = _plan(c, old, "o1-p")
    assert prop.get("demand_policy") is None
    acc = c.post(f"/api/v1/proposals/{prop['proposal_id']}/decision", headers=c.mgr,
                 json={"request_id": "o1-acc", "run_id": old, "decision": "ACCEPT", "option_id": prop["options"][0]["option_id"],
                       "expected_revision": prop["revision"]})
    assert acc.status_code == 200, acc.text
    before = _state(c, old)["active_commitment"]
    _state(c, old)  # reading state (which evaluates the scheduling block) never touches the accepted plan
    after = _state(c, old)["active_commitment"]
    assert before == after and before["status"] == "ACCEPTED"


def test_6_default_selection_survives_reload_and_restart(api):
    c = api
    run = _run(c, "r1", apartment_params={})
    from backend import main
    fresh = TestClient(main.app)  # a new client over the same stored data
    r = fresh.get(f"/api/v1/state?run_id={run}&apartment_id=APT_1", headers=c.op)
    s = r.json()["scheduling"]
    assert s["requested_policy"] == "demand_aware_causal_experimental" and s["provenance"]["source"] == "default"


def test_server_owns_history_and_telemetry_cannot_claim_whole_connection(api):
    c = api
    bad = c.post("/api/v1/runs", headers=c.op, json={"request_id": "x1", "clock_start_utc": FS, "apartment_params": {
        "whole_connection": {"source": "synthetic", "category": "HT-4", "contract_demand_kva": 168, "history": []}}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_FIELDS"
    tel = c.post("/api/v1/runs", headers=c.op, json={"request_id": "x2", "clock_start_utc": FS, "apartment_params": {
        "observation_source": "telemetry",
        "whole_connection": {"source": "synthetic", "category": "HT-4", "contract_demand_kva": 168, "day_index": 30}}})
    assert tel.status_code == 422 and tel.json()["error"]["code"] == "WHOLE_CONNECTION_UNAVAILABLE"
