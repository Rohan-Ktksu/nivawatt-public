// Run with: node --test tests/ui/campus.test.js
// A fake service with the real one's step semantics: request_id idempotency (same ID and body -> stored response,
// same ID with another body -> 409), one interval per new step.
const test = require('node:test');
const assert = require('node:assert');
const C = require('../../web/campus-core.js');

const FORECAST = { forecast_id: 'fc-1', forecast_start: '2026-08-18T18:30:00Z', forecast_end: '2026-08-19T18:30:00Z', peak_windows: [{ start: 'a', end: 'b' }] };
const err = (code, status = 0) => Object.assign(new Error(code), { code, status });

function fakeService() {
  const clock = Object.fromEntries(C.APARTMENTS.map((a) => [a, 0]));
  const seen = new Map();
  const steps = [];
  const faults = {};  // apt -> list of 'before' (fail without stepping) / 'after' (step, then lose the response)
  const step = async (apt, body) => {
    steps.push({ apt, body });
    const f = (faults[apt] || []).shift();
    if (f === 'before') throw err('NETWORK');
    const key = JSON.stringify(body);
    let out;
    if (seen.has(body.request_id)) {
      if (seen.get(body.request_id).key !== key) throw err('REQUEST_ID_REUSED', 409);
      out = seen.get(body.request_id).out;
    } else {
      const i = clock[apt];
      clock[apt] = i + 1;
      out = { stepped_interval: i, state: { clock: { interval_index: i + 1 } } };
      seen.set(body.request_id, { key, out });
    }
    if (f === 'after') throw err('TIMEOUT');
    return out;
  };
  return { clock, steps, faults, step, readClock: async (apt) => clock[apt] };
}

const inputs = Object.fromEntries(C.APARTMENTS.map((a) => [a, { pump: 'OFF', ev: 'ON', fault: 'none' }]));
const group = () => { const g = C.newGroup(FORECAST, 'g1'); C.APARTMENTS.forEach((a, n) => { g.members[a].run_id = `run-${n}`; }); return g; };

test('setup resumes without duplicates: same request ID per apartment, saved after each run', async () => {
  const g = C.newGroup(FORECAST, 'g1');
  const created = [];
  let fail = 'APT_3';
  const create = async (body) => {
    created.push(body.request_id);
    if (body.apartment_params.apartment_id === fail) throw new Error('timeout');
    return { run_id: `run-${body.apartment_params.apartment_id}`, model_dt_minutes: 15, horizon_intervals: 96 };
  };
  const errors = await C.ensureRuns(g, create, () => {});
  assert.deepStrictEqual(Object.keys(errors), ['APT_3']);
  fail = null;
  await C.ensureRuns(g, create, () => {});
  assert.deepStrictEqual(created, ['g1-APT_1-run', 'g1-APT_2-run', 'g1-APT_3-run', 'g1-APT_4-run', 'g1-APT_3-run']);
  assert.ok(C.APARTMENTS.every((a) => g.members[a].run_id === `run-${a}`));
  const b1 = C.runBody(g, 'APT_1');
  assert.strictEqual(b1.clock_start_utc, FORECAST.forecast_start);
  assert.strictEqual(b1.apartment_params.apartment_id, 'APT_1');
  assert.notDeepStrictEqual(C.runBody(g, 'APT_2').apartment_params, b1.apartment_params);
});

test('advisory delivery is tracked per run and a retry resends only the failed one, unchanged', async () => {
  const g = group();
  const sent = [];
  let down = true;
  const post = async (body) => { sent.push(body); if (body.run_id === 'run-1' && down) throw new Error('offline'); return { event_id: `ev-${body.run_id}` }; };
  assert.deepStrictEqual(await C.deliverAdvisory(g, FORECAST.peak_windows, post, () => {}), ['APT_2']);
  down = false;
  assert.deepStrictEqual(await C.deliverAdvisory(g, FORECAST.peak_windows, post, () => {}), []);
  assert.strictEqual(sent.length, 5);
  assert.deepStrictEqual(sent[4], sent[1]);
  assert.deepStrictEqual(sent[1].target_apartments, C.APARTMENTS);
});

test('a group advance steps each building once, and a second advance while one is in flight is refused', async () => {
  const s = fakeService();
  const g = group();
  const ctl = C.controller({ readClock: s.readClock, step: s.step, save: () => {} });
  const [a, b] = await Promise.all([ctl.advance(g, inputs), ctl.advance(g, inputs)]);
  assert.strictEqual(a.status, 'advanced');
  assert.strictEqual(b.status, 'busy');
  assert.deepStrictEqual(Object.values(s.clock), [1, 1, 1, 1]);
  assert.strictEqual(g.interval, 1);
  assert.strictEqual(g.pending, null);
});

test('a timed-out step is retried with the same request ID and payload and does not double-step', async () => {
  const s = fakeService();
  const g = group();
  s.faults.APT_2 = ['after'];  // the service stepped, the response was lost
  const ctl = C.controller({ readClock: s.readClock, step: s.step, save: () => {} });
  const r = await ctl.advance(g, inputs);
  assert.strictEqual(r.status, 'advanced');
  const apt2 = s.steps.filter((x) => x.apt === 'APT_2');
  assert.strictEqual(apt2.length, 2);
  assert.deepStrictEqual(apt2[0].body, apt2[1].body);
  assert.strictEqual(apt2[0].body.request_id, 'g1-APT_2-step-0');
  assert.strictEqual(s.clock.APT_2, 1);
});

