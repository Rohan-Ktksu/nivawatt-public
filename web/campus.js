// Operator campus: four buildings (one run each) in a 2x2 isometric view, one forecast issued to all four, and one
// group clock. Acceptance happens on each manager's page; simulated switching happens here, as a separate action.
(() => {
  const { html, fmtTime, fmtClock, fmtWindows, intervalTime, fmtAction, fmtStatus, fmtError, fmtReadingSource } = NW_UI;
  const C = NW_CAMPUS;
  const B = NW_BUILDING;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 2000;
  const STEP_TIMEOUT_MS = 15000;
  const KEY = 'nivawatt.campus.group';
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* kept in memory */ } },
  };

  let group = null;
  try { group = JSON.parse(store.get(KEY) || 'null'); } catch { group = null; }
  const save = (g) => { group = g; store.set(KEY, g ? JSON.stringify(g) : null); };
  const states = {};       // apt -> latest state
  let lastReceived = null;
  let connectionLost = false;
  let selected = C.APARTMENTS[0];
  let inputs = null;       // apt -> { pump, ev, fault }: the simulated switches, edited here, snapshotted per advance
  let advanceNote = null;  // result of the last group advance, shown under the controls
  let busy = false;        // setup or delivery in flight
  let gen = 0;
  let polling = null;
  let approved = null;     // Set of approved window starts

  const put = (id, content) => { $(id).innerHTML = String(content); };
  const attr = (s) => html([s]);
  const toast = (tone, message) => { $('toast').innerHTML = message ? String(html`<div class="notice tone-${tone}"><p>${message}</p></div>`) : ''; };
  const runOf = (apt) => group?.members?.[apt]?.run_id;
  const allRuns = () => Boolean(group) && C.APARTMENTS.every(runOf);
  const delivered = () => allRuns() && C.APARTMENTS.every((a) => group.deliveries?.[a]?.status === 'delivered');

  const ctl = C.controller({
    readClock: async (apt) => (await NW_API.state(runOf(apt), apt)).clock.interval_index,
    step: (apt, body) => NW_API.stepWith(runOf(apt), body, STEP_TIMEOUT_MS),
    save,
  });

  // ---------- top: group, model time, next step ----------
  function nextStep() {
    if (!group) return 'Create the four-building demo group.';
    if (!allRuns()) return 'Finish setting up the group: some buildings have no run yet.';
    if (!delivered()) return 'Review the forecast below and issue its approved peak windows to all four buildings.';
    if (group.pending) return `Retry the building that did not advance: ${advanceNote?.behind?.join(', ') || 'see the controls'}.`;
    if (C.APARTMENTS.some((a) => states[a]?.pending_proposals?.length)) return 'Managers: open your building link and review the waiting plan. Accepting switches nothing.';
    return 'Set the simulated switches (or fill them from the accepted plans), then advance all buildings by 15 minutes.';
  }

  function renderTop() {
    put('top', html`
      <div class="label">Demo group ${group ? html`<code data-testid="group-id">${group.group_id}</code>, scenario ${group.scenario_id}` : 'not created yet'}</div>
      <div class="big" data-testid="next-step">${nextStep()}</div>
      ${group ? html`<p>Group interval <span data-testid="group-interval">${group.interval}</span> of ${group.clock.horizon_intervals}, model time ${fmtTime(intervalTime(group.clock, group.interval))}.</p>` : ''}
      <p class="label">Switching is simulated in this browser. NivaWatt does not operate any equipment.</p>`);
  }

  // ---------- setup ----------
  function renderSetup() {
    if (!group) {
      put('setup', html`<h2>Set up the campus</h2>
        <p>Creates one historical forecast replay and four separate runs (APT_1 to APT_4) on the same clock: same start, 15-minute intervals, 96 intervals. Each building has its own tank level and EV need.</p>
        <div class="buttons"><button class="primary" data-act="create" ${busy ? attr('disabled') : ''}>Create the four-building demo</button></div>
        <form id="resume-form" class="inline-form"><label class="field">Or resume a group ID <input type="text" id="resume-id" autocomplete="off"></label><button type="submit">Resume</button></form>`);
      return;
    }
    put('setup', html`<h2>Demo group</h2>
      <table class="members"><thead><tr><th>Building</th><th>Run</th><th>Setup</th></tr></thead><tbody>
        ${C.APARTMENTS.map((a) => html`<tr><td>${a} · ${C.BUILDINGS[a].label}</td><td><code data-testid="run-${a}">${runOf(a) || 'not created'}</code></td><td>${C.BUILDINGS[a].story}</td></tr>`)}
      </tbody></table>
      ${allRuns() ? '' : html`<div class="buttons"><button class="primary" data-act="create" ${busy ? attr('disabled') : ''}>Finish setup (resumes without duplicates)</button></div>`}
      <p class="muted">Forecast ${group.forecast.forecast_id}, clock start ${fmtTime(group.clock.clock_start_utc)}. The group record lives in this browser; resuming with the same group ID returns the same runs.</p>
      <div class="buttons"><button class="decline" data-act="forget">Start a new group</button></div>`);
  }

  async function createOrResume(gid) {
    busy = true; renderSetup();
    try {
      if (!group || (gid && group.group_id !== gid)) {
        const ev = await NW_API.evaluation();
        const f = await NW_API.replayForecast('ridge_lag_calendar', ev.demo_forecast_start);
        save(C.newGroup(f, gid || undefined));
      }
      const errors = await C.ensureRuns(group, NW_API.createRunWith, save);
      const failed = Object.keys(errors);
      if (failed.length) toast('bad', `Setup stopped for ${failed.join(', ')}: ${fmtError(errors[failed[0]])} Finish setup to resume; existing runs are not recreated.`);
      else toast('good', 'Four runs ready on the same clock.');
    } catch (e) {
      if (e.code !== 'ABORTED') toast('bad', fmtError(e));
    } finally {
      busy = false;
      renderAll();
      refresh();
    }
  }

  // ---------- forecast review and delivery ----------
  function renderForecast() {
    const f = group.forecast;
    if (!approved) approved = new Set(f.peak_windows.map((w) => w.start));
    const d = (a) => group.deliveries?.[a];
    const sent = C.APARTMENTS.some((a) => d(a));
    const failed = C.APARTMENTS.filter((a) => d(a) && d(a).status !== 'delivered');
    put('forecast', html`<h2>Forecast and advisory</h2>
      <p>One regional forecast replay (KPTCL-derived, state level) for ${fmtTime(f.forecast_start)} to ${fmtTime(f.forecast_end)}. A forecast is not a capacity limit.</p>
      <fieldset ${sent ? attr('disabled') : ''}><legend>Approve peak windows</legend>
        ${f.peak_windows.map((w) => html`<label class="choice"><input type="checkbox" data-window="${w.start}" ${approved.has(w.start) ? attr('checked') : ''}> ${fmtWindows([w])[0]}</label>`)}
      </fieldset>
      ${!sent ? html`<div class="buttons"><button class="primary" data-act="issue" ${busy || !allRuns() ? attr('disabled') : ''}>Issue approved windows to all four buildings</button></div>` : ''}
      ${sent ? html`<table class="members"><thead><tr><th>Building</th><th>Delivery</th></tr></thead><tbody>
        ${C.APARTMENTS.map((a) => html`<tr><td>${a}</td><td data-testid="delivery-${a}">${d(a)?.status === 'delivered' ? html`Delivered <code>${d(a).event_id}</code>` : d(a)?.status === 'failed' ? `Failed: ${d(a).error}` : d(a)?.status || 'Not sent'}</td></tr>`)}
      </tbody></table>` : ''}
      ${failed.length ? html`<div class="buttons"><button class="primary" data-act="issue" ${busy ? attr('disabled') : ''}>Retry failed deliveries (${failed.join(', ')})</button></div>` : ''}`);
  }

  async function issue() {
    const windows = group.forecast.peak_windows.filter((w) => approved.has(w.start));
    if (!windows.length) { toast('bad', 'Approve at least one peak window.'); return; }
    busy = true; renderForecast();
    try {
      const failed = await C.deliverAdvisory(group, windows, NW_API.postEvent, save);
      toast(failed.length ? 'bad' : 'good', failed.length ? `Not delivered to ${failed.join(', ')}. Retry sends only those.` : 'Advisory issued to all four runs. Each manager now has a plan to review.');
    } finally { busy = false; renderAll(); refresh(); }
  }

  // ---------- campus view ----------
  function buildGrid() {
    put('campus-grid', html`${C.APARTMENTS.map((a) => html`
      <button type="button" class="bldg" data-act="select" data-apt="${a}" id="bldg-${a}" aria-pressed="false">
        <span class="bldg-name">${a} · ${C.BUILDINGS[a].label}</span>
        <span class="bldg-scene">${B.sceneMarkup(false)}</span>
        <span class="bldg-status" data-status></span>
      </button>`)}`);
  }

  function renderCampus() {
    if (!$('bldg-APT_1')) buildGrid();
    for (const a of C.APARTMENTS) {
      const el = $(`bldg-${a}`);
      el.setAttribute('aria-pressed', String(a === selected));
      const st = states[a];
      if (!st) { el.querySelector('[data-status]').textContent = 'Waiting for its state.'; continue; }
      B.renderScene(el, st, { connectionLost, lastReceived });
      const h = (st.execution_history || []).find((x) => x.interval_index === B.focusInterval(st) && x.observation_status !== 'UNOBSERVED');
      el.querySelector('[data-status]').innerHTML = String(html`Interval ${st.clock.interval_index} · ${B.chip(fmtStatus(h ? h.observation_status : 'UNOBSERVED'))}${st.pending_proposals?.length ? html` <span class="chip tone-warn">Plan waiting</span>` : ''}${st.observation ? html` <span class="muted">${fmtReadingSource(st.observation_source, st.observation.source_kind)}</span>` : ''}`);
    }
    const t = C.campusTotal(states);
    put('campus-total', html`<strong>Participating load across four buildings:</strong> <span data-testid="campus-total">${t.text}</span>${t.complete ? html` <span class="muted">for ${fmtClock(intervalTime(group.clock, t.interval))} IST, pump and charger circuits only</span>` : html` <span class="muted">${t.reason}</span>`}${connectionLost ? html` <strong>Last received ${lastReceived?.toLocaleTimeString() || 'earlier'}, not live.</strong>` : ''}`);
  }

  function renderSide() {
    const st = states[selected];
    if (!st) { put('side', html`<h2>${selected}</h2><p>Waiting for this building's state.</p>`); return; }
    const c = st.active_commitment;
    const a = c?.option?.actions?.find((x) => x.interval_index === st.clock.interval_index);
    const cols = B.columns(st, { connectionLost, lastReceived });
    put('side', html`<h2 data-testid="side-title">${selected} · ${C.BUILDINGS[selected].label}</h2>
      <dl class="facts">
        <dt>Current interval</dt><dd data-testid="side-interval">${st.clock.interval_index} (${fmtClock(st.clock.model_time_utc)} IST model time)</dd>
        <dt>Accepted action</dt><dd data-testid="side-action">${c ? fmtAction(a) : 'No accepted plan'}</dd>
        <dt>Run</dt><dd><code>${st.run_id}</code></dd>
      </dl>
      <div class="side-cols">${cols.requested}${cols.reported}${cols.observed}</div>
      ${B.revision(st)}`);
  }

  // ---------- simulated switches, faults and the group clock ----------
  // Reads each run's state fresh, so a plan accepted since the last poll is used, then fills the controls only.
  async function fillFromPlans() {
    const my = gen;
    try {
      const got = await Promise.all(C.APARTMENTS.map((a) => NW_API.state(runOf(a), a)));
      if (my !== gen) return;
      C.APARTMENTS.forEach((a, n) => { states[a] = got[n]; inputs[a] = { ...inputs[a], ...C.planSwitches(got[n]) }; });
      toast('good', 'Switches set to each accepted plan for the current interval. Nothing has advanced yet.');
    } catch (e) {
      if (my === gen && e.code !== 'ABORTED') toast('bad', fmtError(e));
    }
    renderAll();
  }

  function renderControls() {
    if (!inputs) inputs = Object.fromEntries(C.APARTMENTS.map((a) => [a, { pump: 'OFF', ev: 'OFF', fault: 'none' }]));
    const sel = (a, field, options) => html`<select data-apt="${a}" data-field="${field}" aria-label="${a} ${field}" ${group.pending ? attr('disabled') : ''}>
      ${options.map(([v, label]) => html`<option value="${v}" ${inputs[a][field] === v ? attr('selected') : ''}>${label}</option>`)}</select>`;
    const note = advanceNote;
    put('controls', html`<h2>Simulated equipment and the group clock</h2>
      <p class="muted">These are the switch positions a person would set in each building, and simulated reading faults. They are separate from acceptance: a manager accepting a plan changes none of them.</p>
      <table class="members controls"><thead><tr><th>Building</th><th>Pump</th><th>EV charger</th><th>Reading</th></tr></thead><tbody>
        ${C.APARTMENTS.map((a) => html`<tr><td>${a}</td><td>${sel(a, 'pump', [['OFF', 'Off'], ['ON', 'On']])}</td><td>${sel(a, 'ev', [['OFF', 'Off'], ['ON', 'On']])}</td>
          <td>${sel(a, 'fault', Object.entries(C.FAULTS).map(([k, v]) => [k, v.label]))}</td></tr>`)}
      </tbody></table>
      <div class="buttons">
        <button data-act="fill" ${group.pending || !delivered() ? attr('disabled') : ''}>Set switches to accepted plans</button>
        <button class="primary" data-act="advance" ${ctl.inFlight || group.pending || !delivered() ? attr('disabled') : ''}>Advance all buildings by 15 minutes</button>
      </div>
      ${ctl.inFlight ? html`<p role="status" data-testid="advance-status">Advancing interval ${group.interval}. One group advance at a time.</p>` : ''}
      ${note?.status === 'advanced' ? html`<p data-testid="advance-status">All four buildings advanced to interval ${note.interval}, confirmed against each server clock.</p>` : ''}
      ${group.pending ? html`<div class="notice tone-warn" data-testid="behind"><p><strong>Interval ${group.pending.interval} is not finished.</strong> ${(note?.behind || []).length ? `Behind: ${note.behind.join(', ')}.` : ''} The others kept their result. A retry resends the same request ID and switch snapshot.</p>
        ${(note?.results || []).filter((r) => r.status !== 'done').map((r) => html`<p>${r.apt}: ${r.error}</p>`)}
        <div class="buttons">${C.APARTMENTS.filter((a) => (note?.behind || C.APARTMENTS).includes(a)).map((a) => html`<button class="primary" data-act="retry" data-apt="${a}" ${ctl.inFlight ? attr('disabled') : ''}>Retry ${a}</button>`)}</div></div>` : ''}
      ${note?.status === 'unexpected' ? html`<div class="notice tone-bad" data-testid="unexpected"><p><strong>Stopped: a building's clock moved outside this controller.</strong> Expected interval ${group.interval}${group.pending ? ` or ${group.interval + 1}` : ''}; the service reports ${C.APARTMENTS.map((a) => `${a} at ${note.clocks[a]}`).join(', ')}. Nothing was sent.</p>
        ${new Set(Object.values(note.clocks)).size === 1 ? html`<div class="buttons"><button data-act="adopt">Adopt interval ${note.clocks.APT_1} for the group</button></div>`
          : html`<p>Bring the buildings level on the single-building operator page, then advance again here.</p>`}</div>` : ''}`);
  }

  async function advance(only = null) {
    if (ctl.inFlight) return;
    const p = ctl.advance(group, inputs, only);
    renderControls();
    try {
      advanceNote = await p;
      if (advanceNote.status === 'busy') return;
      if (advanceNote.status === 'advanced') {
        toast('good', `Interval ${advanceNote.interval - 1} closed for all four buildings.`);
        C.APARTMENTS.forEach((a) => { inputs[a].fault = 'none'; });  // a fault applies to one step only
      } else if (advanceNote.status === 'partial') {
        toast('bad', `${advanceNote.behind.join(', ')} did not advance. The others kept their new interval.`);
      }
    } catch (e) {
      if (e.code !== 'ABORTED') toast('bad', fmtError(e));
    } finally {
      renderAll();
      refresh();
    }
  }

  // ---------- manager links ----------
  function renderLinks() {
    const base = store.get('nivawatt.campus.base') || location.origin;
    const local = /^(localhost|127\.|\[::1\])/.test(new URL(base).hostname);
    put('links', html`<h2>Manager links, one per building</h2>
      <label class="field">Address phones use <input type="url" id="base-url" value="${base}"></label>
      ${local ? html`<p class="notice tone-warn">Phones cannot open ${base}. Start the service with <code>python app.py --host 0.0.0.0</code>, then use the LAN address it prints (for example http://192.0.2.20:8780).</p>` : ''}
      <ul class="demo-links">${C.APARTMENTS.map((a) => {
        const u = `${base.replace(/\/$/, '')}/web/manager.html?run=${encodeURIComponent(runOf(a) || '')}&apartment=${a}`;
        return html`<li><a href="${u}" target="_blank" rel="noopener" data-testid="link-${a}">${a} manager</a> <code>${u}</code></li>`;
      })}</ul>
      <p class="muted">Each manager signs in with their own building's code from config/local.json. A link for another building's run is refused by the service.</p>`);
  }

  function renderAll() {
    renderTop();
    renderSetup();
    $('group-ui').hidden = !allRuns();
    if (!allRuns()) return;
    renderForecast();
    renderCampus();
    renderSide();
    renderLinks();
    const editing = document.activeElement?.closest?.('#controls select');
    if (!editing) renderControls();
  }

  // ---------- polling: one at a time, dropped after sign-out ----------
  function refresh() {
    if (polling || !allRuns()) return polling;
    const p = poll().finally(() => { if (polling === p) polling = null; });
    polling = p;
    return p;
  }

  async function poll() {
    const my = gen;
    const g = group;
    let got;
    try {
      got = await Promise.all(C.APARTMENTS.map((a) => NW_API.state(g.members[a].run_id, a)));
    } catch (e) {
      if (my !== gen || e.code === 'ABORTED' || e.status === 401) return;
      if (C.APARTMENTS.some((a) => states[a])) { connectionLost = true; toast('bad', `${fmtError(e)} Retrying.`); renderAll(); }
      else toast('bad', fmtError(e));
      return;
    }
    if (my !== gen || g !== group) return;
    C.APARTMENTS.forEach((a, n) => { if (got[n].run_id === g.members[a].run_id && got[n].apartment.apartment_id === a) states[a] = got[n]; });
    if (connectionLost) toast();
    connectionLost = false;
    lastReceived = new Date();
    renderAll();
  }

  // ---------- events ----------
  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-act]');
    if (!b || b.disabled) return;
    const act = b.dataset.act;
    if (act === 'create') createOrResume();
    else if (act === 'forget') { save(null); approved = null; inputs = null; advanceNote = null; Object.keys(states).forEach((k) => delete states[k]); $('campus-grid').innerHTML = ''; renderAll(); }
    else if (act === 'issue') issue();
    else if (act === 'select') { selected = b.dataset.apt; renderCampus(); renderSide(); }
    else if (act === 'fill') fillFromPlans();
    else if (act === 'advance') advance();
    else if (act === 'retry') advance(b.dataset.apt);
    else if (act === 'adopt') { group.interval = advanceNote.clocks.APT_1; group.pending = null; save(group); advanceNote = null; renderAll(); }
  });
  document.addEventListener('change', (ev) => {
    const t = ev.target;
    if (t.dataset.field && inputs) inputs[t.dataset.apt][t.dataset.field] = t.value;
    if (t.dataset.window) { if (t.checked) approved.add(t.dataset.window); else approved.delete(t.dataset.window); }
    if (t.id === 'base-url') { store.set('nivawatt.campus.base', t.value.trim() || null); renderLinks(); }
  });
  document.addEventListener('submit', (ev) => {
    if (ev.target.id !== 'resume-form') return;
    ev.preventDefault();
    const gid = $('resume-id').value.trim();
    if (gid) createOrResume(gid);
  });

  function enter() {
    $('signin').hidden = true;
    $('app').hidden = false;
    renderAll();
    refresh();
  }
  function showSignIn(message) {
    gen += 1;
    NW_API.abortAll();
    polling = null;
    $('app').hidden = true;
    $('signin').hidden = false;
    toast(message ? 'bad' : null, message);
  }
  $('signin-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      await NW_API.signIn({ access_code: $('access-code').value });
      $('access-code').value = '';
      toast();
      enter();
    } catch (e) { toast('bad', fmtError(e)); }
  });
  $('sign-out').addEventListener('click', () => { NW_API.signOut(); showSignIn(); });
  NW_API.init('operator', (e) => showSignIn(fmtError(e)));
  put('meta', html`Operator view. Four runs, one per building; the service keeps one apartment per run.`);
  if (NW_API.hasSession()) enter(); else showSignIn();
  setInterval(() => { if (!document.hidden && !$('app').hidden) refresh(); }, POLL_MS);
})();
