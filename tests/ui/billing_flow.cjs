// Phone-width (390 x 844) bill-analysis flow on the operator page: meter readings -> run report -> participating-load
// charges, plus the committed illustrative monthly scenario. Synthetic simulator runs only; no physical hardware.
//
//   NODE_PATH=<playwright-core>  NW_OPERATOR_CODE=...  NW_MANAGER_CODE=...
//   node tests/ui/run_isolated.cjs tests/ui/billing_flow.cjs
const path = require('node:path');
const assert = require('node:assert');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const OP = process.env.NW_OPERATOR_CODE;
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots', 'billing');
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };

const results = [];
async function check(name, fn) {
  try { await fn(); results.push(['PASS', name]); console.log(`PASS  ${name}`); } catch (e) {
    results.push(['FAIL', name]); console.log(`FAIL  ${name}\n      ${String(e.message).split('\n').slice(0, 8).join('\n      ')}`);
  }
}
let n = 0;
const rid = () => `bill-flow-${Date.now()}-${++n}`;
async function api(method, p, token, body) {
  const res = await fetch(`${BASE}/api/v1${p}`, { method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json();
  if (res.status !== 200) throw new Error(`${method} ${p} -> ${res.status} ${JSON.stringify(data)}`);
  return data;
}
const has = (t, s) => assert.ok(t.includes(s), `missing: ${s}\n---\n${t}`);
const lacks = (t, s) => assert.ok(!t.includes(s), `must not show: ${s}\n---\n${t}`);

(async () => {
  assert.ok(OP, 'set NW_OPERATOR_CODE');
  const opt = (await api('POST', '/demo/session', null, { role: 'operator', access_code: OP })).token;
  const mkRun = async () => (await api('POST', '/runs', opt, { request_id: rid(), clock_start_utc: '2026-08-18T18:30:00Z', apartment_params: {} })).run_id;
  const stepRun = (run, extra = {}) => api('POST', `/runs/${run}/step`, opt, { request_id: rid(), actual_switches: { pump: 'ON', ev: 'ON' }, ...extra });
  const runA = await mkRun();
  await stepRun(runA); await stepRun(runA);                 // 2 intervals: 2 x (2.2 + 3.3) kW x 0.25 h = 2.75 kWh
  const runB = await mkRun();
  await stepRun(runB); await stepRun(runB, { drop_observation: true });  // one interval without a reading

  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const page = await (await browser.newContext(PHONE)).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(`${BASE}/operator?run=${runA}`);
  await page.fill('#access-code', OP);
  await page.click('#signin-form button');
  await page.locator('#app').waitFor();
  const panel = async (run) => {
    const el = page.locator(`#billing-run[data-run-id="${run}"]`);
    await el.waitFor({ timeout: 15000 });
    return page.locator('#billing').innerText();
  };

  await check('meter -> report -> cost: itemized participating-load charges, labelled illustrative, no total while FPPCA is unknown', async () => {
    const t = await panel(runA);
    has(t, 'Illustrative simulation: synthetic readings, not measurements');
    has(t, 'Energy measured so far');
    has(t, '2.75 kWh');
    has(t, 'Energy charge'); has(t, 'Rs.15.95');                 // 2.75 kWh x Rs.5.80
    has(t, 'P&G surcharge'); has(t, 'Rs.0.96');                  // 2.75 x 35 paise = 0.9625 -> 0.96
    has(t, 'Fuel and power purchase cost adjustment');
    has(t, 'Electricity cost (pump and EV circuits)');
    has(await page.locator('[data-testid=amount-kind]').innerText(), "not the apartment's bill");
    has(t, 'Circuit energy charges, total: Not shown: at least one charge is unknown, so these are partial circuit charges.');
    lacks(t, 'Bill analysis');
    has(t, 'Fixed and demand charges belong to the whole connection');
    has(t, 'assumed for illustration');
    lacks(t, 'Rs.0.00 Unknown');
    const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
    // readable at phone width: no value column squeezed to a sliver (each value at least 40% of the panel)
    const narrow = await page.evaluate(() => [...document.querySelectorAll('#billing dl.facts dd')]
      .map((d) => d.getBoundingClientRect().width).filter((w) => w < 0.4 * document.getElementById('billing').clientWidth).length);
    assert.strictEqual(narrow, 0, `${narrow} value cell(s) squeezed narrower than 40% of the panel`);
    const height = await page.locator('#billing').evaluate((e) => e.getBoundingClientRect().height);
    assert.ok(height < 4000, `bill panel is ${Math.round(height)} px tall at phone width`);
    await page.locator('#billing').screenshot({ path: path.join(SHOTS, 'billing-1-run-and-scenario.png') });
  });

  await check('illustrative month: equal service stated, zero saving after overflow, unknown parts named, no ToD value', async () => {
    const t = await page.locator('[data-testid=billing-scenario]').innerText();
    has(t, 'Illustrative simulation: a month of baseline vs coordinated operation');
    has(t, 'Not an estimate for any real apartment; not a bill.');
    has(t, "Not the apartment's bill.");
    has(t, 'pump on/off state and its minimum on/off history carried across midnight');
    has(t, 'Headline difference (overflow artefact excluded)');
    has(t, 'Pump starts (wear trade-off)');
    has(t, '(partial: FPPCA unknown)');
    has(t, 'Yes: same water delivered');
    has(await page.locator('[data-testid=billing-saving]').innerText(), 'Zero');
    has(t, 'Rs.0.00: LT-1 has one flat energy rate.');
    has(t, 'Not estimable: needs the whole connection');
    has(t, 'Unknown: no verified monthly rate');
    has(t, 'not confidence intervals');
  });

  await check('a run with a missing interval: energy incomplete, no charges calculated, missing is not zero', async () => {
    await page.selectOption('#run-picker', runB);
    const t = await panel(runB);
    has(t, 'interval(s) without a usable reading (unknown, not zero)');
    has(t, 'no cost is calculated from the incomplete sum');
    assert.strictEqual(await page.locator('[data-testid=bill-lines]').count(), 0, 'charges shown for incomplete energy');
    await page.locator('#billing').screenshot({ path: path.join(SHOTS, 'billing-2-missing-interval.png') });
  });

  await check('switching runs: a late billing response for the previous run never replaces the current one', async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    await page.route(`**/api/v1/runs/${runA}/billing`, async (route) => { await gate; try { await route.continue(); } catch { /* cancelled */ } });
    await page.selectOption('#run-picker', runA);       // request for A is held
    await page.waitForTimeout(300);
    await page.selectOption('#run-picker', runB);       // switch on before A's answer
    await panel(runB);
    release();
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(150);
      assert.strictEqual(await page.locator(`#billing-run[data-run-id="${runA}"]`).count(), 0, 'run A billing reappeared after switching to B');
    }
    await page.unroute(`**/api/v1/runs/${runA}/billing`);
    has(await page.locator('#billing').innerText(), 'unknown, not zero');
  });

  await check('no uncaught page errors', async () => assert.deepStrictEqual(errors, []));
  await browser.close();
  const bad = results.filter((r) => r[0] === 'FAIL');
  console.log(`\n${results.length - bad.length} passed, ${bad.length} failed`);
  process.exitCode = bad.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
