"""Simulated multi-channel meter: conversion, grouping, missing channels, counter restarts, attribution, API wiring."""
import pytest

from simulation import meter, plant
from simulation.device_emulator import DeviceEmulator, load_profile
from tests.core.test_core import accept, api, new_run, state, step  # noqa: F401  (api is a fixture)

S, E = "2026-08-18T18:30:00Z", "2026-08-18T18:45:00Z"


def chan(rs, cid):
    return next(r for r in rs if r["channel_id"] == cid)


# ---------- meter module ----------
def test_power_to_energy_and_cumulative_register():
    st, chs = meter.new_state("m"), meter.default_channels("branch")
    r1 = meter.read_interval(st, chs, S, E, 0.25, {"pump-1": 2.2})
    r2 = meter.read_interval(st, chs, S, E, 0.25, {"pump-1": 2.2, "charger-1": 3.3})
    assert chan(r1, "pump")["interval_energy_model_kwh"] == 0.55  # 2.2 kW x 0.25 h
    assert chan(r2, "pump")["cumulative_energy_model_kwh"] == 1.1
    assert chan(r2, "ev_group")["interval_energy_model_kwh"] == 0.825
    assert chan(r1, "ev_group")["average_power_model_kw"] == 0.0 and chan(r1, "ev_group")["quality"] == "VALID"
    assert [r["sequence_number"] for r in r1 + r2] == [1, 1, 2, 2]


def test_multiple_chargers_sum_into_one_group_channel_and_total_is_not_double_counted():
    st, chs = meter.new_state(), meter.default_channels("branch")
    rs = meter.read_interval(st, chs, S, E, 0.25, {"pump-1": 2.2, "charger-1": 3.3, "charger-2": 7.4})
    ev = chan(rs, "ev_group")
    assert ev["average_power_model_kw"] == 10.7 and ev["grouped"] and ev["devices"] == ["charger-1", "charger-2"]
    tot = meter.channel_total(rs)
    assert tot["complete"] and tot["average_power_model_kw"] == 12.9  # pump + group, each device once
    assert len(rs) == 2  # the total is reported beside the channels, never appended as another channel


def test_overlapping_or_unmapped_channels_rejected():
    with pytest.raises(ValueError, match="must not overlap"):
        meter.validate_channels([{"channel_id": "pump", "devices": ["pump-1", "charger-1"]},
                                 {"channel_id": "ev_group", "devices": ["charger-1"]}], "branch")
    with pytest.raises(ValueError, match="not wired"):
        plant.make_params({"ev_sessions": [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 8,
                                            "requested_model_kwh": 2, "charger_id": "charger-9"}]})


def test_missing_channel_makes_total_incomplete_not_zero():
    st, chs = meter.new_state(), meter.default_channels("branch")
    rs = meter.read_interval(st, chs, S, E, 0.25, {"pump-1": 2.2, "charger-1": 3.3}, missing={"ev_group"})
    ev = chan(rs, "ev_group")
    assert ev["quality"] == "MISSING" and ev["average_power_model_kw"] is None and ev["interval_energy_model_kwh"] is None
    tot = meter.channel_total(rs)
    assert not tot["complete"] and tot["missing_channels"] == ["ev_group"] and tot["average_power_model_kw"] is None
    # the meter kept counting: the next reading's register includes the lost interval, its interval energy does not
    nxt = meter.read_interval(st, chs, S, E, 0.25, {"charger-1": 3.3})
    assert chan(nxt, "ev_group")["interval_energy_model_kwh"] == 0.825
    assert chan(nxt, "ev_group")["cumulative_energy_model_kwh"] == 1.65


