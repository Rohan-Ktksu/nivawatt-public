"""Meter energy versus time: no time scaling, simulated timestamps for the emulator, measured counters unscaled.

Covers: 2.2 kW over 15 simulated minutes = 0.55 kWh at any execution speed; the same scenario at two speeds gives the
same energy; missing readings then counter recovery keep totals; reboots neither double-count nor invent energy;
physical-meter counters stay unscaled; inconsistent configuration fails with an explicit error.
"""
import copy
import uuid

import pytest

from simulation.device_emulator import DeviceEmulator, load_profile
from tests.core.test_core import FS, accept, api, step  # noqa: F401  (api is a fixture)
from tests.core.test_meter import chan, post


def setup_run(c, **params):
    """Like test_meter.setup_run, with request ids that cannot collide between runs."""
    tag = uuid.uuid4().hex[:8]
    r = c.post("/api/v1/runs", headers=c.op, json={"request_id": f"run-{tag}", "clock_start_utc": FS, "apartment_params": params})
    assert r.status_code == 200, r.text
    run = r.json()["run_id"]
    prop = c.post("/api/v1/requirements", headers=c.mgr, json={"run_id": run, "request_id": f"q-{tag}", "ev_sessions": []}).json()["proposal"]
    assert accept(c, run, prop, f"a-{tag}").status_code == 200
    return run

NORMAL, ACCELERATED = 900, 15  # wall seconds per 900 s model interval: real time, and the demo profile's 60x


def emulator(c, speed, **run_params):
    pf = copy.deepcopy(load_profile())
    pf["sensor_noise"]["power_fraction_sd"] = 0.0
    pf["emulator"]["seconds_per_model_interval"] = speed
    run = setup_run(c, observation_source="telemetry", meter_clock="simulated", **run_params)
    return run, DeviceEmulator(pf, run, device_id="bench-1", clock_start_utc=FS)


@pytest.mark.parametrize("speed", [NORMAL, ACCELERATED])
def test_2_2_kw_for_15_simulated_minutes_is_0_55_kwh_at_any_speed(api, speed):
    c = api
    run, emu = emulator(c, speed)
    emu.pump_on = True
    for i in range(2):
        samples = emu.interval_samples(i)
        assert len(samples) == 15  # the meter cadence is in simulated time; speed only paces the samples
        assert samples[-1]["device_monotonic_ms"] - samples[0]["device_monotonic_ms"] == 14 * speed * 1000 // 15
        post(c, samples)
        pump = chan(step(c, run, f"{run}-t{i}")["state"]["observation"]["meter"]["channels"], "pump")
        assert pump["interval_energy_model_kwh"] == 0.55 and pump["average_power_model_kw"] == 2.2
        assert pump["covered_s"] == 900.0 and pump["cumulative_energy_model_kwh"] == round(0.55 * (i + 1), 5)


def scenario(c, speed):
    """Pump; two chargers; EV channel lost; device reboot before the recovering reading. Returns what the server saw."""
    run, emu = emulator(c, speed, ev_sessions=[{"ev_id": "EV_1", "arrival_interval": 0, "departure_interval": 32,
                                                "requested_model_kwh": 10}])
    out = []
    plan = [dict(pump=True, c1=False, c2=False, lost=set(), reboot=False),
            dict(pump=False, c1=True, c2=True, lost=set(), reboot=False),
            dict(pump=False, c1=True, c2=False, lost={"ev_group"}, reboot=False),
            dict(pump=False, c1=True, c2=False, lost=set(), reboot=False),
            dict(pump=True, c1=True, c2=False, lost={"ev_group"}, reboot=False),
            dict(pump=True, c1=True, c2=False, lost=set(), reboot=True)]
    for i, x in enumerate(plan):
        emu.pump_on, emu.chargers["charger-1"], emu.chargers["charger-2"] = x["pump"], x["c1"], x["c2"]
        emu.lost_channels = x["lost"]
        if x["reboot"]:
            emu.reboot()
        post(c, emu.interval_samples(i))
        o = step(c, run, f"{run}-t{i}")["state"]["observation"]
        out.append([{k: ch.get(k) for k in ("channel_id", "quality", "average_power_model_kw", "interval_energy_model_kwh",
                                            "cumulative_energy_model_kwh", "counter_restarts", "energy_basis",
                                            "recovered_after_missing", "covered_from_utc", "covered_to_utc")}
                    for ch in o["meter"]["channels"]])
    s = c.get(f"/api/v1/state?run_id={run}&apartment_id=APT_1", headers=c.op).json()
    return out, s["readiness"]["ev_energy"], emu


