// Phone-width (390 x 844) check of meter provenance labels (issues #24, #25) against a running service.
// Every reading here is a SYNTHETIC FIXTURE posted through POST /telemetry. Samples marked source_kind=hardware exercise
// the hardware-labelled display states; they are not physical measurements and validate no hardware.
//
//   npm install --prefix <folder outside the repo> playwright-core
//   set NODE_PATH=<that folder>\node_modules
//   start the service (python app.py --port <port>) with its own NIVAWATT_DB and a NIVAWATT_CODES file that has
//   device_tokens {"bench-1": "<token>"}, then:
//   set NW_BASE=http://127.0.0.1:<port>  NW_OPERATOR_CODE=...  NW_MANAGER_CODE=...  NW_DEVICE_TOKEN=<token>
//   node tests/ui/meter_provenance.cjs
const path = require('node:path');
const assert = require('node:assert');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const OP = process.env.NW_OPERATOR_CODE;
const MG = process.env.NW_MANAGER_CODE;
const DEV = process.env.NW_DEVICE_TOKEN;
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots', 'provenance-fixtures');
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };
const SCALED = 'Scaled from bench measurements; model values, not field measurements.';

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(['PASS', name]);
    console.log(`PASS  ${name}`);
  } catch (e) {
    results.push(['FAIL', name]);
    console.log(`FAIL  ${name}\n      ${String(e.message).split('\n').slice(0, 8).join('\n      ')}`);
  }
}