def test_counter_restart_never_negative_or_duplicated():
    # points: (session, time_ms, counter, session_start_ms)
    # same session: increase; a repeated reading adds nothing
    assert meter.register_energy([("a", 0, 1.0, 0), ("a", 1000, 1.5, 0), ("a", 1000, 1.5, 0)])[:2] == (0.5, 1.0)
    # new session: the counter restarted at zero at its session start; energy and time since that start only
    e, secs, n = meter.register_energy([("a", 5000, 40.0, 0), ("b", 9000, 0.3, 7000), ("b", 10000, 0.5, 7000)])
    assert (round(e, 6), secs, n) == (0.5, 3.0, 1)
    # counter lower inside one session (reset): energy since the reset only, never negative
    e, secs, n = meter.register_energy([("a", 0, 9.0, 0), ("a", 1000, 0.2, 0)])
    assert e == 0.2 and secs == 1.0 and n == 1
    st, chs = meter.new_state(), meter.default_channels("branch")
    meter.read_interval(st, chs, S, E, 0.25, {"pump-1": 2.2})
    meter.restart(st)
    rs = meter.read_interval(st, chs, S, E, 0.25, {"pump-1": 2.2})
    p = chan(rs, "pump")
    assert p["meter_session"] == 2 and p["sequence_number"] == 1 and p["cumulative_energy_model_kwh"] == 0.55


# ---------- API: simulator runs ----------
def setup_run(c, **params):
    run = new_run(c, **params)
    prop = c.post("/api/v1/requirements", headers=c.mgr,
                  json={"run_id": run, "request_id": f"r-{run}", "ev_sessions": []}).json()["proposal"]
    assert accept(c, run, prop, f"a-{run}").status_code == 200
    return run


def obs_after(c, run, rid, **body):
    out = c.post(f"/api/v1/runs/{run}/step", headers=c.op, json={"request_id": rid, **body})
    assert out.status_code == 200, out.text
    return out.json()


def test_api_observation_is_built_from_meter_channels(api):
    c = api
    run = setup_run(c)
    out = obs_after(c, run, "s0", actual_switches={"pump": "ON", "ev": "ON"})
    o = out["state"]["observation"]
    m = o["meter"]
    assert m["source_kind"] == "synthetic" and "Simulated meter" in m["label"]
    assert m["power_scale"] is None  # simulator run: no device, so no device-to-model factor
    pump, ev = chan(m["channels"], "pump"), chan(m["channels"], "ev_group")
    assert pump["average_power_model_kw"] == o["pump_model_kw"] == 2.2 and pump["interval_energy_model_kwh"] == 0.55
    assert ev["average_power_model_kw"] == o["ev_model_kw"] == 3.3 and ev["grouped"]
    assert m["total"]["complete"] and o["aggregate_model_kw"] == m["total"]["average_power_model_kw"] == 5.5
    assert pump["interval_start_utc"] == S and pump["interval_end_utc"] == E
    for f in ("meter_id", "channel_id", "cumulative_energy_model_kwh", "quality", "sequence_number"):
        assert f in pump


def test_api_false_done_leaves_measured_load_off(api):
    c = api
    run = setup_run(c)
    s = state(c, run)
    a0 = s["active_commitment"]["option"]["actions"][0]
    assert a0["ev"] != "OFF"
    com = s["active_commitment"]
    c.post("/api/v1/execution/claims", headers=c.mgr, json={"run_id": run, "commitment_id": com["commitment_id"],
           "interval_index": 0, "action_id": f"{com['commitment_id']}:0:ev", "request_id": "fd"})
    out = obs_after(c, run, "s0", actual_switches={"pump": a0["pump"], "ev": "OFF"})
    ev = chan(out["state"]["observation"]["meter"]["channels"], "ev_group")
    assert ev["quality"] == "VALID" and ev["average_power_model_kw"] == 0.0 and ev["interval_energy_model_kwh"] == 0.0
    assert out["execution"]["observation_status"] == "OBSERVED_DEVIATION"
    assert "EV_CHARGING_NOT_OBSERVED" in out["execution"]["reason"]
    assert state(c, run)["execution"]["claims"]  # the claim is kept beside the contradicting reading


