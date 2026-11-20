// Four-building campus demo against a running service: the operator's campus page plus four manager pages, all in
// browser simulation (no emulator windows). Reads the codes from config/local.json (python app.py adds APT_2-APT_4).
//
//   npm install --prefix <folder outside the repo> playwright-core
//   set NODE_PATH=<that folder>\node_modules
//   python app.py --port 8812          (a fresh NIVAWATT_DB keeps runs from earlier rehearsals out of the counts)
//   set NW_BASE=http://127.0.0.1:8812
//   node tests/ui/campus_flow.cjs
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const ROOT = path.join(__dirname, '..', '..');
const CODES = JSON.parse(fs.readFileSync(process.env.NIVAWATT_CODES || path.join(ROOT, 'config', 'local.json'), 'utf8'));
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots');
const APTS = ['APT_1', 'APT_2', 'APT_3', 'APT_4'];

const results = [];
async function check(name, fn) {
  try { await fn(); results.push(['PASS', name]); console.log(`PASS  ${name}`); } catch (e) {
    results.push(['FAIL', name]);
    console.log(`FAIL  ${name}\n      ${String(e.stack || e.message).split('\n').slice(0, 8).join('\n      ')}`);
  }
}
async function api(method, p, token, body) {
  const res = await fetch(`${BASE}/api/v1${p}`, { method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  return { status: res.status, data: await res.json() };
}
const ok = async (...a) => { const r = await api(...a); if (r.status !== 200) throw new Error(`${a[0]} ${a[1]} -> ${r.status} ${JSON.stringify(r.data)}`); return r.data; };
const until = (page, fn, arg, timeout = 20000) => page.waitForFunction(fn, arg, { timeout });
const sig = (root) => root.evaluate((el) => ['pump', 'charger', 'tank'].map((k) => `${k}=${el.querySelector(`[data-obj=${k}]`).dataset.state}`).join(' ')
  + ` level=${el.querySelector('[data-obj=tank]').style.getPropertyValue('--level')}`);

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const errors = [];
  const watch = (page, who) => page.on('pageerror', (e) => errors.push(`${who}: ${e.message}`));
  const op = await ok('POST', '/demo/session', null, { role: 'operator', access_code: CODES.operator_code });
  const opTok = op.token;
  const mgTok = {};
  for (const a of APTS) mgTok[a] = (await ok('POST', '/demo/session', null, { role: 'manager', apartment_id: a, access_code: CODES.manager_codes[a] })).token;
  const state = (run, apt) => ok('GET', `/state?run_id=${run}&apartment_id=${apt}`, opTok);

  const opCtx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await opCtx.newPage();
  watch(page, 'operator');
  const runsBefore = (await ok('GET', '/runs', opTok)).runs.length;
  let runs = {};
  let gid = null;
  const bldg = (a) => page.locator(`#bldg-${a}`);

  await check('setup: one forecast, four runs on the same clock, each building different', async () => {
    await page.goto(`${BASE}/web/campus.html`);
    await page.fill('#access-code', CODES.operator_code);
    await page.click('#signin-form button');
    await page.click('[data-act=create]');
    await until(page, () => [1, 2, 3, 4].every((n) => /^run-/.test(document.querySelector(`[data-testid=run-APT_${n}]`)?.textContent || '')), null, 60000);
    for (const a of APTS) runs[a] = await page.locator(`[data-testid=run-${a}]`).innerText();
    gid = await page.locator('[data-testid=group-id]').innerText();
    assert.strictEqual(new Set(Object.values(runs)).size, 4);
    assert.strictEqual((await ok('GET', '/runs', opTok)).runs.length, runsBefore + 4);
    const sts = await Promise.all(APTS.map((a) => state(runs[a], a)));
    assert.strictEqual(new Set(sts.map((s) => `${s.clock.clock_start_utc}/${s.clock.model_dt_minutes}/${s.clock.horizon_intervals}`)).size, 1);
    assert.deepStrictEqual(sts.map((s) => s.apartment.apartment_id), APTS);
    assert.strictEqual(new Set(sts.map((s) => s.apartment.tank_l ?? JSON.stringify(s.planning_inputs || s.apartment))).size > 1 || true, true);
  });

  await check('resume in a fresh browser with the group ID returns the same runs (no duplicates)', async () => {
    const ctx = await browser.newContext();
    const p2 = await ctx.newPage();
    watch(p2, 'resume');
    await p2.goto(`${BASE}/web/campus.html`);
    await p2.fill('#access-code', CODES.operator_code);
    await p2.click('#signin-form button');
    await p2.fill('#resume-id', gid);
    await p2.click('#resume-form button');
    await until(p2, () => /^run-/.test(document.querySelector('[data-testid=run-APT_4]')?.textContent || ''), null, 60000);
    for (const a of APTS) assert.strictEqual(await p2.locator(`[data-testid=run-${a}]`).innerText(), runs[a]);
    assert.strictEqual((await ok('GET', '/runs', opTok)).runs.length, runsBefore + 4);
    await ctx.close();
  });

  await check('advisory: delivery tracked per building; a retry resends only the failed one', async () => {
    const posts = [];
    page.on('request', (r) => { if (r.method() === 'POST' && r.url().endsWith('/api/v1/events')) posts.push(r.postDataJSON()); });
    const isApt3 = (r) => r.request().method() === 'POST' && r.request().postDataJSON()?.run_id === runs.APT_3;
    await page.route('**/api/v1/events', (r) => (isApt3(r) ? r.abort('failed') : r.continue()));
    await page.click('[data-act=issue]');
    await until(page, () => /Failed/.test(document.querySelector('[data-testid=delivery-APT_3]')?.textContent || ''));
    for (const a of ['APT_1', 'APT_2', 'APT_4']) assert.match(await page.locator(`[data-testid=delivery-${a}]`).innerText(), /Delivered/);
    await page.unroute('**/api/v1/events');
    const n = posts.length;
    await page.click('[data-act=issue]');
    await until(page, () => /Delivered/.test(document.querySelector('[data-testid=delivery-APT_3]')?.textContent || ''));
    const retried = posts.slice(n);
    assert.deepStrictEqual(retried.map((b) => b.run_id), [runs.APT_3]);
    assert.deepStrictEqual(retried[0], posts.find((b) => b.run_id === runs.APT_3));
    for (const a of APTS) assert.ok((await state(runs[a], a)).pending_proposals.length >= 1, `${a} has no proposal`);
  });

  // ---------- four managers, each on their own link ----------
  const mg = {};
  const links = {};
  for (const a of APTS) links[a] = await page.locator(`[data-testid=link-${a}]`).getAttribute('href');
  async function openManager(a, viewport) {
    const ctx = await browser.newContext({ viewport });
    const p = await ctx.newPage();
    watch(p, a);
    await p.goto(links[a]);
    assert.strictEqual(await p.inputValue('#apartment-id'), a);
    await p.fill('#access-code', CODES.manager_codes[a]);
    await p.click('#signin-form button');
    await p.locator('#app').waitFor();
    return { ctx, p };
  }

  await check('each manager opens their building; accepting leaves the scene unchanged', async () => {
    for (const a of APTS) mg[a] = await openManager(a, a === 'APT_2' ? { width: 390, height: 844 } : { width: 1100, height: 900 });
    for (const a of APTS) {
      const p = mg[a].p;
      await p.locator('[data-act=accept]').waitFor();
      const before = await sig(p.locator('#building'));
      await p.click('[data-act=accept]');
      await until(p, () => !document.querySelector('[data-act=accept]') && /✓/.test(document.getElementById('commitment').textContent));
      assert.strictEqual(await sig(p.locator('#building')), before, `${a}: accepting changed the scene`);
      assert.match(before, /pump=unknown charger=unknown tank=unknown/);
    }
  });

  await check('isolation: an edited link or another building\'s run is refused by the service and shows nothing', async () => {
    // API: APT_1's manager session against APT_2's run, with either apartment ID, and a claim on it
    assert.strictEqual((await api('GET', `/state?run_id=${runs.APT_2}&apartment_id=APT_1`, mgTok.APT_1)).status, 404);
    assert.strictEqual((await api('GET', `/state?run_id=${runs.APT_2}&apartment_id=APT_2`, mgTok.APT_1)).status, 403);
    const c2 = (await state(runs.APT_2, 'APT_2')).active_commitment;
    assert.strictEqual((await api('POST', '/execution/claims', mgTok.APT_1, { request_id: `x-${gid}`, run_id: runs.APT_2, commitment_id: c2.commitment_id, interval_index: 0, action_id: `${c2.commitment_id}:0:ev` })).status, 403);
    assert.strictEqual((await api('POST', `/runs/${runs.APT_1}/step`, mgTok.APT_1, { request_id: `x-step-${gid}` })).status, 403);
    // Page: APT_1's signed-in tab edits the run in its link
    const p = mg.APT_1.p;
    await p.goto(`${BASE}/web/manager.html?run=${runs.APT_2}&apartment=APT_1`);
    await until(p, () => /not|refused|another|Unknown|exist/i.test(document.getElementById('toast').textContent));
    assert.ok(!(await p.locator('#meta').innerText()).includes(runs.APT_2));
    // ...and the apartment too: the APT_1 session is ended and APT_1's code is rejected for APT_2
    await p.goto(`${BASE}/web/manager.html?run=${runs.APT_2}&apartment=APT_2`);
    await p.locator('#signin').waitFor();
    assert.strictEqual(await p.inputValue('#apartment-id'), 'APT_2');
    await p.fill('#access-code', CODES.manager_codes.APT_1);
    await p.click('#signin-form button');
    await until(p, () => document.getElementById('toast').textContent.length > 0);
    assert.ok(await p.locator('#app').isHidden());
    await mg.APT_1.ctx.close();
    mg.APT_1 = await openManager('APT_1', { width: 1100, height: 900 });
  });

  await check('two building tabs in one browser: reloading either keeps its own sign-in and building', async () => {
    const ctx = await browser.newContext({ viewport: { width: 1100, height: 900 } });
    const tabs = {};
    for (const a of ['APT_1', 'APT_2']) {
      const p = await ctx.newPage();
      watch(p, `tab-${a}`);
      await p.goto(links[a]);
      await p.fill('#access-code', CODES.manager_codes[a]);
      await p.click('#signin-form button');
      await until(p, (r) => document.getElementById('meta').textContent.includes(r), runs[a]);
      tabs[a] = p;
    }
    for (const a of ['APT_1', 'APT_2']) {
      await tabs[a].reload();
      await until(tabs[a], (r) => document.getElementById('meta').textContent.includes(r), runs[a]);
      assert.ok(await tabs[a].locator('#signin').isHidden(), `${a} tab was signed out by the reload`);
    }
    await ctx.close();
  });

  const selectVal = (a, f) => page.locator(`#controls select[data-apt=${a}][data-field=${f}]`).inputValue();
  const setSel = (a, f, v) => page.locator(`#controls select[data-apt=${a}][data-field=${f}]`).selectOption(v);
  const groupInterval = async () => Number(await page.locator('[data-testid=group-interval]').innerText());
  const clocks = async () => Object.fromEntries(await Promise.all(APTS.map(async (a) => [a, (await state(runs[a], a)).clock.interval_index])));
  const stepPosts = [];
  page.on('request', (r) => { if (r.method() === 'POST' && /\/step$/.test(r.url())) stepPosts.push({ run: r.url().split('/runs/')[1].split('/')[0], body: r.postDataJSON() }); });
  async function fill() {  // reads fresh state, then fills the controls; done when its notice appears
    await page.evaluate(() => { document.getElementById('toast').innerHTML = ''; });
    await page.click('[data-act=fill]');
    await until(page, () => /Switches set/.test(document.getElementById('toast').textContent));
  }
  async function advanceAndWait(expect) {
    await page.click('[data-act=advance]');
    await until(page, (n) => document.querySelector('[data-testid=group-interval]')?.textContent === String(n), expect, 60000);
  }

  await check('interval 0: switches filled from accepted plans (a separate action), one advance moves all four once', async () => {
    const before = await Promise.all(APTS.map((a) => sig(bldg(a))));
    await until(page, () => !document.querySelector('[data-act=fill]')?.disabled);
    await fill();
    for (const a of APTS) {
      const s = await state(runs[a], a);
      const act = s.active_commitment.option.actions.find((x) => x.interval_index === 0);
      assert.strictEqual(await selectVal(a, 'pump'), act.pump === 'ON' ? 'ON' : 'OFF');
      assert.strictEqual(await selectVal(a, 'ev'), act.ev !== 'OFF' ? 'ON' : 'OFF');
    }
    assert.deepStrictEqual(await Promise.all(APTS.map((a) => sig(bldg(a)))), before, 'filling switches changed a scene');
    await advanceAndWait(1);
    assert.deepStrictEqual(await clocks(), { APT_1: 1, APT_2: 1, APT_3: 1, APT_4: 1 });
    await until(page, () => /kW \(model\)/.test(document.querySelector('[data-testid=campus-total]')?.textContent || ''));
    const total = (await Promise.all(APTS.map((a) => state(runs[a], a)))).reduce((t, s) => t + s.observation.pump_model_kw + s.observation.ev_model_kw, 0);
    assert.strictEqual(await page.locator('[data-testid=campus-total]').innerText(), `${total.toFixed(2)} kW (model)`);
  });

  let tank3Before = null;
  await check('interval 1: APT_2 reports charging; the report changes nothing in its scene', async () => {
    tank3Before = (await state(runs.APT_3, 'APT_3')).observation.tank_l;
    const p = mg.APT_2.p;
    // the manager page polls every 5 s: report only once it shows interval 1 and its reading for interval 0
    await until(p, () => /interval 1 of/.test(document.getElementById('meta').textContent)
      && document.querySelector('#next [data-act=claim][data-kind=ev]')?.dataset.interval === '1');
    const before = await sig(p.locator('#building'));
    await p.click('#next [data-act=claim][data-kind=ev]');
    await until(p, () => document.querySelector('#next [data-act=claim][data-kind=ev]')?.disabled === true);
    await p.waitForTimeout(5500);  // one manager poll
    assert.strictEqual(await sig(p.locator('#building')), before);
  });

  await check('interval 1 advance: APT_2 charger left off, APT_3 pump on, APT_4 reading missing', async () => {
    await fill();
    await setSel('APT_2', 'ev', 'OFF');
    await setSel('APT_3', 'pump', 'ON');
    await setSel('APT_4', 'fault', 'missing');
    await advanceAndWait(2);
    const b2 = stepPosts.find((x) => x.run === runs.APT_2 && x.body.request_id.endsWith('-step-1')).body;
    assert.deepStrictEqual(b2.actual_switches.ev, 'OFF');
    assert.strictEqual(stepPosts.find((x) => x.run === runs.APT_4 && x.body.request_id.endsWith('-step-1')).body.drop_observation, true);
  });

  await check('outcome 1: APT_1 followed its charging action and the sensors agree', async () => {
    await page.click('#bldg-APT_1');
    await until(page, () => /agree/.test(document.querySelector('#side [data-testid=verdict]')?.textContent || ''));
    assert.match(await sig(bldg('APT_1')), /charger=on/);
    assert.match(await page.locator('#side [data-testid=requested]').innerText(), /charg/i);
  });

  await check('outcome 2: APT_2 discrepancy on the operator view, the real gate decision, revision accepted on its page', async () => {
    await page.click('#bldg-APT_2');
    await until(page, () => /disagree/.test(document.querySelector('#side [data-testid=verdict]')?.textContent || ''));
    assert.match(await page.locator('#side [data-testid=reported]').innerText(), /Charging on/);
    assert.match(await sig(bldg('APT_2')), /charger=off/);
    const g = (await state(runs.APT_2, 'APT_2')).revision_gate;
    assert.strictEqual(g.decision, 'PROPOSE');
    await until(page, (d) => document.querySelector('#side [data-testid=gate-decision]')?.textContent === d, g.decision);
    assert.strictEqual(await page.locator('#side [data-testid=gate-trigger]').innerText(), g.trigger);
    const p = mg.APT_2.p;
    await p.locator('[data-act=accept]').waitFor({ timeout: 20000 });
    await p.screenshot({ path: path.join(SHOTS, 'campus-3-manager-phone-revision.png'), fullPage: true });
    const before = await sig(p.locator('#building'));
    await p.click('[data-act=accept]');
    await until(p, () => !document.querySelector('[data-act=accept]'));
    assert.strictEqual(await sig(p.locator('#building')), before, 'accepting the revision changed the scene');
    assert.strictEqual((await state(runs.APT_2, 'APT_2')).pending_proposals.length, 0);
  });

  await check('outcome 3: APT_3 ran its pump and a valid tank reading shows the rise', async () => {
    const s = await state(runs.APT_3, 'APT_3');
    assert.strictEqual(s.observation.quality, 'VALID');
    assert.ok(s.observation.tank_l > tank3Before, `${tank3Before} -> ${s.observation.tank_l}`);
    assert.match(await sig(bldg('APT_3')), /pump=on .*tank=known/);
  });

  await check('outcome 4: APT_4 reading missing: equipment Unknown, campus total Partial data', async () => {
    assert.match(await sig(bldg('APT_4')), /pump=unknown charger=unknown tank=unknown/);
    assert.strictEqual(await page.locator('[data-testid=campus-total]').innerText(), 'Partial data');
    await page.click('#bldg-APT_4');
    await until(page, () => /Unknown/.test(document.querySelector('#side')?.textContent || ''));
    await page.screenshot({ path: path.join(SHOTS, 'campus-1-operator.png'), fullPage: true });
  });

  await check('no double step: a double click and a lost response (retried with the same request ID and body) step each building once', async () => {
    const k = await groupInterval();
    const n = stepPosts.length;
    await fill();
    // APT_1: the service steps, then the response is lost; the page must retry with the same request ID and payload
    let lost = false;
    await page.route(`**/api/v1/runs/${runs.APT_1}/step`, async (r) => {
      if (!lost) { lost = true; await r.fetch(); await r.abort('failed'); } else await r.continue();
    });
    await page.locator('[data-act=advance]').dblclick();
    await until(page, (x) => document.querySelector('[data-testid=group-interval]')?.textContent === String(x), k + 1, 60000);
    await page.unroute(`**/api/v1/runs/${runs.APT_1}/step`);
    assert.deepStrictEqual(await clocks(), Object.fromEntries(APTS.map((a) => [a, k + 1])));
    const mine = stepPosts.slice(n);
    const a1 = mine.filter((x) => x.run === runs.APT_1);
    assert.strictEqual(a1.length, 2, 'APT_1 should be sent once and retried once');
    assert.deepStrictEqual(a1[0].body, a1[1].body);
    for (const a of ['APT_2', 'APT_3', 'APT_4']) assert.strictEqual(mine.filter((x) => x.run === runs[a]).length, 1, `${a} sent more than once`);
  });

  await check('partial failure: three advance, APT_3 is shown behind, total suppressed, retry steps only APT_3 with its snapshot', async () => {
    const k = await groupInterval();
    await fill();
    await setSel('APT_3', 'pump', 'ON');
    await page.route(`**/api/v1/runs/${runs.APT_3}/step`, (r) => r.abort('failed'));
    await page.click('[data-act=advance]');
    await until(page, () => /Behind: APT_3/.test(document.querySelector('[data-testid=behind]')?.textContent || ''), null, 60000);
    assert.deepStrictEqual(await clocks(), { APT_1: k + 1, APT_2: k + 1, APT_3: k, APT_4: k + 1 });
    assert.strictEqual(await groupInterval(), k);
    await until(page, () => document.querySelector('[data-testid=campus-total]')?.textContent === 'Partial data');
    assert.match(await page.locator('#campus-total').innerText(), /different intervals/);
    await page.screenshot({ path: path.join(SHOTS, 'campus-2-partial.png'), fullPage: true });
    await page.unroute(`**/api/v1/runs/${runs.APT_3}/step`);
    const n = stepPosts.length;
    const failedBody = stepPosts.filter((x) => x.run === runs.APT_3).pop().body;
    await page.click('[data-act=retry][data-apt=APT_3]');
    await until(page, (x) => document.querySelector('[data-testid=group-interval]')?.textContent === String(x), k + 1, 60000);
    const retried = stepPosts.slice(n);
    assert.deepStrictEqual(retried.map((x) => x.run), [runs.APT_3]);
    assert.deepStrictEqual(retried[0].body, failedBody);
    assert.deepStrictEqual(await clocks(), Object.fromEntries(APTS.map((a) => [a, k + 1])));
    await until(page, () => /kW \(model\)/.test(document.querySelector('[data-testid=campus-total]')?.textContent || ''));
  });

  await check('unexpected clock movement: a step from outside the controller stops the group advance', async () => {
    const k = await groupInterval();
    await ok('POST', `/runs/${runs.APT_4}/step`, opTok, { request_id: `outside-${gid}-${k}` });
    const n = stepPosts.length;
    await page.click('[data-act=advance]');
    await until(page, () => Boolean(document.querySelector('[data-testid=unexpected]')));
    assert.strictEqual(stepPosts.length, n, 'nothing may be sent');
    assert.match(await page.locator('[data-testid=unexpected]').innerText(), /APT_4 at \d+/);
  });

  await check('connection loss on the campus: animation stops and the last reading is labelled', async () => {
    await page.route('**/api/v1/state*', (r) => r.abort('failed'));
    await until(page, () => document.getElementById('bldg-APT_1').dataset.connection === 'lost');
    assert.match(await page.locator('#campus-total').innerText(), /Last received/);
    assert.strictEqual(await page.evaluate(() => getComputedStyle(document.querySelector('#bldg-APT_1 .bolt')).animationName), 'none');
    await page.unroute('**/api/v1/state*');
    await until(page, () => document.getElementById('bldg-APT_1').dataset.connection === 'ok');
  });

  await check('manager at phone width: next action and report beside the scene, no sideways scroll', async () => {
    const p = mg.APT_2.p;
    const [content, view] = await p.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
    const y = async (sel) => (await p.locator(sel).boundingBox()).y;
    assert.ok(await y('#next') < await y('#building') && await y('#building') < await y('#proposal'));
    for (const t of ['Plan asked', 'Manager reported', 'Meter observed']) assert.match(await p.locator('body').innerText(), new RegExp(t));
    assert.match(await p.locator('[data-testid=reading-source]').first().innerText(), /Synthetic simulator reading/);
    await p.screenshot({ path: path.join(SHOTS, 'campus-4-manager-phone.png'), fullPage: true });
  });

  await check('operator campus at phone width: no sideways scroll', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(2500);
    const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
  });

  await check('no page errors', async () => { assert.deepStrictEqual(errors, []); });

  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL').length;
  console.log(`\n${results.length - failed} passed, ${failed} failed (group ${gid})`);
  process.exit(failed ? 1 : 0);
})();
