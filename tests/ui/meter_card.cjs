// Phone-width (390 x 844) checks of the manager meter card and charging energy labels against a running service.
// The manager only reads the page; the operator side (run setup, simulated steps, lost channels) uses the API.
//
//   npm install --prefix <folder outside the repo> playwright-core
//   set NODE_PATH=<that folder>\node_modules
//   start the service (python app.py --port <port>) with its own NIVAWATT_DB, then:
//   set NW_BASE=http://127.0.0.1:<port>  NW_OPERATOR_CODE=...  NW_MANAGER_CODE=...
//   node tests/ui/meter_card.cjs
//
// Simulator runs only. The scheduler supports one charging circuit (one EV session charging at a time); two chargers
// drawing together are demonstrated by the device emulator (tests/core/test_meter.py), not here.
const path = require('node:path');
const assert = require('node:assert');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const OP = process.env.NW_OPERATOR_CODE;
const MG = process.env.NW_MANAGER_CODE;
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots');
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(['PASS', name]);
    console.log(`PASS  ${name}`);
  } catch (e) {
    results.push(['FAIL', name]);
    console.log(`FAIL  ${name}\n      ${String(e.message).split('\n').slice(0, 6).join('\n      ')}`);
  }
}

let n = 0;
const rid = () => `meter-card-${Date.now()}-${++n}`;
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
const noSideScroll = async (page) => {
  const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
};

(async () => {
  assert.ok(OP && MG, 'set NW_OPERATOR_CODE and NW_MANAGER_CODE');
  const opt = (await api('POST', '/demo/session', null, { role: 'operator', access_code: OP })).token;
  const mgt = (await api('POST', '/demo/session', null, { role: 'manager', apartment_id: 'APT_1', access_code: MG })).token;
  const run = (await api('POST', '/runs', opt, { request_id: rid(), clock_start_utc: '2026-08-18T18:30:00Z', apartment_params: {} })).run_id;
  const p = (await api('POST', '/requirements', mgt, { run_id: run, request_id: rid(), ev_sessions: [] })).proposal;
  await api('POST', `/proposals/${p.proposal_id}/decision`, mgt, { run_id: run, expected_revision: p.revision, decision: 'ACCEPT', option_id: 'o1', request_id: rid() });
  const stepRun = (extra) => api('POST', `/runs/${run}/step`, opt, { request_id: rid(), actual_switches: { pump: 'ON', ev: 'ON' }, ...extra });
  console.log(`run ${run}`);

  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const page = await (await browser.newContext(PHONE)).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(`${BASE}/manager?run=${run}`);
  await page.fill('#apartment-id', 'APT_1');
  await page.fill('#access-code', MG);
  await page.click('#signin-form button');
  await page.locator('#app').waitFor();
  const reload = async () => { await page.reload(); await page.locator('[data-testid=meter-card]').waitFor(); };
  const card = () => page.locator('[data-testid=meter-card]').innerText();

  await check('complete reading: one row per channel, grouped and simulated labels, energies, total', async () => {
    await stepRun({});
    await reload();
    const t = await card();
    for (const s of ['Meter meter-1', 'Simulated meter: synthetic readings, not a measurement', 'Pump', 'EV charger group',
      'Group of 2: charger-1, charger-2. One total for the group, not per charger or vehicle.', '2.2 kW (model)', '0.55 kWh (model)',
      '3.3 kW (model)', '0.825 kWh (model)', 'On time', '5.5 kW (model), 1.375 kWh (model) this interval. Sum of separate channels',
      'Pump power does not prove water was delivered']) has(t, s);
    await noSideScroll(page);
    await page.locator('[data-testid=meter-card]').screenshot({ path: path.join(SHOTS, 'meter-1-complete.png') });
  });

  await check('charging: measured group energy, inferred allocation, verified per EV not available', async () => {
    const t = await page.locator('#charging').innerText();
    assert.match(t, /EV charger group, measured\s*0\.83 kWh \(model\)/);
    assert.match(t, /Allocated to this EV \(inferred\)\s*0\.83 kWh \(model\)/);
    assert.match(t, /Verified for this EV\s*Not available/);
    has(t, 'no per-vehicle meter exists, so none is verified');
    assert.ok(!/Delivered, verified/.test(t), 'allocation must not be labelled verified');
  });

  await check('missing channel: Missing and Unknown (never zero), incomplete total, readable verdict', async () => {
    await stepRun({ drop_meter_channels: ['ev_group'] });
    await reload();
    const t = await card();
    has(t, 'Missing');
    has(t, 'Incomplete: no reading from EV charger group. The total is unknown, not zero.');
    const ev = await page.locator('[data-channel=ev_group]').innerText();
    assert.match(ev, /Power\s*Unknown/);
    assert.match(ev, /Energy this interval\s*Unknown/);
    has(await page.locator('#execution').innerText(), 'charging power is unknown');
    await noSideScroll(page);
    await page.locator('[data-testid=meter-card]').screenshot({ path: path.join(SHOTS, 'meter-2-missing-channel.png') });
  });

  await check('recovery: counter energy from the lost interval shown once, not assigned to an interval or EV', async () => {
    await stepRun({});
    await reload();
    const rec = await page.locator('[data-testid=meter-recovered]').innerText();
    has(rec, 'After 1 missing reading, the counter showed 0.825 kWh (model) used between 00:15 and 00:30 IST');
    has(rec, 'not given to any interval or vehicle');
    const ev = await page.locator('[data-channel=ev_group]').innerText();
    assert.match(ev, /Energy this interval\s*0\.825 kWh/);
    const t = await page.locator('#charging').innerText();
    assert.match(t, /Allocated to this EV \(inferred\)\s*1\.65 kWh/); // intervals 0 and 2 only
    assert.match(t, /Recovered after missing readings\s*0\.83 kWh \(model\), not assigned to a time or an EV/);
    await noSideScroll(page);
    await page.locator('[data-testid=meter-card]').screenshot({ path: path.join(SHOTS, 'meter-3-recovered.png') });
  });

  await check('no uncaught page errors', async () => assert.deepStrictEqual(errors, []));
  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL');
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  process.exitCode = failed.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