def test_api_missing_channel_is_incomplete_and_unknown(api):
    c = api
    run = setup_run(c)
    out = obs_after(c, run, "s0", drop_meter_channels=["ev_group"])
    o = out["state"]["observation"]
    assert o["ev_model_kw"] is None and o["aggregate_model_kw"] is None and o["pump_model_kw"] is not None
    assert o["meter"]["total"] == {**o["meter"]["total"], "complete": False, "missing_channels": ["ev_group"]}
    assert out["execution"]["observation_status"] == "AMBIGUOUS" and "EV_CHANNEL_MISSING" in out["execution"]["reason"]
    assert not out["state"]["pending_proposals"]  # missing data alone proposes nothing
    ee = state(c, run)["readiness"]["ev_energy"]
    assert ee["allocated_by_ev_model_kwh"]["EV_1"] == 0 and ee["intervals_ev_channel_missing"] == 1  # nothing credited
    bad = c.post(f"/api/v1/runs/{run}/step", headers=c.op, json={"request_id": "s1", "drop_meter_channels": ["x"]})
    assert bad.status_code == 422


def test_api_meter_restart_restarts_counters_without_negative_energy(api):
    c = api
    run = setup_run(c)
    obs_after(c, run, "s0", actual_switches={"pump": "ON", "ev": "OFF"})
    out = obs_after(c, run, "s1", actual_switches={"pump": "ON", "ev": "OFF"}, restart_meter=True)
    p = chan(out["state"]["observation"]["meter"]["channels"], "pump")
    assert p["meter_session"] == 2 and p["sequence_number"] == 1
    assert p["interval_energy_model_kwh"] == 0.55 and p["cumulative_energy_model_kwh"] == 0.55


def test_api_group_energy_with_two_evs_present_is_not_attributed(api):
    c = api
    sessions = [{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 16, "requested_model_kwh": 3},
                {"ev_id": "EV_2", "arrival_interval": 0, "departure_interval": 16, "requested_model_kwh": 3,
                 "charger_id": "charger-2"}]
    run = setup_run(c, ev_sessions=sessions)
    out = obs_after(c, run, "s0", actual_switches={"pump": "OFF", "ev": "ON"})
    o = out["state"]["observation"]
    assert o["ev_model_kw"] > 0 and o["ev_id"] is None  # group energy, not one vehicle's
    ee = out["state"]["readiness"]["ev_energy"]
    assert ee["measured_group_model_kwh"] == 0.825 and ee["allocated_by_ev_model_kwh"] == {"EV_1": 0.0, "EV_2": 0.0}
    assert ee["unallocated_group_model_kwh"] == 0.825 and ee["verified_by_ev_model_kwh"] is None
    from backend import main
    r = main.load_run(run)
    assert main.planning_state(r)["ev_group_unattributed_model_kwh"] == 0.825


def test_api_aggregate_scope_uses_one_combined_channel(api):
    c = api
    run = setup_run(c, observation_scope="aggregate")
    out = obs_after(c, run, "s0", actual_switches={"pump": "ON", "ev": "ON"})
    m = out["state"]["observation"]["meter"]
    assert [x["channel_id"] for x in m["channels"]] == ["participating"]
    assert out["state"]["observation"]["aggregate_model_kw"] == 5.5 and out["state"]["observation"]["pump_model_kw"] is None


# ---------- API: telemetry runs (device emulator meter) ----------
def telemetry_run(c, quiet=False):
    pf = load_profile()
    if quiet:
        pf["sensor_noise"]["power_fraction_sd"] = 0.0
    run = setup_run(c, observation_source="telemetry", telemetry_power_scale=1.0, meter_clock="simulated")
    return run, DeviceEmulator(pf, run, device_id="bench-1", clock_start_utc=S)


def post(c, samples):
    return [c.post("/api/v1/telemetry", headers=c.dev, json=s).json()["status"] for s in samples]


