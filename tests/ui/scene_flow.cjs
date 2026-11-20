// Full false-"done" sequence on the 3D demo view, against a running service and the real emulator prompt
// (COORDINATION_ROHAN.md "Demo runbook": bench_emulator.py --new-run --follow-plan --interactive --ev-kwh 10.5).
// Manager decisions and the done report go through the API, as scripts/rehearse_demo.py does; the page is only read.
//
//   npm install --prefix <folder outside the repo> playwright-core
//   set NODE_PATH=<that folder>\node_modules
//   start the service with config/local.json codes (the emulator reads them), then:
//   set NW_BASE=http://127.0.0.1:<port>  NW_OPERATOR_CODE=...  NW_MANAGER_CODE=...  NW_PYTHON=.venv\Scripts\python
//   node tests/ui/scene_flow.cjs
const path = require('node:path');
const assert = require('node:assert');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright-core');

const BASE = process.env.NW_BASE || 'http://127.0.0.1:8780';
const OP = process.env.NW_OPERATOR_CODE;
const MG = process.env.NW_MANAGER_CODE;
const PY = process.env.NW_PYTHON || 'python';
const SHOTS = process.env.NW_SHOTS || path.join(__dirname, 'screenshots');
const ROOT = path.join(__dirname, '..', '..');

const results = [];
async function check(name, fn) {
  try { await fn(); results.push(['PASS', name]); console.log(`PASS  ${name}`); } catch (e) {
    results.push(['FAIL', name]);
    console.log(`FAIL  ${name}\n      ${String(e.message).split('\n').slice(0, 6).join('\n      ')}`);
  }
}