let n = 0;
const rid = () => `meter-prov-${Date.now()}-${++n}`;
async function api(method, p, token, body) {
  const res = await fetch(`${BASE}/api/v1${p}`, {
    method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (res.status !== 200) throw new Error(`${method} ${p} -> ${res.status} ${JSON.stringify(data)}`);
  return data;
}
const has = (text, s) => assert.ok(text.includes(s), `missing: ${s}\n---\n${text}`);
const lacks = (text, s) => assert.ok(!text.includes(s), `must not show: ${s}\n---\n${text}`);
const noSideScroll = async (page) => {
  const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
};

// 16 fixture samples 60 s apart on the device clock: pump 2.2 kW (0.0366667 kWh/min), chargers off.
function samples(run, kind, { dropPump = false, tank = 4500 } = {}) {
  return Array.from({ length: 16 }, (_, k) => {
    const pumpKwh = 1.0 + 0.0366667 * k;
    const channels = [{ channel_id: 'ev_group', devices: ['charger-1', 'charger-2'], power_w: 0, energy_kwh: 0.5 }];
    if (!dropPump) channels.unshift({ channel_id: 'pump', devices: ['pump-1'], power_w: 2200, energy_kwh: pumpKwh });
    return { run_id: run, apartment_id: 'APT_1', device_id: 'bench-1', device_session_id: `fx-${run}`, seq_num: k + 1,
      device_monotonic_ms: 60000 * k, source_kind: kind, tank_volume_l: tank, pump_power_w: 2200, ev_load_power_w: 0,
      aggregate_power_w: 2200, meter: { meter_id: 'fixture-meter', clock: 'device', meter_session: `fx-${run}`, channels } };
  });
}

(async () => {
  assert.ok(OP && MG && DEV, 'set NW_OPERATOR_CODE, NW_MANAGER_CODE and NW_DEVICE_TOKEN');
  const opt = (await api('POST', '/demo/session', null, { role: 'operator', access_code: OP })).token;
  const mgt = (await api('POST', '/demo/session', null, { role: 'manager', apartment_id: 'APT_1', access_code: MG })).token;
  async function fixtureRun(kind, scale, opts, volume = 1) {
    const run = (await api('POST', '/runs', opt, { request_id: rid(), clock_start_utc: '2026-08-18T18:30:00Z',
      apartment_params: { observation_source: 'telemetry', telemetry_power_scale: scale, telemetry_volume_scale: volume } })).run_id;
    const p = (await api('POST', '/requirements', mgt, { run_id: run, request_id: rid(), ev_sessions: [] })).proposal;
    await api('POST', `/proposals/${p.proposal_id}/decision`, mgt, { run_id: run, expected_revision: p.revision, decision: 'ACCEPT', option_id: 'o1', request_id: rid() });
    for (const s of samples(run, kind, opts)) await api('POST', '/telemetry', DEV, s);
    await api('POST', `/runs/${run}/step`, opt, { request_id: rid() });
    console.log(`${kind} power x${scale} volume x${volume}${opts?.dropPump ? ' (pump channel missing)' : ''}: run ${run}`);
    return run;
  }
  const runs = {
    synthetic1: await fixtureRun('synthetic', 1),
    hardware1: await fixtureRun('hardware', 1),
    scaled2: await fixtureRun('hardware', 2),
    scaledMissing: await fixtureRun('hardware', 2, { dropPump: true }),
    volumeOnly: await fixtureRun('hardware', 1, { tank: 450 }, 10),
  };

  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const errors = [];
  const mgr = await (await browser.newContext(PHONE)).newPage();
  mgr.on('pageerror', (e) => errors.push(e.message));
  await mgr.goto(`${BASE}/manager?run=${runs.synthetic1}`);
  await mgr.fill('#apartment-id', 'APT_1');
  await mgr.fill('#access-code', MG);
  await mgr.click('#signin-form button');
  await mgr.locator('#app').waitFor();
  const op = await (await browser.newContext(PHONE)).newPage();
  op.on('pageerror', (e) => errors.push(e.message));
  await op.goto(`${BASE}/operator?run=${runs.synthetic1}`);
  await op.fill('#access-code', OP);
  await op.click('#signin-form button');
  await op.locator('#app').waitFor();

  const card = async (run) => {
    await mgr.goto(`${BASE}/manager?run=${run}`);
    await mgr.locator('[data-testid=meter-card]').waitFor();
    return mgr.locator('[data-testid=meter-card]').innerText();
  };
  const outcome = async (run) => {
    await op.goto(`${BASE}/operator?run=${run}`);
    const basis = op.locator(`#outcome-basis[data-run-id="${run}"]`); // this run's completed report, not a stale one
    await basis.waitFor();
    return basis.innerText();
  };
  const shot = (name) => mgr.locator('[data-testid=meter-card]').screenshot({ path: path.join(SHOTS, name) });

  await check('1. synthetic scale-1 run: labelled simulated in card and report, no raw or scale rows', async () => {
    const t = await card(runs.synthetic1);
    has(t, 'Simulated meter: synthetic readings, not a measurement');
    lacks(t, 'Raw'); lacks(t, 'Scale'); lacks(t, 'measured');
    const r = await outcome(runs.synthetic1);
    has(r, 'Synthetic simulation, not a measurement');
    has(r, 'emulator (synthetic readings, not measurements)');
    lacks(r, 'measured by hardware');
    await noSideScroll(mgr);
    await shot('fixture-1-synthetic-scale1.png');
  });

  await check('2. hardware-labelled scale-1 fixture: unscaled, measured label, model values once with a scale-1 note', async () => {
    const t = await card(runs.hardware1);
    has(t, "Physical meter: unscaled measurements (model values equal the meter's own readings)");
    has(t, "Electrical measurements: unscaled (×1). Model values are the meter's own readings.");
    has(t, 'Water volume: unscaled (×1).');
    has(t, '2.2 kW (model)'); has(t, '0.55 kWh (model)');
    lacks(t, 'Raw energy this interval'); lacks(t, 'Raw counter');
    const r = await outcome(runs.hardware1);
    has(r, 'Physical measurement, unscaled');
    has(r, 'measured by hardware (unscaled)');
    await noSideScroll(mgr);
    await shot('fixture-2-hardware-label-scale1.png');
  });

  await check('3+4. scaled fixture: factor, raw and model values distinct; scale applied once; totals unchanged', async () => {
    const t = await card(runs.scaled2);
    has(t, SCALED);
    has(t, 'Electrical measurements: model-scaled (×2). Model kWh = raw counter kWh × 2. Time is not scaled.');
    has(t, 'Water volume: unscaled (×1).');
    const pump = await mgr.locator('[data-channel=pump]').innerText();
    assert.match(pump, /Power\s*4\.4 kW \(model\)/);
    assert.match(pump, /Energy this interval\s*1\.1 kWh \(model\)/);
    assert.match(pump, /Raw energy this interval\s*0\.55 kWh \(raw, before scale\)/);
    assert.match(pump, /Raw counter\s*1\.55 kWh \(raw, before scale\)/);
    has(t, '4.4 kW (model), 1.1 kWh (model) this interval'); // total = scaled channels, not raw + model
    const label = 'Bench measurements: electrical measurements model-scaled (×2); water volume unscaled (×1). Scaled values are model values, not field measurements.';
    assert.strictEqual((await api('GET', `/runs/${runs.scaled2}/report`, opt)).provenance.label, label);
    const r = await outcome(runs.scaled2);
    has(r, label); has(r, 'electrical measurements model-scaled (x2); water volume unscaled (x1)');
    lacks(r, 'measured by hardware');
    await noSideScroll(mgr);
    await shot('fixture-3-scaled-x2.png');
  });

  await check('5+6. missing channel on a scaled run: raw unavailable (not 0), Unknown model values, incomplete total', async () => {
    const t = await card(runs.scaledMissing);
    const pump = await mgr.locator('[data-channel=pump]').innerText();
    has(pump, 'Missing');
    assert.match(pump, /Raw meter reading\s*Raw measurement unavailable/);
    assert.match(pump, /Power\s*Unknown/);
    assert.ok(!/Raw[^\n]*\n?\s*0 kWh/.test(pump), 'raw must not be shown as zero');
    has(t, 'Incomplete: no reading from Pump. The total is unknown, not zero.');
    await noSideScroll(mgr);
    await shot('fixture-4-scaled-missing-channel.png');
  });

  await check('volume-only scaling: electrical shown unscaled (×1), water volume model-scaled (×10), never "measured" overall', async () => {
    const t = await card(runs.volumeOnly);
    has(t, 'Measured'); lacks(t, 'Scaled');
    has(t, "Physical meter: unscaled measurements (model values equal the meter's own readings)");
    has(t, "Electrical measurements: unscaled (×1). Model values are the meter's own readings.");
    has(t, 'Water volume: model-scaled (×10).');
    has(t, '2.2 kW (model)'); lacks(t, 'Raw energy this interval');
    const label = 'Bench measurements: electrical measurements unscaled (×1); water volume model-scaled (×10). Scaled values are model values, not field measurements.';
    assert.strictEqual((await api('GET', `/runs/${runs.volumeOnly}/report`, opt)).provenance.label, label);
    has(await mgr.locator('#readings').innerText(), label); // the manager's Source line names both scales too
    const r = await outcome(runs.volumeOnly);
    has(r, label); lacks(r, SCALED); lacks(r, 'electrical measurements model-scaled');
    has(r, 'electrical measurements unscaled (x1); water volume model-scaled (x10)');
    lacks(r, 'measured by hardware');
    await noSideScroll(mgr);
    await shot('fixture-5-volume-only-x10.png');
  });

  await check('7. card label and report label agree for every source kind', async () => {
    for (const run of Object.values(runs)) {
      const st = await api('GET', `/state?run_id=${run}&apartment_id=APT_1`, mgt);
      const rep = (await api('GET', `/runs/${run}/report`, opt)).provenance;
      assert.ok(st.observation?.meter, `no meter on ${run}`);
      const m = st.observation.meter;
      if (rep.source_kind !== 'scaled_hardware') { assert.strictEqual(m.label, rep.label, run); continue; }
      // scaled: the report's short label states exactly the card's two scale facts
      const word = (f) => (f === 1 ? 'unscaled (×1)' : `model-scaled (×${f})`);
      has(rep.label, `electrical measurements ${word(m.power_scale)}; water volume ${word(m.volume_scale)}.`);
      has(await card(run), `Electrical measurements: ${word(m.power_scale)}.`);
      has(await card(run), `Water volume: ${word(m.volume_scale)}.`);
      has(await outcome(run), rep.label); // the operator page shows the service's label verbatim
    }
  });

  await check('8. no uncaught page errors', async () => assert.deepStrictEqual(errors, []));
  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL');
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  process.exitCode = failed.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