def test_telemetry_two_chargers_sum_and_reboot_counts_once(api):
    c = api
    run, emu = telemetry_run(c, quiet=True)
    emu.pump_on, emu.chargers["charger-1"], emu.chargers["charger-2"] = False, True, True
    post(c, emu.interval_samples(0))
    o0 = step(c, run, "t0")["state"]["observation"]
    ev0 = chan(o0["meter"]["channels"], "ev_group")
    # 2 x 3.3 kW; the counter started at zero at the run clock start, so interval 0 is counted in full
    assert ev0["average_power_model_kw"] == 6.6 and ev0["interval_energy_model_kwh"] == 1.65
    assert ev0["energy_basis"] == "energy_counter" and ev0["grouped"] and o0["ev_id"] is None
    # interval 1: device reboots after 5 samples and resends one sample (duplicate)
    first = [emu.sample(1) for _ in range(5)]
    emu.reboot()
    rest = [emu.sample(1) for _ in range(10)]
    st = post(c, first + rest + [rest[-1]])
    assert st[-1] == "DUPLICATE" and "NEW_DEVICE_SESSION" in st
    o1 = step(c, run, "t1")["state"]["observation"]
    ev1 = chan(o1["meter"]["channels"], "ev_group")
    # energy before the reboot (5 samples) + since the reboot (10 samples) = the full interval, counted once
    assert ev1["counter_restarts"] == 1 and ev1["interval_energy_model_kwh"] == 1.65
    assert ev1["average_power_model_kw"] == 6.6 and ev1["meter_session"] == f"{emu.boot_prefix}-2"
    assert ev1["cumulative_energy_model_kwh"] == 1.1  # the counter holds only the 10 minutes since the reboot


def test_telemetry_missing_channel_is_incomplete(api):
    c = api
    run, emu = telemetry_run(c)
    emu.pump_on = True
    emu.lost_channels = {"pump"}
    post(c, emu.interval_samples(0))
    out = step(c, run, "t0")
    o = out["state"]["observation"]
    lost = chan(o["meter"]["channels"], "pump")
    assert lost["quality"] == "MISSING"
    assert lost.get("raw_counter_kwh") is None and lost.get("raw_counter_increase_kwh") is None  # unavailable, not 0
    assert chan(o["meter"]["channels"], "ev_group").get("raw_counter_kwh") is not None
    assert o["pump_model_kw"] is None and o["aggregate_model_kw"] is None and not o["meter"]["total"]["complete"]


# ---------- review: measured vs allocated vs verified ----------
def test_nothing_is_labelled_verified_per_ev(api):
    c = api
    run = setup_run(c)
    out = obs_after(c, run, "s0", actual_switches={"pump": "OFF", "ev": "ON"})
    rd = out["state"]["readiness"]
    assert rd["verified_ev_delivered_by_ev_model_kwh"] is None and rd["verified_ev_delivered_model_kwh"] is None
    assert rd["verified_ev_unavailable_reason"].startswith("NO_PER_VEHICLE_MEASUREMENT")
    ee = rd["ev_energy"]
    assert ee["measured_group_model_kwh"] == 0.825 and ee["allocated_by_ev_model_kwh"] == {"EV_1": 0.825}
    assert "inferred" in ee["allocation_basis"] and ee["verified_by_ev_model_kwh"] is None
    assert rd["charging_status_basis"] == "allocation (inferred, not verified)"


def test_two_chargers_below_one_rating_are_allocated_not_verified(api):
    """Two chargers each drawing 1.5 kW (3.0 kW, under one 3.3 kW charger rating) with one declared EV. The group
    channel cannot tell them apart: the allocation credits all 3.0 kW to EV_1, while the emulator truth shows the
    two chargers delivered equal energy. The system must call this an inferred allocation and never verified."""
    c = api
    run, emu = telemetry_run(c, quiet=True)
    emu.chargers["charger-1"] = emu.chargers["charger-2"] = True
    emu.charger_kw = {"charger-1": 1.5, "charger-2": 1.5}
    post(c, emu.interval_samples(0))
    st = step(c, run, "t0")["state"]
    o = st["observation"]
    ev = chan(o["meter"]["channels"], "ev_group")
    assert abs(ev["average_power_model_kw"] - 3.0) < 1e-6 and ev["grouped"]
    assert o["ev_id"] == "EV_1"  # permitted allocation: one declared EV, group draw within one charger rating
    ee = st["readiness"]["ev_energy"]
    assert abs(ee["allocated_by_ev_model_kwh"]["EV_1"] - 0.75) < 1e-6 and "inferred" in ee["allocation_basis"]
    assert ee["verified_by_ev_model_kwh"] is None and st["readiness"]["verified_ev_delivered_by_ev_model_kwh"] is None
    # emulator truth (never sent to the server): charger-1 delivered only half the group energy
    d = emu.charger_delivered_kwh
    assert d["charger-1"] > 0 and abs(d["charger-1"] - d["charger-2"]) < 1e-9


