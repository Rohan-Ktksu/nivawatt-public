"""Software stand-in for the bench device: produces TELEMETRY_EXAMPLE-shaped readings (source_kind=synthetic).

It models the switches a person operates, tank mass balance and sensor noise, then emits raw readings:
true-scale watts per branch plus model-scale tank litres. It knows nothing about plans, claims or verdicts.

It also carries a simulated multi-channel meter (simulation/meter.py channels): a pump channel and one EV charger-group
channel wired to two chargers. Each sample reports per-channel power (W, true scale) and an energy counter in kWh of
SIMULATED energy: power x simulated time, never wall-clock time. Every meter sample carries its simulated timestamp
(sample_time_utc, the end of its sample period) and the simulated time its counter session started at zero
(session_start_utc). The meter's cadence is a property of the meter in simulated time
(emulator.samples_per_model_interval readings per 900 s model interval). Execution speed
(emulator.seconds_per_model_interval wall seconds per model interval) only paces the samples and advances the device's
monotonic clock; it never changes a sample, a timestamp or the energy. The counter restarts at zero when the device
reboots. The meter knows only its wiring: which vehicle a charger serves is not part of its reading.
A meter block is emitted only when clock_start_utc (the run's clock start) is given: without simulated timestamps the
server could not integrate energy, so the emulator then sends the earlier per-branch fields only.
"""
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from simulation import meter, plant

PROFILES = Path(__file__).resolve().parent / "profiles"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_profile(name="apartment_reference"):
    return json.loads((PROFILES / f"{name}.json").read_text())