def test_execution_speed_does_not_change_energy(api):
    a, ea, emu_a = scenario(api, ACCELERATED)
    b, eb, emu_b = scenario(api, NORMAL)
    assert a == b  # every channel value, recovered figure and covered span identical
    assert {k: v for k, v in ea.items()} == {k: v for k, v in eb.items()}
    assert emu_a.charger_delivered_kwh == emu_b.charger_delivered_kwh  # same simulated physics
    pump3 = next(ch for ch in a[0] if ch["channel_id"] == "pump")
    assert pump3["interval_energy_model_kwh"] == 0.55


def test_missing_then_recovery_preserves_totals_and_reboot_invents_nothing(api):
    out, ee, emu = scenario(api, ACCELERATED)
    ev = [next(ch for ch in row if ch["channel_id"] == "ev_group") for row in out]
    assert ev[1]["interval_energy_model_kwh"] == 1.65  # two chargers, one group channel
    assert ev[2]["quality"] == "MISSING" and ev[2]["interval_energy_model_kwh"] is None
    # interval 3 recovers interval 2 from the counter: interval 0..3 totals equal the counter and the delivered truth
    rec = ev[3]["recovered_after_missing"]
    total = ev[0]["interval_energy_model_kwh"] + ev[1]["interval_energy_model_kwh"] + rec["recovered_unassigned_model_kwh"] \
        + ev[3]["interval_energy_model_kwh"]
    assert round(total, 6) == ev[3]["cumulative_energy_model_kwh"] == 3.3
    # interval 4 lost, then a reboot: energy before the restart is unknowable and is not invented
    assert ev[5]["recovered_after_missing"]["lower_bound"] and ev[5]["recovered_after_missing"]["recovered_unassigned_model_kwh"] == 0
    assert ev[5]["counter_restarts"] == 0 and ev[5]["interval_energy_model_kwh"] == 0.825  # new session from zero
    delivered = sum(emu.charger_delivered_kwh.values())
    assert round(delivered, 6) == 4.95  # 0 + 1.65 + 0.825 x 4
    seen = ee["measured_group_model_kwh"] + ee["recovered_unassigned_model_kwh"]
    assert seen <= delivered and round(delivered - seen, 6) == 0.825  # only interval 4 (before the reboot) is unknown


def hw(run, seq, mono_ms, pump_kwh, ev_kwh, pump_w, ev_w, session="hw-1", clock="device"):
    return {"run_id": run, "apartment_id": "APT_1", "device_id": "bench-1", "device_session_id": session,
            "seq_num": seq, "device_monotonic_ms": mono_ms, "source_kind": "hardware", "tank_volume_l": 4500.0,
            "pump_power_w": pump_w, "ev_load_power_w": ev_w, "aggregate_power_w": pump_w + ev_w,
            "meter": {"meter_id": "bench-meter", "clock": clock, "meter_session": session,
                      "channels": [{"channel_id": "pump", "devices": ["pump-1"], "power_w": pump_w, "energy_kwh": pump_kwh},
                                   {"channel_id": "ev_group", "devices": ["charger-1", "charger-2"], "power_w": ev_w,
                                    "energy_kwh": ev_kwh}]}}


