// Phone-width checks of the manager page against a running PR #20 service: stale (expired) acceptance,
// replacement, refresh failure, withheld/deferred revisions, missing reading and an allowance that blocks service.
// The manager acts only through the page. The operator side (run setup, clock steps, allowance event) uses the API,
// as the operator page would. Nothing is mocked except where a check says so (blocked request, emptied replacement).
//
//   npm install --prefix <folder outside the repo> playwright-core
//   set NODE_PATH=<that folder>\node_modules
//   start the service with its own NIVAWATT_DB and NIVAWATT_CODES, then:
//   set NW_BASE=http://127.0.0.1:8790  NW_OPERATOR_CODE=...  NW_MANAGER_CODE=...  NW_DB=<the NIVAWATT_DB path>
//   set NW_PYTHON=.venv\Scripts\python   (reads commitments from NW_DB with sqlite3, read-only)
//   node tests/ui/stale_flow.cjs
const path = require('node:path');
const assert = require('node:assert');
const { spawnSync } = require('node:child_process');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8790';
const OP = process.env.NW_OPERATOR_CODE;
const MG = process.env.NW_MANAGER_CODE;
const DB = process.env.NW_DB;
const PY = process.env.NW_PYTHON || 'python';
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots');
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };
const CLOCK = '2026-08-18T18:30:00Z';

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
const rid = (tag) => `stale-flow-${Date.now()}-${tag}-${++n}`;
async function api(method, p, token, body) {
  const res = await fetch(`${BASE}/api/v1${p}`, {
    method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => null);
  return { status: res.status, data };
}
async function ok(method, p, token, body) {
  const r = await api(method, p, token, body);
  if (r.status !== 200) throw new Error(`${method} ${p} -> ${r.status} ${JSON.stringify(r.data)}`);
  return r.data;
}

// Read-only look at the commitments table: the API shows only the active one.
function commitments(runId) {
  const code = 'import sqlite3,sys,json;c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True);'
    + 'print(json.dumps([dict(zip(("commitment_id","proposal_id","status"),r)) for r in '
    + 'c.execute("SELECT commitment_id,proposal_id,status FROM commitments WHERE run_id=? ORDER BY rowid",(sys.argv[2],))]))';
  const out = spawnSync(PY, ['-c', code, DB, runId], { encoding: 'utf8' });
  if (out.status !== 0) throw new Error(`commitments query failed: ${out.stderr}`);
  return JSON.parse(out.stdout);
}

const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
const see = (page, text, timeout = 10000) => page.getByText(text, { exact: false }).first().waitFor({ timeout });
const noSideScroll = async (page) => {
  const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
  assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
};
const noRawJson = async (page) => {
  const text = await page.locator('#app').innerText();
  assert.ok(!/[{}]|"kind"\s*:/.test(text), `raw JSON on the page: ${(text.match(/.{0,40}[{}].{0,40}/) || [''])[0]}`);
};
const acceptButtons = (page) => page.locator('#proposal button[data-act=accept]');
// Every decision request the page sends, in order (proposal IDs).
const watchDecisions = (page) => {
  const sent = [];
  page.on('request', (r) => { if (r.url().includes('/decision')) sent.push(r.url().match(/proposals\/([^/]+)\/decision/)[1]); });
  return sent;
};

let OPT;
let MGT;
const state = (run) => ok('GET', `/state?run_id=${run}&apartment_id=APT_1`, MGT);

async function newRun(params) {
  const run = (await ok('POST', '/runs', OPT, { request_id: rid('run'), clock_start_utc: CLOCK, apartment_params: params || {} })).run_id;
  await ok('POST', '/requirements', MGT, { run_id: run, request_id: rid('req'), ev_sessions: [] });  // first proposal
  return run;
}
// The operator's simulated hands follow the accepted plan unless told otherwise.
async function step(run, over = {}, extra = {}) {
  const s = await state(run);
  const i = s.clock.interval_index;
  const a = s.active_commitment.option.actions.find((x) => x.interval_index === i);
  const sw = { pump: a.pump, ev: a.ev !== 'OFF' ? 'ON' : 'OFF', ...over };
  return ok('POST', `/runs/${run}/step`, OPT, { request_id: rid('step'), actual_switches: sw, ...extra });
}

async function managerPage(browser, run) {
  const ctx = await browser.newContext(PHONE);
  const page = await ctx.newPage();
  page.on('pageerror', (e) => console.log(`  page error: ${e.message}`));
  await page.goto(`${BASE}/manager?run=${run}`);
  await page.fill('#apartment-id', 'APT_1');
  await page.fill('#access-code', MG);
  await page.click('#signin-form button');
  await page.locator('#app').waitFor();
  return page;
}
const reload = async (page) => { await page.reload(); await page.locator('#app').waitFor(); };

// Brings run A to: original plan accepted, operator raised the allowance, the clock moved one interval past the
// resulting proposal, and the manager has not acted. Returns that (now stale) proposal.
async function makeStale(run) {
  for (let k = 0; k < 4; k += 1) await step(run);
  const i = (await state(run)).clock.interval_index;
  await ok('POST', '/events', OPT, { request_id: rid('cap'), run_id: run, kind: 'allowance', known_from_interval: i, allowance_model_kw: 3.3 });
  const p = (await state(run)).pending_proposals[0];
  assert.ok(p && p.reason === 'ALLOWANCE_CHANGED', 'allowance change should propose a revision');
  await step(run);  // time moves on before the manager taps Accept
  return p;
}

(async () => {
  assert.ok(OP && MG && DB, 'set NW_OPERATOR_CODE, NW_MANAGER_CODE and NW_DB');
  OPT = (await ok('POST', '/demo/session', null, { role: 'operator', access_code: OP })).token;
  MGT = (await ok('POST', '/demo/session', null, { role: 'manager', apartment_id: 'APT_1', access_code: MG })).token;
  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });

  // ---------- Run A: allowance 3 kW blocks the 3.3 kW charger; later raised to 3.3 kW ----------
  const runA = await newRun({ allowance_model_kw: 3.0 });
  console.log(`run A ${runA}`);
  const mg = await managerPage(browser, runA);
  let first;
  let stale;

  await check('infeasible allowance: readable cannot-be-met and operator-decision text, no raw JSON, no override', async () => {
    first = (await state(runA)).pending_proposals[0];
    await see(mg, 'Service shortfall: this plan cannot fully meet the request.');
    await see(mg, 'EV_1 is expected to be 10 kWh (model) short of its request at its deadline.');
    await see(mg, 'Operator decision needed.');
    await see(mg, 'allowance of 3 kW (model) blocks the requested service');
    await see(mg, 'An allowance of 3.3 kW (model) would reduce this to no shortfall.');
    await see(mg, 'NOT VERIFIED');
    await see(mg, 'Only the operator can change the allowance.');
    await noRawJson(mg);
    const acts = await mg.$$eval('#app button[data-act]', (bs) => bs.map((b) => b.dataset.act));
    assert.ok(!acts.some((a) => /allow|override|exception/i.test(a)), `unexpected action: ${acts}`);
    await mg.click('#proposal details.plan-basis summary');
    await see(mg, 'Readiness target');
    await see(mg, 'demonstration assumptions, not a real pump');
    await see(mg, 'SYNTHETIC apartment simulation. Not a field measurement.');
    await noSideScroll(mg);
    await shot(mg, 'stale-1-infeasible-allowance');
  });

  await check('1. timely acceptance succeeds and creates exactly one commitment', async () => {
    await acceptButtons(mg).click();
    await see(mg, 'Plan accepted.');
    const s = await state(runA);
    assert.strictEqual(s.active_commitment.proposal_id, first.proposal_id);
    assert.deepStrictEqual(commitments(runA).map((c) => c.proposal_id), [first.proposal_id]);
    await noSideScroll(mg);
  });

  let decisionsA;
  let oldButton;
  await check('2 + 4. late acceptance is refused; a double tap sends one request and shows Accept disabled with a reason; a failed refresh offers a retry; no commitment', async () => {
    stale = await makeStale(runA);
    await reload(mg);
    await see(mg, `Version ${stale.revision}, not active`);
    decisionsA = watchDecisions(mg);
    oldButton = await acceptButtons(mg).evaluate((b) => b.outerHTML);
    // Fail every state refresh until the retry, so the page must offer one. Hold the decision response briefly so the
    // in-flight state can be seen.
    let blocked = 0;
    await mg.route('**/api/v1/state?**', (r) => { blocked += 1; return r.abort('internetdisconnected'); });
    await mg.route('**/decision', async (r) => { await new Promise((done) => setTimeout(done, 1200)); await r.continue(); });
    const decision = mg.waitForResponse((r) => r.url().includes(`/proposals/${stale.proposal_id}/decision`));
    await acceptButtons(mg).dblclick();  // repeated interaction with the old plan
    await see(mg, 'Accept and Decline are paused until the service answers.');
    assert.ok(await acceptButtons(mg).isDisabled(), 'Accept must look disabled while the decision is in flight');
    const res = await decision;
    await mg.unroute('**/decision');
    assert.strictEqual(res.status(), 409);
    assert.strictEqual((await res.json()).error.code, 'PROPOSAL_STALE');
    await see(mg, 'This plan has expired because time has moved on.');
    await see(mg, 'Could not load the updated plan');
    assert.ok(blocked >= 1, 'a state request should have been blocked');
    assert.deepStrictEqual(decisionsA, [stale.proposal_id], 'a double tap on the old plan sends one request');
    assert.strictEqual(await acceptButtons(mg).count(), 0, 'the refused proposal must not be offered again');
    const cs = commitments(runA);
    assert.ok(!cs.some((c) => c.proposal_id === stale.proposal_id), 'refused proposal must have no commitment');
    assert.strictEqual(cs.filter((c) => c.status === 'ACCEPTED').length, 1);
    assert.strictEqual((await state(runA)).active_commitment.proposal_id, first.proposal_id, 'original plan stays active');
    await noSideScroll(mg);
    await shot(mg, 'stale-2-refused-refresh-failed');
    await mg.unroute('**/api/v1/state?**');
  });

  let replacement;
  await check('3. retry loads the replacement with what changed; old-plan taps and time alone accept nothing', async () => {
    await mg.click('button[data-act=retry-load]');
    await mg.locator('[data-testid=what-changed]').waitFor({ timeout: 10000 });
    replacement = (await state(runA)).pending_proposals[0];
    assert.ok(replacement && replacement.reason === 'PROPOSAL_STALE_AT_ACCEPTANCE' && replacement.proposal_id !== stale.proposal_id);
    await see(mg, `Version ${replacement.revision}, not active`);
    await see(mg, 'The previous plan expired because time moved on.');
    await see(mg, 'needs its own Accept');
    await see(mg, 'Worked out when the allowance was 3 kW (model). The operator has since set 3.3 kW (model).');
    const changed = await mg.locator('[data-testid=what-changed]').innerText();
    console.log(`  what changed: ${changed.replace(/\n/g, ' | ')}`);
    await noRawJson(mg);
    // A stale handler: the refused plan's Accept button (bound to its proposal ID) put back on the page and tapped
    // repeatedly. It must not act on the replacement now displayed.
    await mg.evaluate((h) => {
      const t = document.createElement('div');
      t.innerHTML = h;
      const b = t.firstElementChild;
      b.id = 'old-accept';
      b.disabled = false;
      document.getElementById('proposal').appendChild(b);
    }, oldButton);
    for (let k = 0; k < 3; k += 1) await mg.click('#old-accept');
    await mg.evaluate(() => document.getElementById('old-accept')?.remove());
    await mg.waitForTimeout(6000);  // several poll cycles
    const s = await state(runA);
    assert.strictEqual(s.active_commitment.proposal_id, first.proposal_id, 'nothing may be accepted automatically');
    assert.strictEqual(s.pending_proposals[0].proposal_id, replacement.proposal_id);
    assert.deepStrictEqual(decisionsA, [stale.proposal_id], 'old-plan taps send no request');
    assert.ok(await acceptButtons(mg).isEnabled(), 'the replacement Accept is enabled');
    await noSideScroll(mg);
    await shot(mg, 'stale-3-replacement');
  });

  await check('3 (cont). a deliberate first tap on the replacement accepts it at once; one commitment', async () => {
    const t0 = Date.now();
    const sent = mg.waitForRequest((r) => r.url().includes(`/proposals/${replacement.proposal_id}/decision`), { timeout: 1000 });
    await acceptButtons(mg).click();
    await sent;  // the first tap is acted on immediately, not after a delay
    console.log(`  first tap to decision request: ${Date.now() - t0} ms`);
    await see(mg, 'Plan accepted.');
    const s = await state(runA);
    assert.strictEqual(s.active_commitment.proposal_id, replacement.proposal_id);
    const cs = commitments(runA);
    assert.deepStrictEqual(cs.map((c) => c.proposal_id), [first.proposal_id, replacement.proposal_id]);
    assert.ok(!cs.some((c) => c.proposal_id === stale.proposal_id), '5. refused proposal still has no commitment');
    assert.deepStrictEqual(decisionsA, [stale.proposal_id, replacement.proposal_id], 'one request per decision, never a retry on the refused proposal');
    console.log(`  commitments: ${JSON.stringify(cs)}`);
    await noSideScroll(mg);
    await shot(mg, 'stale-4-replacement-accepted');
  });
  await mg.context().close();

  // ---------- Run C: stale refusal where the page is shown no replacement ----------
  await check('no replacement (state response with the replacement removed): honest notice, Check again; then a double tap on the replacement sends one request', async () => {
    const runC = await newRun({ allowance_model_kw: 3.0 });
    const page = await managerPage(browser, runC);
    await acceptButtons(page).click();
    await see(page, 'Plan accepted.');
    const p = await makeStale(runC);
    await reload(page);
    await see(page, `Version ${p.revision}, not active`);
    // MOCKED: the service did make a replacement; the page is shown state without it, as when the planner fails or
    // the recomputed plan equals the active one (both produce no replacement in PR #20).
    await page.route('**/api/v1/state?**', async (r) => {
      const res = await r.fetch();
      const body = await res.json();
      body.pending_proposals = [];
      body.discrepancy_status.revision = { status: 'NONE', explanation: null };
      await r.fulfill({ response: res, json: body });
    });
    await acceptButtons(page).click();
    await see(page, 'This plan has expired because time has moved on.');
    await see(page, 'No updated plan is available right now. Nothing was accepted.');
    await see(page, 'The service did not say why no updated plan was made.');
    assert.strictEqual(await acceptButtons(page).count(), 0, 'no accept prompt without a proposal');
    await page.locator('button[data-act=retry-load]').waitFor();
    await noSideScroll(page);
    await shot(page, 'stale-5-no-replacement');
    await page.unroute('**/api/v1/state?**');
    await page.click('button[data-act=retry-load]');
    await see(page, 'needs its own Accept');
    // Double tap on the replacement: one request, one commitment.
    const repl = (await state(runC)).pending_proposals[0];
    const sent = watchDecisions(page);
    await acceptButtons(page).dblclick();
    await see(page, 'Plan accepted.');
    assert.deepStrictEqual(sent, [repl.proposal_id], `double tap sent: ${sent}`);
    const cs = commitments(runC);
    assert.strictEqual(cs.filter((c) => c.proposal_id === repl.proposal_id).length, 1);
    assert.ok(!cs.some((c) => c.proposal_id === p.proposal_id));
    await page.context().close();
  });

  // ---------- Run B: default run: deferred and withheld revisions, missing reading ----------
  await check('deferred revision: plan unchanged, reason shown, nothing to accept', async () => {
    const runB = await newRun();
    console.log(`run B ${runB}`);
    const page = await managerPage(browser, runB);
    await acceptButtons(page).click();
    await see(page, 'Plan accepted.');
    await step(runB);
    const out = await step(runB, { pump: 'ON' });  // pump run outside the plan, during the 1 h cooldown
    assert.strictEqual(out.state.discrepancy_status.revision.status, 'DEFERRED', JSON.stringify(out.state.discrepancy_status.revision));
    await reload(page);
    await see(page, 'A possible change is on hold. Your accepted plan stays as it is.');
    await see(page, 'less than 1 hour ago');
    await see(page, 'There is nothing to accept.');
    await see(page, 'Sensors saw a difference');
    assert.strictEqual(await acceptButtons(page).count(), 0);
    await noSideScroll(page);
    await shot(page, 'stale-6-deferred');

    const miss = await step(runB, {}, { drop_observation: true });
    assert.strictEqual(miss.execution.observation_status, 'STALE');
    await reload(page);
    await see(page, 'Unknown now');
    await see(page, 'Values are unknown, not zero.');
    await see(page, 'Synthetic simulation, not a measurement');
    await noSideScroll(page);
    await shot(page, 'stale-7-missing-reading');

    const after = await step(runB);
    const rev = after.state.discrepancy_status.revision;
    console.log(`  after missing reading: revision ${rev.status}${rev.gate_decision ? ` (${rev.gate_decision})` : ''}`);
    await reload(page);
    if (rev.status === 'NOT_NEEDED') await see(page, 'No change is needed. Your accepted plan stays as it is.');
    else if (rev.status === 'DEFERRED') await see(page, 'A possible change is on hold.');
    assert.strictEqual(await acceptButtons(page).count(), after.state.pending_proposals.length ? 1 : 0);
    await page.context().close();
  });

  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL');
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  process.exitCode = failed.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