test('partial failure: three advance, the fourth is shown behind and a retry steps only it, with the snapshot', async () => {
  const s = fakeService();
  const g = group();
  s.faults.APT_3 = ['before', 'before', 'before'];  // all attempts fail
  const ctl = C.controller({ readClock: s.readClock, step: s.step, save: () => {} });
  const r = await ctl.advance(g, { ...inputs, APT_3: { pump: 'ON', ev: 'OFF', fault: 'none' } });
  assert.strictEqual(r.status, 'partial');
  assert.deepStrictEqual(r.behind, ['APT_3']);
  assert.deepStrictEqual(s.clock, { APT_1: 1, APT_2: 1, APT_3: 0, APT_4: 1 });
  assert.strictEqual(g.interval, 0);
  const before = s.steps.length;
  // the operator changes the controls meanwhile; the retry must still send the snapshot
  const r2 = await ctl.advance(g, { ...inputs, APT_3: { pump: 'OFF', ev: 'OFF', fault: 'missing' } }, 'APT_3');
  assert.strictEqual(r2.status, 'advanced');
  const retried = s.steps.slice(before);
  assert.deepStrictEqual(retried.map((x) => x.apt), ['APT_3']);
  assert.deepStrictEqual(retried[0].body.actual_switches, { pump: 'ON', ev: 'OFF' });
  assert.strictEqual(retried[0].body.drop_observation, undefined);
  assert.deepStrictEqual(s.clock, { APT_1: 1, APT_2: 1, APT_3: 1, APT_4: 1 });
  assert.strictEqual(g.interval, 1);
});

test('unexpected clock movement stops the advance and sends nothing', async () => {
  const s = fakeService();
  const g = group();
  s.clock.APT_4 = 1;  // stepped by someone else
  const ctl = C.controller({ readClock: s.readClock, step: s.step, save: () => {} });
  const r = await ctl.advance(g, inputs);
  assert.strictEqual(r.status, 'unexpected');
  assert.deepStrictEqual(r.check.unexpected, ['APT_4']);
  assert.strictEqual(s.steps.length, 0);
  assert.strictEqual(g.pending, null);
});

test('fault and switch inputs map onto the existing step fields', () => {
  const g = group();
  assert.deepStrictEqual(C.stepBody(g, 'APT_4', 3, { pump: 'OFF', ev: 'ON', fault: 'missing' }),
    { request_id: 'g1-APT_4-step-3', actual_switches: { pump: 'OFF', ev: 'ON' }, drop_observation: true });
  assert.deepStrictEqual(C.stepBody(g, 'APT_1', 0, { pump: 'ON', ev: 'OFF', fault: 'charger-meter' }).drop_meter_channels, ['ev_group']);
  const st = { clock: { interval_index: 2 }, active_commitment: { option: { actions: [{ interval_index: 2, pump: 'ON', ev: 'EV_1' }] } } };
  assert.deepStrictEqual(C.planSwitches(st), { pump: 'ON', ev: 'ON' });
  assert.deepStrictEqual(C.planSwitches({ clock: { interval_index: 0 }, active_commitment: null }), { pump: 'OFF', ev: 'OFF' });
});

test('campus total only when all four have usable readings for the same interval and scope', () => {
  const obs = (i, pump, evKw, extra = {}) => ({ observation: { interval_index: i, quality: 'VALID', measurement_scope: 'branch', pump_model_kw: pump, ev_model_kw: evKw, ...extra } });
  const all = { APT_1: obs(3, 0, 3.3), APT_2: obs(3, 0, 0), APT_3: obs(3, 2.2, 3.3), APT_4: obs(3, 0, 3.3) };
  const t = C.campusTotal(all);
  assert.strictEqual(t.complete, true);
  assert.strictEqual(t.kw, 12.1);
  assert.strictEqual(C.campusTotal({ ...all, APT_2: obs(2, 0, 0) }).text, 'Partial data');
  assert.match(C.campusTotal({ ...all, APT_2: obs(2, 0, 0) }).reason, /different intervals/);
  assert.strictEqual(C.campusTotal({ ...all, APT_4: obs(3, null, null, { quality: 'STALE' }) }).complete, false);
  assert.strictEqual(C.campusTotal({ ...all, APT_1: obs(3, 0, 3.3, { measurement_scope: 'aggregate' }) }).complete, false);
  const missingCh = obs(3, 0, 3.3, { meter: { channels: [{ channel_id: 'pump', quality: 'VALID', average_power_model_kw: 0 }, { channel_id: 'ev_group', quality: 'MISSING', average_power_model_kw: null }] } });
  assert.strictEqual(C.campusTotal({ ...all, APT_3: missingCh }).complete, false);
  assert.strictEqual(C.campusTotal({ ...all, APT_3: { observation: null } }).complete, false);
});
