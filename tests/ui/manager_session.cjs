// Issues #29 and #31: switching runs / sessions on the manager page must never show the previous run's state or act on it.
// Deterministic: chosen responses are held by Playwright routes and released at known points (no timing guesses).
//
//   npm install --prefix <folder outside the repo> playwright-core
//   set NODE_PATH=<that folder>\node_modules  NW_OPERATOR_CODE=...  NW_MANAGER_CODE=...
//   node tests/ui/run_isolated.cjs tests/ui/manager_session.cjs      (or set NW_BASE to a running service)
const assert = require('node:assert');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const OP = process.env.NW_OPERATOR_CODE;
const MG = process.env.NW_MANAGER_CODE;
const PHONE = { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };

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
const rid = () => `mgr-session-${Date.now()}-${++n}`;
async function api(method, p, token, body) {
  const res = await fetch(`${BASE}/api/v1${p}`, {
    method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (res.status !== 200) throw new Error(`${method} ${p} -> ${res.status} ${JSON.stringify(data)}`);
  return data;
}
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

// A held response: the route waits until release() is called, then forwards (or finds the request already cancelled).
function holder() {
  let release;
  const gate = new Promise((r) => { release = r; });
  const seen = [];
  return {
    seen, release: () => release(),
    async handle(route) {
      seen.push(route.request().url());
      await gate;
      try { await route.continue(); } catch { /* the page cancelled it: that is allowed */ }
    },
  };
}

(async () => {
  assert.ok(OP && MG, 'set NW_OPERATOR_CODE and NW_MANAGER_CODE');
  const opt = (await api('POST', '/demo/session', null, { role: 'operator', access_code: OP })).token;
  const mgt = (await api('POST', '/demo/session', null, { role: 'manager', apartment_id: 'APT_1', access_code: MG })).token;
  // Each run has an open (unaccepted) proposal, so Accept/Decline buttons are on screen.
  async function runWithProposal() {
    const run = (await api('POST', '/runs', opt, { request_id: rid(), clock_start_utc: '2026-08-18T18:30:00Z', apartment_params: {} })).run_id;
    const p = (await api('POST', '/requirements', mgt, { run_id: run, request_id: rid(), ev_sessions: [] })).proposal;
    return { run, proposal: p.proposal_id };
  }
  const commitmentOf = async (run) => (await api('GET', `/state?run_id=${run}&apartment_id=APT_1`, mgt)).active_commitment;

  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const errors = [];
  const ctx = await browser.newContext(PHONE);
  const page = await ctx.newPage();
  page.on('pageerror', (e) => errors.push(e.message));
  const decisions = [];
  page.on('request', (r) => { if (r.method() === 'POST' && /\/decision$/.test(r.url())) decisions.push(JSON.parse(r.postData() || '{}')); });
  const failed = [];
  page.on('requestfailed', (r) => failed.push(r.url()));

  const meta = () => page.locator('#meta').innerText();
  async function signIn(run) {
    await page.locator('#signin-form').waitFor({ state: 'visible' });
    await page.fill('#run-id', run);
    await page.fill('#apartment-id', 'APT_1');
    await page.fill('#access-code', MG);
    await page.click('#signin-form button');
    await page.waitForFunction((r) => document.getElementById('meta').textContent.includes(r), run);
    await page.locator('[data-act=accept]').first().waitFor();
  }
  async function signOut() {
    await page.click('#sign-out');
    await page.locator('#signin-form').waitFor({ state: 'visible' });
  }
  // Watch the page for the duration and record every run id it shows in the header.
  async function watch(ms, a, b) {
    const seen = [];
    for (let t = 0; t < ms; t += 100) {
      const m = await meta();
      const who = m.includes(a) ? 'A' : m.includes(b) ? 'B' : 'none';
      if (!seen.length || seen[seen.length - 1][1] !== who) seen.push([t, who]);
      await pause(100);
    }
    return seen;
  }

  await page.goto(`${BASE}/manager`);

  await check('1. a late /state for the old run never reappears after switching sessions (the issue #29 reproduction)', async () => {
    const A = await runWithProposal(), B = await runWithProposal();
    await signIn(A.run);
    const h = holder();
    await page.route(`**/api/v1/state?run_id=${A.run}*`, (r) => h.handle(r));
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));  // starts a refresh for A
    await page.waitForFunction(() => true);
    for (let i = 0; i < 20 && !h.seen.length; i++) await pause(50);
    assert.ok(h.seen.length >= 1, 'the refresh for run A was not issued');
    await signOut();
    assert.strictEqual(await page.locator('[data-act=accept]').count(), 0, 'old Accept buttons stayed on the page after sign-out');
    assert.strictEqual((await meta()).trim(), '', 'the old run header stayed on the page after sign-out');
    await signIn(B.run);
    h.release();  // run A's response now arrives, after the switch
    const seen = await watch(4000, A.run, B.run);
    await page.unroute(`**/api/v1/state?run_id=${A.run}*`);
    assert.ok(!seen.some(([, w]) => w === 'A'), `run A appeared after switching to B: ${JSON.stringify(seen)}`);
    assert.ok(seen.every(([, w]) => w === 'B'), `run B was not shown throughout: ${JSON.stringify(seen)}`);
    assert.strictEqual(await page.locator(`[data-act=accept][data-run="${A.run}"]`).count(), 0);
    assert.ok(await page.locator(`[data-act=accept][data-run="${B.run}"]`).count() >= 1, 'run B proposal buttons missing');
    await signOut();
  });

  await check('2. an old Accept button sends no decision after switching sessions; the current one still works', async () => {
    const A = await runWithProposal(), B = await runWithProposal();
    await signIn(A.run);
    await page.evaluate(() => { window.__oldAccept = document.querySelector('[data-act=accept]'); });
    await signOut();
    await signIn(B.run);
    const before = decisions.length;
    // Simulate a stale render surviving the switch: put run A's old button back on the page and tap it.
    await page.evaluate(() => { document.body.appendChild(window.__oldAccept); window.__oldAccept.click(); });
    await pause(1500);
    assert.strictEqual(decisions.length, before, `old button sent a decision: ${JSON.stringify(decisions.slice(before))}`);
    assert.strictEqual(await commitmentOf(A.run), null, 'run A has a commitment: the old button acted');
    await page.evaluate(() => window.__oldAccept.remove());
    // Positive control: the current run's button decides for the current run only.
    await page.locator(`[data-act=accept][data-run="${B.run}"]`).click();
    await page.waitForFunction(() => /Plan accepted/.test(document.getElementById('toast').textContent));
    const sent = decisions.slice(before);
    assert.strictEqual(sent.length, 1, `expected one decision, got ${sent.length}`);
    assert.strictEqual(sent[0].run_id, B.run, 'the decision was not for the current run');
    assert.ok((await commitmentOf(B.run))?.proposal_id === B.proposal, 'run B was not accepted');
    assert.strictEqual(await commitmentOf(A.run), null, 'run A changed');
    await signOut();
  });

  // Issue #31: the cases are asserted separately. Only 3a allows two outcomes, because a genuine race permits both.
  const inflight = {};
  await check('3a. a legitimate Accept sent while signed in may complete after sign-out, but only for its own proposal', async () => {
    const A = await runWithProposal(), B = await runWithProposal();
    Object.assign(inflight, { A, B });
    await signIn(A.run);
    const hs = holder(), hd = holder();
    inflight.hs = hs; inflight.hd = hd;
    await page.route(`**/api/v1/state?run_id=${A.run}*`, (r) => hs.handle(r));
    await page.route(`**/api/v1/proposals/${A.proposal}/decision`, (r) => hd.handle(r));
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    const before = decisions.length;
    await page.locator(`[data-act=accept][data-run="${A.run}"]`).click();
    for (let i = 0; i < 40 && !(hs.seen.length && hd.seen.length); i++) await pause(50);
    assert.ok(hs.seen.length && hd.seen.length, 'the poll and the decision were not both in flight');
    assert.strictEqual(decisions.length - before, 1, 'one tap must send exactly one decision');
    assert.strictEqual(decisions[decisions.length - 1].run_id, A.run, 'the decision was not bound to run A');
    await signOut();
    hs.release(); hd.release();
    await pause(2500);
    assert.ok(failed.some((u) => u.includes(`/proposals/${A.proposal}/decision`)), 'the page did not cancel its in-flight decision');
    const ca = await commitmentOf(A.run);
    inflight.committed = Boolean(ca);
    // Either outcome is allowed here (the request may already have reached the service). If it was recorded, it is
    // exactly the proposal the manager accepted, on run A, once; it never lands on another run.
    if (ca) {
      assert.strictEqual(ca.proposal_id, A.proposal, 'the commitment is not the proposal that was accepted');
      const st = await api('GET', `/state?run_id=${A.run}&apartment_id=APT_1`, mgt);
      assert.ok(!(st.pending_proposals || []).some((p) => p.proposal_id === A.proposal), 'accepted proposal still pending');
    }
    assert.strictEqual(await commitmentOf(B.run), null, 'the acceptance landed on another run');
    console.log(`      note: run A after the cancelled decision: ${ca ? 'accepted (request had already reached the service)' : 'not accepted'}`);
  });

  await check('3b. the old session\'s late responses restore nothing and show no success', async () => {
    assert.ok(await page.locator('#signin').isVisible(), 'the sign-in form was replaced');
    assert.ok(await page.locator('#app').isHidden(), 'the app panels came back after sign-out');
    assert.strictEqual((await meta()).trim(), '', 'old run header reappeared');
    assert.strictEqual(await page.locator('#app [data-act]').count(), 0, 'old action buttons reappeared');
    assert.ok(!/Plan accepted/.test(await page.locator('#toast').innerText()), 'a late acceptance message was shown');
  });

  await check('3c. the manager is told an action already sent may still complete', async () => {
    const t = await page.locator('#toast').innerText();
    assert.ok(t.includes('An action already sent may still complete. Check the plan status before trying again.'), `notice missing: ${t}`);
    await page.unroute(`**/api/v1/state?run_id=${inflight.A.run}*`);
    await page.unroute(`**/api/v1/proposals/${inflight.A.proposal}/decision`);
  });

  await check('3d. a plain sign-out with nothing in flight shows no such notice', async () => {
    const C = await runWithProposal();
    await signIn(C.run);
    await signOut();
    assert.ok(!/may still complete/.test(await page.locator('#toast').innerText()), 'notice shown without an action in flight');
  });

  await check('5. the next sign-in shows actions only after authoritative state arrives and resends nothing', async () => {
    // put the earlier pending session back: sign out with an action in flight, then sign in again to the same run
    const D = await runWithProposal();
    await signIn(D.run);
    const hd = holder();
    await page.route(`**/api/v1/proposals/${D.proposal}/decision`, (r) => hd.handle(r));
    await page.locator(`[data-act=accept][data-run="${D.run}"]`).click();
    for (let i = 0; i < 40 && !hd.seen.length; i++) await pause(50);
    await signOut();
    hd.release();
    await pause(1500);
    await page.unroute(`**/api/v1/proposals/${D.proposal}/decision`);
    const committed = Boolean(await commitmentOf(D.run));
    const before = decisions.length;
    const hs = holder();
    await page.route(`**/api/v1/state?run_id=${D.run}*`, (r) => hs.handle(r));
    await page.fill('#run-id', D.run);
    await page.fill('#apartment-id', 'APT_1');
    await page.fill('#access-code', MG);
    await page.click('#signin-form button');
    for (let i = 0; i < 40 && !hs.seen.length; i++) await pause(50);
    assert.ok(hs.seen.length, 'the sign-in did not fetch state');
    assert.strictEqual(await page.locator('#app [data-act]').count(), 0, 'actions were offered before the authoritative state arrived');
    hs.release();
    await page.waitForFunction((r) => document.getElementById('meta').textContent.includes(r), D.run);
    await page.unroute(`**/api/v1/state?run_id=${D.run}*`);
    await pause(1500);
    assert.strictEqual(decisions.length, before, 'a decision was resent automatically after signing in again');
    assert.ok((await page.locator('#toast').innerText()).includes('may still complete'), 'the notice was not repeated after signing in');
    const acceptButtons = await page.locator(`[data-act=accept][data-proposal="${D.proposal}"]`).count();
    if (committed) assert.strictEqual(acceptButtons, 0, 'an already accepted proposal is offered for acceptance again');
    else assert.ok(acceptButtons >= 1, 'the still-open proposal is not offered (state not authoritative)');
    console.log(`      note: run D was ${committed ? 'accepted before sign-in; screen shows it as accepted' : 'not accepted; screen offers it again for a deliberate tap'}`);
    await signOut();
  });

  await check('4. no uncaught page errors', async () => assert.deepStrictEqual(errors, []));
  await browser.close();
  const bad = results.filter((r) => r[0] === 'FAIL');
  console.log(`\n${results.length - bad.length} passed, ${bad.length} failed`);
  process.exitCode = bad.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