# ---------- review: missing readings, then counter recovery ----------
def test_simulator_missing_then_recovery_reports_gap_once_unassigned(api):
    c = api
    run = setup_run(c)
    obs_after(c, run, "s0", actual_switches={"pump": "OFF", "ev": "ON"})
    obs_after(c, run, "s1", actual_switches={"pump": "OFF", "ev": "ON"}, drop_meter_channels=["ev_group"])
    obs_after(c, run, "s2", actual_switches={"pump": "OFF", "ev": "ON"}, drop_meter_channels=["ev_group"])
    out = obs_after(c, run, "s3", actual_switches={"pump": "OFF", "ev": "ON"})
    ev = chan(out["state"]["observation"]["meter"]["channels"], "ev_group")
    assert ev["interval_energy_model_kwh"] == 0.825  # this interval only
    assert ev["cumulative_energy_model_kwh"] == 3.3  # the meter counter includes the two lost intervals
    rec = ev["recovered_after_missing"]
    assert rec["recovered_unassigned_model_kwh"] == 1.65 and rec["missing_intervals"] == 2 and not rec["restart_in_gap"]
    assert rec["span_start_utc"] == "2026-08-18T18:45:00Z" and rec["span_end_utc"] == "2026-08-18T19:15:00Z"
    hist = {h["interval_index"]: h["observation_status"] for h in out["state"]["execution_history"]}
    assert hist[1] == hist[2] == "AMBIGUOUS"  # the lost intervals stay unknown; no per-interval timing invented
    ee = out["state"]["readiness"]["ev_energy"]
    assert ee["allocated_by_ev_model_kwh"]["EV_1"] == 1.65  # intervals 0 and 3 only; recovered energy not credited
    assert ee["recovered_unassigned_model_kwh"] == 1.65 and ee["measured_group_model_kwh"] == 1.65


def test_simulator_missing_then_restart_gives_lower_bound(api):
    c = api
    run = setup_run(c)
    obs_after(c, run, "s0", actual_switches={"pump": "OFF", "ev": "ON"})
    obs_after(c, run, "s1", actual_switches={"pump": "OFF", "ev": "ON"}, drop_meter_channels=["ev_group"])
    out = obs_after(c, run, "s2", actual_switches={"pump": "OFF", "ev": "ON"}, restart_meter=True)
    ev = chan(out["state"]["observation"]["meter"]["channels"], "ev_group")
    rec = ev["recovered_after_missing"]
    assert rec["restart_in_gap"] and rec["lower_bound"] and rec["recovered_unassigned_model_kwh"] == 0.0
    assert ev["cumulative_energy_model_kwh"] == ev["interval_energy_model_kwh"] == 0.825
    assert out["state"]["readiness"]["ev_energy"]["allocated_by_ev_model_kwh"]["EV_1"] == 1.65