def test_physical_meter_counters_are_not_scaled(api):
    c = api
    run = setup_run(c, observation_source="telemetry")  # meter_clock defaults to device
    # a physical meter that already counted 12.3 kWh before this run; readings 60 s apart on the device clock
    pts = [hw(run, k + 1, 60_000 * k, 12.3 + 0.0366667 * k, 4.0, 2200.0, 0.0) for k in range(16)]
    assert all(c.post("/api/v1/telemetry", headers=c.dev, json=x).json()["status"] in ("ACCEPTED", "NEW_DEVICE_SESSION")
               for x in pts)
    o = step(c, run, "h0")["state"]["observation"]
    pump = chan(o["meter"]["channels"], "pump")
    assert o["source_kind"] == "hardware"
    # measured counter increase over 15 device minutes, unscaled; the counter is the meter's own figure
    assert pump["raw_counter_increase_kwh"] == pump["interval_energy_model_kwh"] == round(0.0366667 * 15, 5)
    assert round(pump["raw_counter_kwh"], 5) == pump["cumulative_energy_model_kwh"] == round(12.3 + 0.0366667 * 15, 5)
    assert pump["covered_s"] == 900.0 and abs(pump["average_power_model_kw"] - 2.2) < 1e-3
    assert pump["energy_basis"] == "energy_counter_within_interval"  # first reading: earlier counts are not this run's
    assert o["meter"]["power_scale"] == 1.0 and o["meter"]["label"].startswith("Physical meter: unscaled measurements")
    prov = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()["provenance"]
    assert prov["meter"] == {"meter_clock": "device", "energy": "counter increases in kWh; no time scaling",
                             "durations": "device clock (monotonic)", "telemetry_power_scale": 1.0,
                             "telemetry_volume_scale": 1.0}  # volume scale added (additive)
    assert prov["source_kind"] == "hardware" and prov["label"] == o["meter"]["label"]
    assert prov["plant"].startswith("device telemetry, measured by hardware (unscaled)")