class DeviceEmulator:
    def __init__(self, profile, run_id, apartment_id="APT_1", device_id="bench-1", seed=0, clock_start_utc=None):
        self.pf, self.run_id, self.apartment_id, self.device_id = profile, run_id, apartment_id, device_id
        self.rng = random.Random(seed)
        self.tank_l = float(profile["tank"]["initial_l"])
        self.pump_on = False
        self.chargers = {"charger-1": False, "charger-2": False}  # each charger's switch
        self.charger_requested_kwh = {"charger-1": 10.0, "charger-2": 10.0}  # a vehicle stops drawing when full
        self.charger_delivered_kwh = {"charger-1": 0.0, "charger-2": 0.0}  # emulator truth, never sent
        self.charger_kw = {}  # optional per-charger draw override in kW (e.g. a tapering vehicle); default: rated
        self.channels = meter.default_channels("branch")
        self.counters_kwh = {}  # simulated energy since the counter session started
        self.lost_channels = set()  # channels whose values are missing from the next samples (fault injection)
        self.dry = False  # fault: pump runs but source water is absent
        self.boot = 1
        self.boot_prefix = f"boot-{uuid.uuid4().hex[:12]}"  # a real device gets a fresh boot id at every power-up
        self.seq = 0
        self.mono_ms = 0
        self.withdrawal = plant.default_withdrawals()
        em = profile["emulator"]
        # readings per model interval: the meter cadence in simulated time (older profiles: derived from wall period)
        self.samples_per_interval = int(em.get("samples_per_model_interval")
                                        or round(em["seconds_per_model_interval"] / em["sample_period_s"]))
        self.period_s = em["seconds_per_model_interval"] / self.samples_per_interval  # wall seconds per sample (pacing)
        self.dt_sim_s = 900 / self.samples_per_interval  # simulated seconds covered by one sample
        self.time_scale = self.dt_sim_s / self.period_s  # execution speed: simulated seconds per wall second
        self.clock_start = None if clock_start_utc is None else \
            datetime.fromisoformat(clock_start_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        self.session_start = self.clock_start  # counter session 1 starts at zero when the run clock starts
        self._interval, self._k, self._restarted = None, 0, False

    # Back-compatible single-charger switch: "EV on" means charger-1.
    @property
    def ev_on(self):
        return any(self.chargers.values())

    @ev_on.setter
    def ev_on(self, on):
        self.chargers["charger-1"] = bool(on)

    @property
    def ev_requested_kwh(self):
        return self.charger_requested_kwh["charger-1"]

    @ev_requested_kwh.setter
    def ev_requested_kwh(self, kwh):
        self.charger_requested_kwh["charger-1"] = float(kwh)

    def reboot(self):
        """Device restart: new session id; sequence, monotonic clock and meter counters restart from zero. The new counter
        session starts at the beginning of the next sample period (simulated time)."""
        self.boot += 1
        self.seq = 0
        self.mono_ms = 0
        self.counters_kwh = {}
        self._restarted = True

    def sample(self, interval_index):
        """Advance one sample period of physics and return one telemetry payload."""
        pf, dt_model_h = self.pf, self.dt_sim_s / 3600
        if interval_index != self._interval:
            self._interval, self._k = interval_index, 0
        self._k += 1
        t_end = None if self.clock_start is None else \
            self.clock_start + timedelta(seconds=interval_index * 900 + self._k * self.dt_sim_s)
        if self._restarted:
            self.session_start = None if t_end is None else t_end - timedelta(seconds=self.dt_sim_s)
            self._restarted = False
        per_interval = self.samples_per_interval
        if self.pump_on and not self.dry:
            self.tank_l += pf["pump"]["flow_lpm"] * 60 * dt_model_h
        self.tank_l = min(self.tank_l, pf["tank"]["capacity_l"])  # overflow controller
        w = self.withdrawal[interval_index % len(self.withdrawal)] / per_interval
        self.tank_l = max(0.0, self.tank_l - w)
        noise = pf["sensor_noise"]
        pump_w = pf["pump"]["rated_kw"] * 1000 * (1 + self.rng.gauss(0, noise["power_fraction_sd"])) if self.pump_on else 0.0
        device_w = {"pump-1": pump_w}
        for cid, on in self.chargers.items():
            w_ = 0.0
            if on and self.charger_delivered_kwh[cid] < self.charger_requested_kwh[cid] - 1e-9:
                kw_ = self.charger_kw.get(cid, pf["ev_charger"]["rated_kw"])
                w_ = kw_ * 1000 * (1 + self.rng.gauss(0, noise["power_fraction_sd"]))
                self.charger_delivered_kwh[cid] += w_ / 1000 * dt_model_h
            device_w[cid] = w_
        ev_w = sum(w_ for d, w_ in device_w.items() if d != "pump-1")
        self.seq += 1
        self.mono_ms += int(self.period_s * 1000)
        chans = []
        for ch in self.channels:
            w_ = sum(device_w.get(d, 0.0) for d in ch["devices"])
            self.counters_kwh[ch["channel_id"]] = self.counters_kwh.get(ch["channel_id"], 0.0) + w_ / 1000 * dt_model_h
            if ch["channel_id"] not in self.lost_channels:
                chans.append({"channel_id": ch["channel_id"], "devices": list(ch["devices"]), "power_w": round(w_, 1),
                              "energy_kwh": round(self.counters_kwh[ch["channel_id"]], 6)})
        return {
            "schema_version": "0.2", "run_id": self.run_id, "apartment_id": self.apartment_id,
            "device_id": self.device_id, "device_session_id": f"{self.boot_prefix}-{self.boot}", "seq_num": self.seq,
            "device_monotonic_ms": self.mono_ms, "source_kind": "synthetic",
            "supply_present": True, "voltage_v": pf["pump"]["supply_v"],
            "aggregate_power_w": round(pump_w + ev_w, 1), "pump_power_w": None if meter.PUMP in self.lost_channels else round(pump_w, 1),
            "ev_load_power_w": None if meter.EV_GROUP in self.lost_channels else round(ev_w, 1),
            "tank_volume_l": round(max(0.0, self.tank_l + self.rng.gauss(0, noise["tank_l_sd"])), 1),
            "sump_water_available": not self.dry,
            "pump_switch_position": "ON" if self.pump_on else "OFF",
            "ev_switch_position": "ON" if self.ev_on else "OFF",
            "branch_attribution": "BRANCH_METERS", "quality": "VALID",
            "profile_id": pf["profile_id"],
            **({} if t_end is None else {"meter": {
                "meter_id": "meter-1", "clock": "simulated", "meter_session": f"{self.boot_prefix}-{self.boot}",
                "sample_time_utc": _iso(t_end), "session_start_utc": _iso(self.session_start), "channels": chans}}),
            "note": "Emulated device reading (synthetic). Not a physical sensor.",
        }

    def interval_samples(self, interval_index):
        n = self.samples_per_interval
        return [self.sample(interval_index) for _ in range(n)]