async function api(method, p, token, body) {
  const res = await fetch(`${BASE}/api/v1${p}`, { method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json();
  if (res.status !== 200) throw new Error(`${method} ${p} -> ${res.status} ${JSON.stringify(data)}`);
  return data;
}

// The emulator's interactive prompt: read stdout until "> ", type a line.
function emulator() {
  const proc = spawn(PY, ['scripts/bench_emulator.py', '--base', BASE, '--new-run', '--follow-plan', '--interactive', '--fast', '--ev-kwh', '10.5'],
    { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: '1' } });
  let buf = '';
  let waiter = null;
  const feed = (d) => { buf += d.toString(); if (waiter && buf.includes('> ')) { const out = buf; buf = ''; const w = waiter; waiter = null; w(out); } };
  proc.stdout.on('data', feed);
  proc.stderr.on('data', feed);
  const prompt = () => new Promise((resolve, reject) => {
    if (buf.includes('> ')) { const out = buf; buf = ''; resolve(out); return; }
    waiter = resolve;
    proc.once('exit', (c) => reject(new Error(`emulator exited (${c}):\n${buf}`)));
  });
  const type = async (text) => { proc.stdin.write(`${text}\n`); const out = await prompt(); return out.split('\n').filter((l) => l.startsWith('interval')).pop() || ''; };
  return { proc, prompt, type };
}

const sig = (page) => page.evaluate(() => ['obj-pump', 'obj-charger', 'obj-tank']
  .map((id) => `${id}=${document.getElementById(id).dataset.state}`).join(' ') + ` level=${document.getElementById('obj-tank').style.getPropertyValue('--level')}`
  + ` reading=${document.querySelector('[data-testid=scene-interval]').textContent}`);
const text = (page, id) => page.locator(`[data-testid=${id}]`).first().innerText();
const onOff = (kw) => (kw === null || kw === undefined ? 'unknown' : kw > 1e-6 ? 'on' : 'off');

(async () => {
  assert.ok(OP && MG, 'set NW_OPERATOR_CODE and NW_MANAGER_CODE (config/local.json, which the emulator also reads)');
  const opt = (await api('POST', '/demo/session', null, { role: 'operator', access_code: OP })).token;
  const mgt = (await api('POST', '/demo/session', null, { role: 'manager', apartment_id: 'APT_1', access_code: MG })).token;
  const emu = emulator();
  const first = await emu.prompt();
  const run = first.match(/created telemetry run (run-[0-9a-f]+)/)[1];
  console.log(`run ${run}`);
  const state = () => api('GET', `/state?run_id=${run}&apartment_id=APT_1`, mgt);
  const fs = (await api('GET', '/forecasts/evaluation', opt)).demo_forecast_start;
  const f = await api('POST', '/forecasts/replay', opt, { request_id: `sc-f-${run}`, dataset_id: 'kptcl-load-curve', model_name: 'ridge_lag_calendar', forecast_start: fs });
  await api('POST', '/events', opt, { request_id: `sc-a-${run}`, run_id: run, kind: 'peak_advisory', known_from_interval: 0, forecast_id: f.forecast_id,
    target_apartments: ['APT_1'], peak_windows: f.peak_windows, valid_from: f.forecast_start, valid_until: f.forecast_end });
  const acceptPending = async (tag) => {
    const p = (await state()).pending_proposals.slice(-1)[0];
    await api('POST', `/proposals/${p.proposal_id}/decision`, mgt, { run_id: run, expected_revision: p.revision, decision: 'ACCEPT', option_id: p.options[0].option_id, request_id: `${tag}-${run}` });
  };

  const browser = await chromium.launch({ channel: process.env.NW_CHANNEL || 'chrome' });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  const writes = [];
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('request', (r) => { if (r.method() !== 'GET' && !r.url().endsWith('/demo/session')) writes.push(`${r.method()} ${r.url()}`); });
  await page.goto(`${BASE}/web/scene.html?run=${run}`);
  await page.fill('#access-code', MG);
  await page.click('#signin-form button');
  await page.locator('#app').waitFor();
  const until = (fn, arg) => page.waitForFunction(fn, arg, { timeout: 12000 });
  const shot = (name) => page.screenshot({ path: path.join(SHOTS, `scene-${name}.png`), fullPage: true });

  // The page must show exactly what the service reports for the latest observation.
  const matchesService = async () => {
    const st = await state();
    const o = st.observation;
    const ch = (id) => o?.meter?.channels?.find((c) => c.channel_id === id);
    const usable = o && ['VALID', 'LATE'].includes(o.quality);
    const kw = (id, field) => (!usable ? null : ch(id) ? (ch(id).quality === 'MISSING' ? null : ch(id).average_power_model_kw) : o[field]);
    const want = `obj-pump=${onOff(kw('pump', 'pump_model_kw'))} obj-charger=${onOff(kw('ev_group', 'ev_model_kw'))} obj-tank=${usable && o.tank_l !== null ? 'known' : 'unknown'}`;
    assert.ok((await sig(page)).startsWith(want), `scene ${await sig(page)} != service ${want}`);
    return st;
  };

  let before;
  await check('before acceptance: no plan, no reading, every object Unknown', async () => {
    await until(() => document.querySelector('[data-testid=requested]')?.textContent === 'No accepted plan');
    assert.match(await sig(page), /obj-pump=unknown obj-charger=unknown obj-tank=unknown/);
    assert.match(await text(page, 'scene-interval'), /No reading yet/);
    await matchesService();
    before = await sig(page);
    await shot('1-before-accept');
  });

  await check('accepting the plan changes the requested action but leaves the equipment unchanged', async () => {
    await acceptPending('sc-acc1');
    await until(() => !/No accepted plan/.test(document.querySelector('[data-testid=requested]').textContent));
    await page.waitForTimeout(2500);  // at least one more poll
    assert.strictEqual(await sig(page), before, 'the scene moved on acceptance');
    assert.match(await text(page, 'requested'), /pump|charg/i);
  });

  let planned;
  await check('interval 0 followed: the scene animates what the meter measured; sensors agree', async () => {
    const st0 = await state();
    planned = st0.active_commitment.option.actions.find((a) => a.interval_index === st0.clock.interval_index);
    console.log(`  interval 0 plan: pump ${planned.pump}, ev ${planned.ev} | ${(await emu.type('')).split('|').slice(-3, -2)}`);
    await until(() => !/No reading yet/.test(document.querySelector('[data-testid=scene-interval]').textContent));
    await matchesService();
    assert.strictEqual((await sig(page)).includes(`obj-charger=${planned.ev !== 'OFF' ? 'on' : 'off'}`), true);
    assert.match(await text(page, 'verdict'), /Sensors agree/);
    assert.match(await text(page, 'reported'), /Nothing reported/);
    await shot('2-interval0-match');
  });

  let afterFalseDone;
  await check('false done: requested and reported say charging, the meter saw 0 kW, sensors disagree', async () => {
    const st = await state();
    const i = st.clock.interval_index;
    const a = st.active_commitment.option.actions.find((x) => x.interval_index === i);
    const kind = a.ev !== 'OFF' ? 'ev' : 'pump';
    const c = st.active_commitment;
    await api('POST', '/execution/claims', mgt, { run_id: run, commitment_id: c.commitment_id, interval_index: i, action_id: `${c.commitment_id}:${i}:${kind}`, request_id: `sc-claim-${run}` });
    console.log(`  typed '${kind} off' | ${(await emu.type(`${kind} off`)).split('|').slice(-3, -1).join('|')}`);
    await until(() => /disagree/.test(document.querySelector('[data-testid=verdict]')?.textContent || ''));
    await matchesService();
    assert.match(await text(page, 'requested'), kind === 'ev' ? /charge EV_1/ : /Turn the pump on/);
    assert.match(await text(page, 'reported'), kind === 'ev' ? /Charging on/ : /Pump on/);
    assert.match(await sig(page), kind === 'ev' ? /obj-charger=off/ : /obj-pump=off/);
    assert.match(await text(page, 'scene-charger'), kind === 'ev' ? /Off \(measured\), 0 kW/ : /./);
    afterFalseDone = await sig(page);
    await shot('3-false-done');
  });

  await check("the revision panel shows the backend's actual gate decision and the pending revision", async () => {
    const st = await state();
    const g = st.revision_gate;
    await until((d) => document.querySelector('[data-testid=gate-decision]')?.textContent === d, g.decision);
    assert.strictEqual(await text(page, 'gate-trigger'), g.trigger);
    console.log(`  gate: ${g.decision} / ${g.trigger}; pending ${st.pending_proposals.map((p) => p.reason)}`);
    assert.strictEqual(g.trigger, 'CLAIM_WITHOUT_OBSERVED_EXECUTION');
    assert.strictEqual(st.pending_proposals.length, 1);
    assert.match(await text(page, 'pending'), /Waiting for the manager: version \d+/);
  });

  await check('accepting the revision leaves the equipment unchanged', async () => {
    await acceptPending('sc-acc2');
    await until(() => !document.querySelector('[data-testid=pending]'));
    await page.waitForTimeout(2500);
    assert.strictEqual(await sig(page), afterFalseDone, 'the scene moved on acceptance');
  });

  await check('missing reading: every object Unknown, the observation says Unknown, the verdict says No reading', async () => {
    console.log(`  typed 'drop' | ${(await emu.type('drop')).split('|').slice(-3, -1).join('|')}`);
    await until(() => /Missing/.test(document.querySelector('[data-testid=scene-interval]').textContent));
    await matchesService();
    assert.match(await sig(page), /obj-pump=unknown obj-charger=unknown obj-tank=unknown/);
    assert.match(await page.locator('#observed').innerText(), /Pump\s*Unknown[\s\S]*EV chargers\s*Unknown[\s\S]*Tank\s*Unknown/);
    assert.match(await text(page, 'verdict'), /No reading/);
    await shot('4-missing-reading');
  });

  await check('readings resume under the accepted revision: sensors agree again', async () => {
    console.log(`  Enter | ${(await emu.type('')).split('|').slice(-3, -1).join('|')}`);
    await until(() => /agree/.test(document.querySelector('[data-testid=verdict]')?.textContent || ''));
    await matchesService();
    await shot('5-resumed');
  });

  await check('emulator readings are labelled Synthetic emulator reading beside the meter values', async () => {
    assert.strictEqual(await text(page, 'reading-source'), 'Synthetic emulator reading');
    assert.strictEqual(await text(page, 'observed-source'), 'Synthetic emulator reading');
  });

  const isState = (u) => u.pathname.endsWith('/api/v1/state');
  const anim = () => page.evaluate(() => getComputedStyle(document.querySelector('#obj-charger .bolt')).animationName);

  await check('failed poll: animation stops, values stay and are labelled Last received; recovery clears it', async () => {
    assert.match(await sig(page), /obj-charger=on/, 'precondition: the charger was measured on');
    assert.notStrictEqual(await anim(), 'none', 'precondition: the charger animates');
    const before = await text(page, 'scene-charger');
    await page.route(isState, (r) => r.abort('failed'));
    await until(() => document.getElementById('scene').dataset.connection === 'lost');
    assert.strictEqual(await anim(), 'none', 'animation must stop while the service is not answering');
    assert.match(await text(page, 'last-received'), /^Last received \S/);
    assert.match(await text(page, 'observed-last-received'), /^Last received \S+.*not live/);
    assert.strictEqual(await text(page, 'scene-charger'), before, 'the last received values stay, not Unknown');
    assert.match(await sig(page), /obj-charger=on/);
    await shot('7-connection-lost');
    await page.unroute(isState);
    await until(() => document.getElementById('scene').dataset.connection === 'ok');
    assert.strictEqual(await page.locator('[data-testid=last-received]').count(), 0);
    assert.strictEqual(await page.locator('[data-testid=observed-last-received]').count(), 0);
    assert.notStrictEqual(await anim(), 'none', 'animation resumes after recovery');
  });

  await check('overlapping polls: one state request at a time while responses are slower than the poll interval', async () => {
    let active = 0;
    let peak = 0;
    let seen = 0;
    await page.route(isState, async (r) => {
      active += 1; seen += 1; peak = Math.max(peak, active);
      await new Promise((res) => setTimeout(res, 5000));  // 2.5 poll intervals
      try { await r.continue(); } catch { /* aborted */ } finally { active -= 1; }
    });
    await page.waitForTimeout(11500);
    await page.unroute(isState);
    console.log(`  ${seen} polls in 11.5 s with 5 s responses, at most ${peak} in flight`);
    assert.ok(seen >= 2, `only ${seen} polls`);
    assert.strictEqual(peak, 1);
  });

  await check('phone width: no sideways scroll', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(2500);
    const [content, view] = await page.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert.ok(content <= view, `page is ${content}px wide in a ${view}px screen`);
    await shot('6-phone');
    await page.setViewportSize({ width: 1280, height: 900 });
  });

  await check('delayed response after sign-out: the read is cancelled and nothing from the old run is shown', async () => {
    const runB = (await api('POST', '/runs', opt, { request_id: `sc-b-${run}`, clock_start_utc: '2026-08-18T18:30:00Z', apartment_params: {} })).run_id;
    const oldRun = (u) => isState(u) && u.searchParams.get('run_id') === run;
    const failedOld = [];
    page.on('requestfailed', (r) => { if (r.url().includes(`run_id=${run}`)) failedOld.push(r.failure()?.errorText); });
    await page.route(oldRun, async (r) => {
      await new Promise((res) => setTimeout(res, 3000));
      try { await r.continue(); } catch { /* cancelled by the page */ }
    });
    await page.waitForRequest((r) => r.url().includes(`/state?run_id=${run}`));
    await page.click('#sign-out');
    await page.locator('#signin').waitFor();
    await page.fill('#run-id', runB);
    await page.fill('#access-code', MG);
    await page.click('#signin-form button');
    await until((r) => document.getElementById('meta').textContent.includes(r), runB);
    const t0 = Date.now();
    while (Date.now() - t0 < 4500) {  // the window in which the delayed old-run response would arrive
      assert.ok(!(await page.locator('#meta').innerText()).includes(run), 'a response from the old run was rendered');
      await page.waitForTimeout(250);
    }
    await page.unroute(oldRun);
    console.log(`  old-run reads failed after sign-out: ${failedOld.join(', ') || 'none'}`);
    assert.ok(failedOld.length >= 1, 'the pending read for the old run was not cancelled');
    assert.match(await text(page, 'scene-interval'), /No reading yet/);
  });

  await check('the page only read the service and raised no errors', async () => {
    assert.deepStrictEqual(writes, [], 'the 3D view must not write to the service');
    assert.deepStrictEqual(errors, []);
  });

  emu.proc.kill();
  await browser.close();
  const failed = results.filter((r) => r[0] === 'FAIL');
  console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
  process.exitCode = failed.length ? 1 : 0;
})().catch((e) => { console.error(e); process.exitCode = 1; });
