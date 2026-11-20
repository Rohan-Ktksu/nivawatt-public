// Scheduling policy on the operator page (390 px): the causal demand-aware default for new runs, explicit advisory-only,
// the documented fallback shown prominently, and browser labels checked against the backend state. Synthetic only.
//
//   NODE_PATH=<playwright-core>  NW_OPERATOR_CODE=...  node tests/ui/run_isolated.cjs tests/ui/demand_policy_flow.cjs
const path = require('node:path');
const assert = require('node:assert');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const OP = process.env.NW_OPERATOR_CODE;
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots', 'demand_policy');
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };

const results = [];
async function check(name, fn) {
  try { await fn(); results.push(['PASS', name]); console.log(`PASS  ${name}`); } catch (e) {
    results.push(['FAIL', name]); console.log(`FAIL  ${name}\n      ${String(e.message).split('\n').slice(0, 8).join('\n      ')}`);
  }
}
let n = 0;
const rid = () => `demand-flow-${Date.now()}-${++n}`;
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
  const clock = '2026-08-18T18:30:00Z';
  const mk = async (body) => (await api('POST', '/runs', opt, { request_id: rid(), clock_start_utc: clock, ...body })).run_id;
  const plain = await mk({ apartment_params: {} });                              // policy omitted -> default
  const advisory = await mk({ scenario: 'flats100_advisory_only' });             // explicit advisory-only
  const demand = await mk({ scenario: 'flats100_demand_aware_experimental' });   // causal with whole-building history
  await api('POST', `/runs/${demand}/step`, opt, { request_id: rid() });
  const backend = async (run) => (await api('GET', `/state?run_id=${run}&apartment_id=APT_1`, opt)).scheduling;

  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const page = await (await browser.newContext(PHONE)).newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const open = async (run) => {
    await page.goto(`${BASE}/operator?run=${run}`);
    if (await page.locator('#access-code').isVisible().catch(() => false)) {
      await page.fill('#access-code', OP);
      await page.click('#signin-form button');
    }
    await page.locator('#app').waitFor();
    await page.locator('[data-testid=scheduling-policy]').waitFor({ timeout: 15000 });
    return page.locator('[data-testid=scheduling-policy]').innerText();
  };
  const matchesBackend = async (run, t) => {
    const s = await backend(run);
    has(t, s.requested_policy_label);
    has(t, s.effective_mode_label);
    return s;
  };

  await check('policy omitted: causal demand-aware requested by default; no whole-building measurement -> fallback shown prominently', async () => {
    const t = await open(plain);
    const s = await matchesBackend(plain, t);
    assert.strictEqual(s.requested_source, 'default');
    has(t, 'Requested policy'); has(t, 'Effective planning mode');
    has(t, 'Experimental: validated in synthetic scenarios'); has(t, '(default for new runs)');
    has(t, 'Fallback in use: advisory-only planning.'); has(t, 'no whole-building measurement is configured for this run');
    has(t, 'None: no whole-building measurement');
    const banner = await page.locator('[data-testid=planning-fallback]').boundingBox();
    const first = await page.locator('[data-testid=scheduling-policy] dl').first().boundingBox();
    assert.ok(banner && first && banner.y < first.y, 'the fallback reason must come before the details');
    const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
    await page.locator('[data-testid=scheduling-policy]').screenshot({ path: path.join(SHOTS, 'demand-0-default-no-measurement.png') });
  });

  await check('explicit advisory-only selection is respected and shown as chosen', async () => {
    const t = await open(advisory);
    const s = await matchesBackend(advisory, t);
    assert.strictEqual(s.requested_policy, 'advisory_only');
    has(t, 'Advisory-only scheduler (chosen explicitly)'); has(t, 'Advisory-only planning');
    lacks(t, 'Fallback in use');
  });

  await check('sufficient fresh history: causal demand-aware planning with planning-target and fallback caveats', async () => {
    const t = await open(demand);
    await matchesBackend(demand, t);
    has(t, 'Demand-aware planning'); has(t, 'Normal');
    has(t, 'kVA, a planning target'); has(t, 'not an electrical safety limit');
    has(t, 'does not guarantee avoiding demand charges'); has(t, "never alters an accepted plan");
    has(t, 'authorized data interface');
    lacks(t, 'Fallback in use');
    await page.locator('[data-testid=scheduling-policy]').screenshot({ path: path.join(SHOTS, 'demand-1-normal.png') });
  });

  await check('stale whole-building readings: documented fallback with reduced confidence, reason shown prominently', async () => {
    await api('POST', `/runs/${demand}/step`, opt, { request_id: rid(), drop_whole_connection: true });
    await api('POST', `/runs/${demand}/step`, opt, { request_id: rid(), drop_whole_connection: true });
    const t = await open(demand);
    await matchesBackend(demand, t);
    has(t, 'Fallback in use: advisory-only planning.'); has(t, 'whole-building readings stale');
    has(t, 'Reduced'); has(t, 'None while in fallback');
    await page.locator('[data-testid=scheduling-policy]').screenshot({ path: path.join(SHOTS, 'demand-2-fallback.png') });
  });

  await check('the scenario list keeps advisory-only and the demand-aware preset as explicit choices', async () => {
    await open(plain);
    const opts = await page.locator('#scenario option').evaluateAll((os) => os.map((o) => o.value));
    for (const v of ['branch', 'flats100_advisory_only', 'flats100_demand_aware_experimental']) assert.ok(opts.includes(v), v);
  });

  await check('no page errors', async () => assert.deepStrictEqual(errors, []));
  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL').length;
  console.log(`\n${results.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