def test_telemetry_missing_then_recovery_and_reboot_in_gap(api):
    c = api
    run, emu = telemetry_run(c, quiet=True)
    emu.ev_on = True
    for i in range(2):
        post(c, emu.interval_samples(i))
        step(c, run, f"t{i}")
    emu.lost_channels = {"ev_group"}  # interval 2 lost
    post(c, emu.interval_samples(2))
    assert chan(step(c, run, "t2")["state"]["observation"]["meter"]["channels"], "ev_group")["quality"] == "MISSING"
    emu.lost_channels = set()
    post(c, emu.interval_samples(3))
    o = step(c, run, "t3")["state"]["observation"]
    ev = chan(o["meter"]["channels"], "ev_group")
    # only interval 3's own readings: 14 of 15 one-minute samples; the minute before the first reading is in the gap
    assert ev["energy_basis"] == "energy_counter_within_interval" and ev["interval_energy_model_kwh"] == 0.77
    assert ev["average_power_model_kw"] == 3.3 and ev["covered_from_utc"] == "2026-08-18T19:16:00.000Z"
    rec = ev["recovered_after_missing"]
    assert rec["missing_intervals"] == 1 and not rec["restart_in_gap"]
    assert rec["recovered_unassigned_model_kwh"] == 0.88  # interval 2 (0.825) + the minute before 19:16 (0.055)
    assert rec["span_start_utc"] == "2026-08-18T19:00:00.000Z" and rec["span_end_utc"] == "2026-08-18T19:16:00.000Z"
    # totals preserved: allocated + recovered = the meter counter = what the chargers delivered
    ee = state(c, run)["readiness"]["ev_energy"]
    assert round(ee["allocated_by_ev_model_kwh"]["EV_1"] + ee["recovered_unassigned_model_kwh"], 6) == \
        ev["cumulative_energy_model_kwh"] == round(emu.charger_delivered_kwh["charger-1"], 6) == 3.3
    assert o["ev_id"] == "EV_1"  # this interval's own reading; the recovered energy is not attributed
    # interval 4 lost, then the device reboots before interval 5: energy before the restart is unknowable
    emu.lost_channels = {"ev_group"}
    post(c, emu.interval_samples(4))
    step(c, run, "t4")
    emu.lost_channels = set()
    emu.reboot()
    post(c, emu.interval_samples(5))
    o = step(c, run, "t5")["state"]["observation"]
    ev = chan(o["meter"]["channels"], "ev_group")
    rec = ev["recovered_after_missing"]
    assert rec["restart_in_gap"] and rec["lower_bound"] and rec["recovered_unassigned_model_kwh"] == 0.0
    # the new session started at zero at 19:45, inside interval 5, so interval 5 is counted in full
    assert ev["interval_energy_model_kwh"] == 0.825 and ev["meter_session"].endswith("-2")
    ee = state(c, run)["readiness"]["ev_energy"]
    assert ee["allocated_by_ev_model_kwh"]["EV_1"] == round(0.825 + 0.825 + 0.77 + 0.825, 4)  # intervals 0, 1, 3, 5
    assert ee["recovered_unassigned_model_kwh"] == 0.88 and ee["verified_by_ev_model_kwh"] is None
    # interval 4's 0.825 kWh is lost with the reboot: reported totals stay below delivered, never above
    assert ee["measured_group_model_kwh"] + ee["recovered_unassigned_model_kwh"] < emu.charger_delivered_kwh["charger-1"]


# ---------- review: evaluation report keeps ground truth apart ----------
def test_report_separates_ground_truth_from_measured_allocated_verified(api):
    c = api
    run = setup_run(c)
    obs_after(c, run, "s0", actual_switches={"pump": "OFF", "ev": "ON"})
    obs_after(c, run, "s1", actual_switches={"pump": "OFF", "ev": "ON"}, drop_meter_channels=["ev_group"])
    obs_after(c, run, "s2", actual_switches={"pump": "OFF", "ev": "ON"})
    rep = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()["ev_energy_accounting"]
    assert rep["ground_truth_delivered_by_ev_model_kwh"] == {"EV_1": 2.475}  # simulator truth: all three intervals
    assert rep["measured_group_model_kwh"] == 1.65 and rep["allocated_by_ev_model_kwh"] == {"EV_1": 1.65}
    assert rep["recovered_unassigned_model_kwh"] == 0.825 and rep["verified_by_ev_model_kwh"] is None
    assert rep["ground_truth_basis"].startswith("simulator truth")
    mrep = c.get(f"/api/v1/runs/{run}/report", headers=c.mgr).json()
    assert "ev_energy_accounting" not in mrep  # evaluator truth never reaches the manager
