"""Campus demo (four separate runs, one apartment each): manager codes, isolation, resumable setup, no double steps."""
import json

from tests.core.test_core import FS, api, synthetic_forecast  # noqa: F401  (api is a fixture)


def test_manager_codes_added_for_campus_apartments_and_existing_codes_kept(tmp_path, monkeypatch):
    p = tmp_path / "codes.json"
    p.write_text(json.dumps({"operator_code": "op", "manager_codes": {"APT_1": "keep"}, "device_tokens": {}}))
    monkeypatch.setenv("NIVAWATT_CODES", str(p))
    from backend import main
    assert main.ensure_manager_codes() == ["APT_2", "APT_3", "APT_4"]
    c = json.loads(p.read_text())["manager_codes"]
    assert c["APT_1"] == "keep" and len({c[a] for a in main.CAMPUS_APARTMENTS}) == 4
    assert main.ensure_manager_codes() == []
    assert json.loads(p.read_text())["manager_codes"] == c


def campus(c):
    """Two campus runs created the way the campus page does (stable request IDs, one apartment per run)."""
    f = synthetic_forecast(c)
    runs = {}
    for apt in ("APT_1", "APT_2"):
        body = {"request_id": f"g1-{apt}-run", "clock_start_utc": FS,
                "apartment_params": {"apartment_id": apt, "tank_init_l": 5000}}
        runs[apt] = c.post("/api/v1/runs", headers=c.op, json=body).json()["run_id"]
        r = c.post("/api/v1/events", headers=c.op, json={
            "request_id": f"g1-{apt}-advisory", "run_id": runs[apt], "kind": "peak_advisory", "known_from_interval": 0,
            "forecast_id": f["forecast_id"], "target_apartments": ["APT_1", "APT_2", "APT_3", "APT_4"],
            "peak_windows": f["peak_windows"], "valid_from": FS, "valid_until": "2026-08-19T18:30:00Z"})
        assert r.status_code == 200, r.text
    return runs


def test_resumed_setup_returns_the_same_runs(api):
    runs = campus(api)
    again = api.post("/api/v1/runs", headers=api.op, json={"request_id": "g1-APT_2-run", "clock_start_utc": FS,
                                                           "apartment_params": {"apartment_id": "APT_2", "tank_init_l": 5000}})
    assert again.json()["run_id"] == runs["APT_2"]
    assert len([r for r in api.get("/api/v1/runs", headers=api.op).json()["runs"]]) == 2


def test_manager_sees_and_acts_only_on_their_own_building(api):
    runs = campus(api)
    own = api.get(f"/api/v1/state?run_id={runs['APT_1']}&apartment_id=APT_1", headers=api.mgr)
    assert own.status_code == 200
    # a link edited to another building's run, with either apartment ID
    assert api.get(f"/api/v1/state?run_id={runs['APT_2']}&apartment_id=APT_1", headers=api.mgr).status_code == 404
    assert api.get(f"/api/v1/state?run_id={runs['APT_2']}&apartment_id=APT_2", headers=api.mgr).status_code == 403
    st2 = api.get(f"/api/v1/state?run_id={runs['APT_2']}&apartment_id=APT_2", headers=api.mgr2).json()
    p = st2["pending_proposals"][0]
    r = api.post(f"/api/v1/proposals/{p['proposal_id']}/decision", headers=api.mgr, json={
        "request_id": "x-dec", "run_id": runs["APT_2"], "expected_revision": p["revision"],
        "option_id": p["options"][0]["option_id"], "decision": "ACCEPT"})
    assert r.status_code == 403
    ok = api.post(f"/api/v1/proposals/{p['proposal_id']}/decision", headers=api.mgr2, json={
        "request_id": "ok-dec", "run_id": runs["APT_2"], "expected_revision": p["revision"],
        "option_id": p["options"][0]["option_id"], "decision": "ACCEPT"})
    assert ok.status_code == 200
    com = ok.json()["active_commitment"]
    r = api.post("/api/v1/execution/claims", headers=api.mgr, json={
        "request_id": "x-claim", "run_id": runs["APT_2"], "commitment_id": com["commitment_id"], "interval_index": 0,
        "action_id": f"{com['commitment_id']}:0:ev"})
    assert r.status_code == 403
    # managers cannot drive the clock
    assert api.post(f"/api/v1/runs/{runs['APT_1']}/step", headers=api.mgr, json={"request_id": "x-step"}).status_code == 403


def test_group_step_retry_with_same_request_id_does_not_double_step(api):
    runs = campus(api)
    body = {"request_id": "g1-APT_1-step-0", "actual_switches": {"pump": "OFF", "ev": "ON"}}
    a = api.post(f"/api/v1/runs/{runs['APT_1']}/step", headers=api.op, json=body).json()
    b = api.post(f"/api/v1/runs/{runs['APT_1']}/step", headers=api.op, json=body).json()
    assert a["stepped_interval"] == b["stepped_interval"] == 0
    assert api.get(f"/api/v1/state?run_id={runs['APT_1']}&apartment_id=APT_1", headers=api.op).json()["clock"]["interval_index"] == 1
    changed = api.post(f"/api/v1/runs/{runs['APT_1']}/step", headers=api.op, json={**body, "drop_observation": True})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "REQUEST_ID_REUSED"