def test_inconsistent_configuration_fails_explicitly(api):
    c = api
    # hardware may not report on simulated time
    run = setup_run(c, observation_source="telemetry")
    r = c.post("/api/v1/telemetry", headers=c.dev, json=hw(run, 1, 0, 1.0, 0.0, 0.0, 0.0, clock="simulated"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "METER_CLOCK_MISMATCH"
    # an emulator posting to a run that did not declare meter_clock=simulated (e.g. one created before this change)
    pf = load_profile()
    emu = DeviceEmulator(pf, run, device_id="bench-1", clock_start_utc=FS)
    r = c.post("/api/v1/telemetry", headers=c.dev, json=emu.sample(0))
    assert r.status_code == 422 and r.json()["error"]["code"] == "METER_CLOCK_MISMATCH"
    assert "meter_clock=simulated" in r.json()["error"]["message"]
    # simulated timestamps outside the run's current interval (wrong speed or clock start)
    run2, emu2 = emulator(c, ACCELERATED)
    r = c.post("/api/v1/telemetry", headers=c.dev, json=[emu2.sample(1) for _ in range(1)][0])
    assert r.status_code == 422 and r.json()["error"]["code"] == "METER_TIME_OUTSIDE_INTERVAL"
    # counters that accumulated wall-clock energy (60x too small for the simulated time) are rejected, not stored
    run3, emu3 = emulator(c, ACCELERATED)
    emu3.pump_on = True
    s1, s2 = emu3.sample(0), emu3.sample(0)
    s2["meter"]["channels"][0]["energy_kwh"] = s1["meter"]["channels"][0]["energy_kwh"] + \
        (s2["meter"]["channels"][0]["energy_kwh"] - s1["meter"]["channels"][0]["energy_kwh"]) / 60
    assert c.post("/api/v1/telemetry", headers=c.dev, json=s1).status_code == 200
    r = c.post("/api/v1/telemetry", headers=c.dev, json=s2)
    assert r.status_code == 422 and r.json()["error"]["code"] == "METER_COUNTER_INCONSISTENT"
    # the removed run parameter is rejected by name, not silently ignored
    r = c.post("/api/v1/runs", headers=c.op, json={"request_id": "old", "clock_start_utc": FS,
                                                   "apartment_params": {"telemetry_time_scale": 60}})
    assert r.status_code == 422 and "telemetry_time_scale" in r.json()["error"]["message"]
    # an emulator without a clock start sends no meter block: the earlier per-branch path still works
    run4 = setup_run(c, observation_source="telemetry")
    old = DeviceEmulator(load_profile(), run4, device_id="bench-1")
    old.pump_on = True
    post(c, old.interval_samples(0))
    o = step(c, run4, "o0")["state"]["observation"]
    assert "meter" not in o and o["pump_model_kw"] > 2


def test_power_scale_is_applied_exactly_once(api):
    """telemetry_power_scale (device-to-model conversion) multiplies counter energy once; time is never scaled."""
    c = api
    run = setup_run(c, observation_source="telemetry", telemetry_power_scale=2.0)
    pts = [hw(run, k + 1, 60_000 * k, 1.0 + 0.0366667 * k, 0.5, 2200.0, 0.0) for k in range(16)]
    for x in pts:
        assert c.post("/api/v1/telemetry", headers=c.dev, json=x).status_code == 200
    o = step(c, run, "sc0")["state"]["observation"]
    pump = chan(o["meter"]["channels"], "pump")
    assert o["source_kind"] == "scaled_hardware"
    assert pump["interval_energy_model_kwh"] == round(2 * pump["raw_counter_increase_kwh"], 5)
    assert pump["cumulative_energy_model_kwh"] == round(2 * pump["raw_counter_kwh"], 5)
    assert pump["covered_s"] == 900.0  # device clock, unscaled
    assert abs(pump["average_power_model_kw"] - 2 * 2.2) < 2e-3  # energy x2 over unscaled time: power x2, not x4
    # provenance: scaled bench values are never called hardware measurements; card label and report agree
    m = o["meter"]
    assert m["power_scale"] == 2.0 and m["label"] == "Scaled from bench measurements; model values, not field measurements."
    prov = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()["provenance"]
    assert prov["source_kind"] == "scaled_hardware"
    assert prov["label"] == ("Bench measurements: electrical measurements model-scaled (×2); water volume unscaled (×1). "
                             "Scaled values are model values, not field measurements.")
    assert "measured by hardware" not in prov["plant"]
    assert prov["plant"].startswith("device telemetry from bench measurements; electrical measurements model-scaled (x2); "
                                    "water volume unscaled (x1); scaled quantities are model values")
    assert m["electrical_source_kind"] == "scaled_hardware" and m["volume_scale"] == 1.0
    # the total is the sum of the scaled channel values, applied once; raw values sit beside it unchanged
    assert m["total"]["interval_energy_model_kwh"] == round(sum(ch["interval_energy_model_kwh"] for ch in m["channels"]), 5)
    assert m["total"]["interval_energy_model_kwh"] == round(2 * sum(ch["raw_counter_increase_kwh"] for ch in m["channels"]), 5)


def test_volume_only_scaling_leaves_electrical_readings_unscaled(api):
    """Only the tank volume is scaled: the meter card must not imply electrical measurements were scaled."""
    c = api
    run = setup_run(c, observation_source="telemetry", telemetry_volume_scale=10.0)
    pts = [hw(run, k + 1, 60_000 * k, 1.0 + 0.0366667 * k, 0.5, 2200.0, 0.0) for k in range(16)]
    for x in pts:
        x["tank_volume_l"] = 450.0
        assert c.post("/api/v1/telemetry", headers=c.dev, json=x).status_code == 200
    o = step(c, run, "v0")["state"]["observation"]
    m, pump = o["meter"], chan(o["meter"]["channels"], "pump")
    assert o["source_kind"] == "scaled_hardware" and o["tank_l"] == 4500.0  # the tank reading is model-scaled
    assert m["source_kind"] == "scaled_hardware"  # observation metadata preserved
    assert m["electrical_source_kind"] == "hardware" and m["power_scale"] == 1.0 and m["volume_scale"] == 10.0
    assert m["label"].startswith("Physical meter: unscaled measurements")
    assert pump["interval_energy_model_kwh"] == pump["raw_counter_increase_kwh"]  # electrical values unscaled
    prov = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()["provenance"]
    assert "measured by hardware" not in prov["plant"]
    assert "electrical measurements unscaled (x1); water volume model-scaled (x10)" in prov["plant"]
    assert prov["label"].startswith("Bench measurements: electrical measurements unscaled (×1); water volume model-scaled (×10).")
    assert "model-scaled (×1)" not in prov["label"] and "electrical measurements model-scaled" not in prov["label"]


def test_synthetic_scale_1_emulator_run_is_labelled_simulated(api):
    c = api
    run, emu = emulator(c, ACCELERATED)
    emu.pump_on = True
    post(c, emu.interval_samples(0))
    o = step(c, run, "e0")["state"]["observation"]
    assert o["source_kind"] == "synthetic" and o["meter"]["power_scale"] == 1.0
    assert o["meter"]["label"] == "Simulated meter: synthetic readings, not a measurement"
    prov = c.get(f"/api/v1/runs/{run}/report", headers=c.op).json()["provenance"]
    assert prov["source_kind"] == "synthetic" and prov["label"] == o["meter"]["label"]
    assert "measured" not in prov["plant"] and "emulator (synthetic readings, not measurements)" in prov["plant"]
