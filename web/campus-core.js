// Campus demo group: four separate apartment buildings, one run each, driven from one operator browser.
// Pure logic (no DOM), so it runs under node --test. The service keeps one apartment per run; the group is a local
// record (scenario, forecast, clock, apartment -> run) kept in the operator's browser. Every write uses a request ID
// derived from the group, apartment and interval, so the service's idempotency makes setup, delivery and steps safe to
// repeat: a resumed setup returns the same runs, and a retried step returns the stored result instead of stepping twice.
const NW_CAMPUS = (() => {
  const SCENARIO_ID = 'campus-four-v1';
  const APARTMENTS = ['APT_1', 'APT_2', 'APT_3', 'APT_4'];
  // Reproducible synthetic inputs (tested against the local service): different tank levels and EV energy needs, so
  // the buildings behave differently. With APT_2's 10.5 kWh, one missed charging interval leaves about 0.6 kWh short,
  // well over the revision gate's 0.1 kWh rule, so a revision is offered.
  const ev = (kwh) => [{ ev_id: 'EV_1', arrival_interval: 0, departure_interval: 32, requested_model_kwh: kwh }];
  const BUILDINGS = {
    APT_1: { label: 'Building 1', story: 'Follows the accepted charging plan; the meter agrees.', params: { tank_init_l: 6000, ev_sessions: ev(8) } },
    APT_2: { label: 'Building 2', story: 'Reports charging while the charger stays off; a revision follows.', params: { tank_init_l: 5000, ev_sessions: ev(10.5) } },
    APT_3: { label: 'Building 3', story: 'Low tank: the pump runs and the tank reading rises.', params: { tank_init_l: 3300, ev_sessions: ev(4) } },
    APT_4: { label: 'Building 4', story: 'A reading goes missing; its equipment shows Unknown.', params: { tank_init_l: 4500, ev_sessions: ev(6) } },
  };
  // Simulated reading faults the operator can apply to one building for one step (existing step API fields).
  const FAULTS = {
    none: { label: 'Normal reading', body: {} },
    missing: { label: 'Reading missing', body: { drop_observation: true } },
    late: { label: 'Reading arrives late', body: { delay_observation: true } },
    'pump-meter': { label: 'Pump meter channel missing', body: { drop_meter_channels: ['pump'] } },
    'charger-meter': { label: 'Charger meter channel missing', body: { drop_meter_channels: ['ev_group'] } },
  };

  const newGroupId = () => `campus-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
  const runRequestId = (gid, apt) => `${gid}-${apt}-run`;
  const advisoryRequestId = (gid, apt, forecastId) => `${gid}-${apt}-advisory-${forecastId}`;
  const stepRequestId = (gid, apt, i) => `${gid}-${apt}-step-${i}`;

  function newGroup(forecast, gid = newGroupId()) {
    return {
      group_id: gid, scenario_id: SCENARIO_ID,
      forecast: { forecast_id: forecast.forecast_id, forecast_start: forecast.forecast_start, forecast_end: forecast.forecast_end, peak_windows: forecast.peak_windows },
      clock: { clock_start_utc: forecast.forecast_start, model_dt_minutes: 15, horizon_intervals: 96 },
      members: Object.fromEntries(APARTMENTS.map((a) => [a, { run_id: null }])),
      deliveries: {}, interval: 0, pending: null,
    };
  }

  const runBody = (g, apt) => ({
    request_id: runRequestId(g.group_id, apt),
    clock_start_utc: g.clock.clock_start_utc,
    apartment_params: { apartment_id: apt, ...BUILDINGS[apt].params },
  });

  // Creates the runs still missing, saving after each one, so a failed setup resumes where it stopped.
  async function ensureRuns(g, createRun, save) {
    const errors = {};
    for (const apt of APARTMENTS) {
      if (g.members[apt].run_id) continue;
      try {
        const out = await createRun(runBody(g, apt));
        if (out.model_dt_minutes !== g.clock.model_dt_minutes || out.horizon_intervals !== g.clock.horizon_intervals) {
          throw new Error(`${apt} run has a different clock (${out.model_dt_minutes} min x ${out.horizon_intervals})`);
        }
        g.members[apt].run_id = out.run_id;
        save(g);
      } catch (e) { errors[apt] = e; }
    }
    return errors;
  }

  const advisoryBody = (g, apt, windows) => ({
    request_id: advisoryRequestId(g.group_id, apt, g.forecast.forecast_id),
    kind: 'peak_advisory', run_id: g.members[apt].run_id, known_from_interval: g.interval,
    forecast_id: g.forecast.forecast_id, target_apartments: APARTMENTS, peak_windows: windows,
    valid_from: g.forecast.forecast_start, valid_until: g.forecast.forecast_end,
  });

  // Issues the approved windows to each run separately. Delivered runs are skipped, so a retry resends only failures.
  // The body is snapshotted per run on first send, so a retry repeats exactly the same request.
  async function deliverAdvisory(g, windows, postEvent, save) {
    await Promise.all(APARTMENTS.map(async (apt) => {
      const d = g.deliveries[apt] || (g.deliveries[apt] = { status: 'pending', body: advisoryBody(g, apt, windows) });
      if (d.status === 'delivered') return;
      d.status = 'sending';
      try {
        const out = await postEvent(d.body);
        Object.assign(d, { status: 'delivered', event_id: out.event_id, error: null });
      } catch (e) {
        Object.assign(d, { status: 'failed', error: e.message || String(e) });
      }
    }));
    save(g);
    return APARTMENTS.filter((a) => g.deliveries[a].status !== 'delivered');
  }

  // Switch positions that follow each run's accepted plan for its current interval (OFF when no plan covers it).
  function planSwitches(st) {
    const i = st.clock.interval_index;
    const a = st.active_commitment?.option?.actions?.find((x) => x.interval_index === i);
    return { pump: a?.pump === 'ON' ? 'ON' : 'OFF', ev: a?.ev && a.ev !== 'OFF' ? 'ON' : 'OFF' };
  }

  const stepBody = (g, apt, i, input) => ({
    request_id: stepRequestId(g.group_id, apt, i),
    actual_switches: { pump: input.pump, ev: input.ev },
    ...FAULTS[input.fault || 'none'].body,
  });

  // Where each member's server clock is relative to the group. With no advance pending every member must be at
  // g.interval. While one is pending (interval k), members at k+1 are done and members at k are behind.
  function clockCheck(g, clocks) {
    const k = g.interval;
    const done = []; const behind = []; const unexpected = [];
    for (const apt of APARTMENTS) {
      const c = clocks[apt];
      if (c === k) behind.push(apt);
      else if (c === k + 1 && g.pending && g.pending.interval === k) done.push(apt);
      else unexpected.push(apt);
    }
    return { interval: k, done, behind, unexpected, aligned: !unexpected.length && (!g.pending ? behind.length === APARTMENTS.length : true) };
  }

  const retryable = (e) => e && (e.code === 'NETWORK' || e.code === 'TIMEOUT' || e.status >= 500);

  // One controller per page: at most one group advance (or member retry) in flight.
  // deps: readClock(apt) -> interval, step(apt, body) -> step response, save(g), retries (extra attempts per member).
  function controller({ readClock, step, save, retries = 2 }) {
    let inFlight = false;

    async function sendOne(g, apt, body, k) {
      let lastErr = null;
      for (let attempt = 0; attempt <= retries; attempt += 1) {
        try {
          const out = await step(apt, body);  // same request ID and payload on every attempt
          const now = out?.state?.clock?.interval_index;
          if (out.stepped_interval !== k || now !== k + 1) {
            return { apt, status: 'failed', error: `server reports interval ${now} after stepping ${out.stepped_interval}, expected ${k + 1}` };
          }
          return { apt, status: 'done' };
        } catch (e) {
          lastErr = e;
          if (e.code === 'ABORTED' || !retryable(e)) break;
        }
      }
      // The response may have been lost after the service stepped: the server clock decides.
      try { if (await readClock(apt) === k + 1) return { apt, status: 'done', recovered: true }; } catch { /* reported below */ }
      return { apt, status: 'failed', error: lastErr?.message || String(lastErr) };
    }

    // inputs: { apt: { pump, ev, fault } }, used only when no advance is pending. only: retry just this member.
    async function advance(g, inputs, only = null) {
      if (inFlight) return { status: 'busy' };
      inFlight = true;
      try {
        const clocks = {};
        await Promise.all(APARTMENTS.map(async (a) => { clocks[a] = await readClock(a); }));
        const chk = clockCheck(g, clocks);
        if (chk.unexpected.length) return { status: 'unexpected', clocks, check: chk };
        const k = g.interval;
        if (!g.pending) {
          if (only) return { status: 'nothing-pending', clocks, check: chk };
          if (k >= g.clock.horizon_intervals) return { status: 'finished', clocks, check: chk };
          g.pending = { interval: k, bodies: Object.fromEntries(APARTMENTS.map((a) => [a, stepBody(g, a, k, inputs[a])])) };
          save(g);  // the snapshot survives a reload, so a retry after one sends the same payload
        }
        const todo = chk.behind.filter((a) => !only || a === only);
        const results = await Promise.all(todo.map((a) => sendOne(g, a, g.pending.bodies[a], k)));
        const failed = results.filter((r) => r.status !== 'done').map((r) => r.apt);
        const stillBehind = chk.behind.filter((a) => failed.includes(a) || !todo.includes(a));
        if (!stillBehind.length) {
          g.interval = k + 1;
          g.pending = null;
          save(g);
          return { status: 'advanced', interval: g.interval, results };
        }
        save(g);
        return { status: 'partial', interval: k, behind: stillBehind, results };
      } finally {
        inFlight = false;
      }
    }

    return { advance, get inFlight() { return inFlight; } };
  }

  // Participating load across the four buildings: only when every building has a usable reading (VALID or LATE) for
  // the same interval and the same measurement scope, with both pump and charger power known. Otherwise partial.
  function campusTotal(states) {
    const obs = APARTMENTS.map((a) => states[a]?.observation || null);
    const why = (reason) => ({ complete: false, text: 'Partial data', reason });
    if (obs.some((o) => !o)) return why('Not every building has a reading yet.');
    if (obs.some((o) => o.quality !== 'VALID' && o.quality !== 'LATE')) return why('A building has no usable reading for its latest interval.');
    if (new Set(obs.map((o) => o.interval_index)).size !== 1) return why('The buildings\' latest readings are for different intervals.');
    if (new Set(obs.map((o) => o.measurement_scope)).size !== 1) return why('The buildings measure different scopes.');
    const ch = (o, id, field) => {
      const c = (o.meter?.channels || []).find((x) => x.channel_id === id);
      if (c) return c.quality === 'MISSING' ? null : c.average_power_model_kw;
      return o[field] ?? null;
    };
    let kw = 0;
    for (const o of obs) {
      const p = ch(o, 'pump', 'pump_model_kw');
      const e = ch(o, 'ev_group', 'ev_model_kw');
      if (p === null || p === undefined || e === null || e === undefined) return why('A pump or charger reading is missing.');
      kw += p + e;
    }
    return { complete: true, kw: Math.round(kw * 1000) / 1000, interval: obs[0].interval_index, text: `${kw.toFixed(2)} kW (model)` };
  }

  return { SCENARIO_ID, APARTMENTS, BUILDINGS, FAULTS, newGroupId, newGroup, runBody, ensureRuns, advisoryBody, deliverAdvisory, planSwitches, stepBody, stepRequestId, clockCheck, controller, campusTotal };
})();

if (typeof module !== 'undefined') module.exports = NW_CAMPUS;
