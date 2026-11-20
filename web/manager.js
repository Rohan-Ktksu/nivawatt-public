(() => {
  const { html, fmtTime, fmtClock, fmtWindows, inWindows, intervalTime, fmtNum, fmtSource, fmtReason, fmtStatus, fmtRevisionReason, fmtQuality, fmtScope, fmtRawBench, fmtAction, recommendation, shortfalls, planChanges, fmtInfeasibility, fmtOperatorIntervention, fmtMeterChannel, fmtMeterScale, METER_CHIP, fmtMeterTotal, fmtRevisionHold, waterLevels, pumpRules, fmtError } = NW_UI;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 5000;

  const params = new URLSearchParams(location.search);
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* the form asks again next time */ } },
  };
  // Per tab, like the session token (sessionStorage): which building and run this tab signed in to. localStorage is
  // shared by every tab, so it only prefills the form; four building tabs in one browser stay independent.
  const tab = {
    get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch { /* lasts until reload */ } },
  };
  let runId = params.get('run') || tab.get('nivawatt.manager.tab_run') || store.get('nivawatt.manager.run') || '';
  // A building link (?run=...&apartment=...) names the run and apartment; a session for another apartment is ended so
  // the sign-in form asks for this building's code instead of showing another building.
  const tabApartment = tab.get('nivawatt.manager.tab_apartment');
  let apartmentId = params.get('apartment') || tabApartment || store.get('nivawatt.manager.apartment') || 'APT_1';
  const otherBuilding = Boolean(params.get('apartment')) && params.get('apartment') !== tabApartment;
  let state = null;
  let report = null;
  let reportKey = null;
  let lastJson = null;
  let lastUpdated = null;
  let shownProposal = null;
  let connectionWarned = false;
  // Set while a decision or a requirement change is in flight: the reason shown beside the disabled Accept/Decline.
  let busy = null;
  // The proposal whose Accept/Decline buttons are on screen. A tap is only acted on for this proposal.
  let displayedProposalId = null;
  // Set when an acceptance comes back PROPOSAL_STALE: { expired: proposal, loadFailed: bool, error: string }.
  // Cleared when the manager decides on the replacement or dismisses the notice. Never auto-accepts anything.
  let stale = null;
  const deliveredSent = new Set();
  // Session generation: bumped on every sign-in, sign-out or lost session. Every response, poll and action captures it
  // when it starts and is dropped if it has changed, so nothing from an earlier run or session can reach the screen
  // or send a decision (issue #29).
  let generation = 0;
  // Write actions (decision, done report, requirement change) still waiting for the service. Polls are not included.
  // If a session ends with one pending, its outcome is unknown: it may still complete on the service (issue #31).
  const pendingActions = new Set();
  // Set when a session ended with an action in flight, so the notice is repeated after the next sign-in.
  let pendingNoticeShown = false;
  const PENDING_NOTICE = 'An action already sent may still complete. Check the plan status before trying again.';
  async function tracked(work) {
    const token = Symbol('action');
    pendingActions.add(token);
    try { return await work(); } finally { pendingActions.delete(token); }
  }
  const PANELS = ['next', 'commitment', 'proposal', 'execution', 'tank', 'charging', 'advisory', 'readings', 'meta', 'asked', 'said', 'seen', 'decision'];
  // Returns true when an action was still in flight: the caller tells the manager its outcome is unknown.
  function resetSession() {
    const hadPending = pendingActions.size > 0;
    pendingActions.clear();
    generation += 1;
    NW_API.abortAll();
    state = null; report = null; reportKey = null; lastJson = null; lastUpdated = null;
    shownProposal = null; displayedProposalId = null; busy = null; stale = null; connectionWarned = false;
    deliveredSent.clear();
    PANELS.forEach((id) => put(id, ''));  // the previous run's state and its buttons leave the page at once
    $('app').hidden = true;
    return hadPending;
  }

  let toastTimer;
  const toast = (tone, message) => {
    clearTimeout(toastTimer);
    $('toast').innerHTML = message ? String(html`<div class="notice tone-${tone}"><p>${message}</p></div>`) : '';
    if (tone === 'good') toastTimer = setTimeout(() => toast(), 8000);
  };
  const chip = (s) => html`<span class="chip tone-${s.tone}"><span aria-hidden="true">${s.icon}</span>${s.label}</span>`;
  const put = (id, content) => { $(id).innerHTML = String(content); };
  const attr = (s) => html([s]);

  function showSignIn(message) {
    $('app').hidden = true;
    $('loading').hidden = true;
    $('signin').hidden = false;
    $('run-id').value = runId;
    $('apartment-id').value = apartmentId;
    if (message) toast('bad', message);
  }

  function optionSummary(option, clock, peakWindows, withShortfalls = true) {
    const levels = waterLevels(option, state.apartment.model_tank_min_l);
    const upcoming = option.actions.filter((a) => a.interval_index >= clock.interval_index && (a.pump === 'ON' || a.ev !== 'OFF')).slice(0, 8);
    return html`
      <dl class="facts">
        <dt>Highest load</dt><dd>${fmtNum(option.predicted_peak_model_kw, 'kW (model)')}</dd>
        <dt>Load in peak hours</dt><dd>${fmtNum(option.predicted_peak_window_energy_model_kwh, 'kWh (model)')}</dd>
        <dt>Lowest tank level</dt><dd>${fmtNum(option.predicted_min_tank_model_l, 'L', 0)}</dd>
        <dt>Pump starts planned</dt><dd>${fmtNum(option.pump_starts?.planned)}</dd>
      </dl>
      <h3>Next planned actions</h3>
      ${upcoming.length ? html`<div class="strip">${upcoming.map((a) => {
        const t = intervalTime(clock, a.interval_index);
        return html`<div class="${inWindows(t, peakWindows) ? 'peak' : ''}"><b>${fmtClock(t)}</b>${a.pump === 'ON' ? 'Pump on' : ''}${a.pump === 'ON' && a.ev !== 'OFF' ? ', ' : ''}${a.ev !== 'OFF' ? `Charge ${a.ev}` : ''}${inWindows(t, peakWindows) ? ' (peak)' : ''}</div>`;
      })}</div>` : html`<p>Nothing else is scheduled to run.</p>`}
      ${withShortfalls ? shortfalls(option).map((s) => html`<div class="notice tone-bad"><p><strong>Service shortfall.</strong> ${s}</p></div>`) : ''}
      <details class="plan-basis">
        <summary>Water levels and pump rules in this plan</summary>
        <dl class="facts" data-testid="water-levels">
          ${levels.map((l) => html`<dt>${l.label}</dt><dd>${l.value}<br><span class="muted">${l.note}</span></dd>`)}
        </dl>
        ${pumpRules(option, clock).map((t) => html`<p class="muted" data-testid="pump-rules">${t}</p>`)}
      </details>`;
  }

  function interventionNotice(oi) {
    const lines = fmtOperatorIntervention(oi);
    if (!lines) return '';
    const now = state.apartment.allowance_model_kw;
    const changed = oi.current_allowance_model_kw !== undefined && oi.current_allowance_model_kw !== now;
    return html`<div class="notice tone-warn" data-testid="operator-intervention">
      <p><strong>Operator decision needed.</strong></p>
      ${changed ? html`<p data-testid="allowance-changed"><strong>Worked out when the allowance was ${fmtNum(oi.current_allowance_model_kw, 'kW (model)')}. The operator has since set ${fmtNum(now, 'kW (model)')}.</strong> ${(state.pending_proposals || []).length ? 'The proposal waiting for you uses the new value.' : ''}</p>` : ''}
      ${lines.map((l) => html`<p>${l}</p>`)}</div>`;
  }

  function holdNotice() {
    const hold = fmtRevisionHold(state.discrepancy_status?.revision);
    const d = state.discrepancy_status?.execution_discrepancy;
    const seen = d?.detected && !d.resolved_by_accepted_revision
      ? html`<p>Sensors saw a difference at ${fmtClock(intervalTime(state.clock, d.interval_index))}: ${fmtReason(d.reason)}</p>` : '';
    if (!hold) return seen ? html`<div class="notice tone-unknown">${seen}</div>` : '';
    return html`<div class="notice tone-unknown" data-testid="revision-hold">${seen}
      <p><strong>${hold.title}</strong></p>${hold.lines.map((l) => html`<p>${l}</p>`)}</div>`;
  }

  function renderNext() {
    const r = recommendation(state);
    const ex = state.execution;
    const action = ex?.next_action;
    const i = state.clock.interval_index;
    const reportButton = (kind, label) => {
      const sent = ex.claims.some((c) => c.interval_index === i && c.action_id.endsWith(`:${kind}`));
      return html`<button class="claim" data-act="claim" data-kind="${kind}" data-run="${state.run_id}"
        data-gen="${generation}" data-commitment="${ex.commitment_id}" data-interval="${i}" ${sent ? attr('disabled') : ''}>
        ${sent ? `${kind === 'ev' ? 'Charging' : 'Pump'} reported` : label}</button>`;
    };
    const due = [];
    if (action?.pump === 'ON') due.push(reportButton('pump', 'I turned the pump on'));
    if (action?.ev && action.ev !== 'OFF') due.push(reportButton('ev', `I started charging ${action.ev}`));
    put('next', html`
      <div class="label">Recommended next action, model time ${fmtTime(state.clock.model_time_utc)}</div>
      <div class="big">${r.title}</div>
      ${r.why.map((w) => html`<p>${w}</p>`)}
      ${state.pending_proposals?.length ? html`<p><a href="#proposal">Review the waiting plan</a></p>` : ''}
      ${due.length ? html`
        <div class="buttons">${due}</div>
        <p class="label">This records your report. The meter checks it when this interval closes.</p>
      ` : ''}
      <p class="label">Switching is manual. NivaWatt does not operate any equipment.</p>`);
  }

  function renderCommitment() {
    const c = state.active_commitment;
    if (!c) {
      put('commitment', html`<h2>Accepted plan</h2><p>No plan is accepted yet.</p>`);
      return;
    }
    put('commitment', html`
      <h2>Accepted plan</h2>
      <p><span class="chip tone-good"><span aria-hidden="true">✓</span>Accepted ${fmtTime(c.accepted_at)}</span></p>
      <p class="muted">Applies from ${fmtTime(intervalTime(state.clock, c.effective_from_interval))} model time. Accepted does not mean done.</p>
      ${optionSummary(c.option, state.clock, state.active_advisory?.peak_windows)}
      ${state.operator_intervention?.source === 'active_commitment' ? interventionNotice(state.operator_intervention) : ''}`);
  }

  function staleNotice(p) {
    if (!stale) return '';
    const head = html`<p><strong>${fmtError({ code: 'PROPOSAL_STALE' })}</strong></p>`;
    if (stale.loadFailed) {
      return html`<div class="notice tone-warn" data-testid="stale-notice">${head}
        <p>Could not load the updated plan: ${stale.error}</p>
        <div class="buttons"><button class="primary" data-act="retry-load">Load the updated plan</button></div></div>`;
    }
    if (p && p.proposal_id !== stale.expired.proposal_id) {
      const oldOpt = stale.expired.options?.[0];
      const newOpt = p.options?.[0];
      const changes = oldOpt && newOpt ? planChanges(oldOpt, newOpt, state.clock, state.clock.interval_index) : null;
      return html`<div class="notice tone-warn" data-testid="stale-notice">${head}
        <p>The clock moved past part of version ${stale.expired.revision} before it was accepted, and the rest of it no longer delivers what it promised.</p>
        ${changes ? html`<p>What changed in the updated plan (version ${p.revision}):</p>
        <ul data-testid="what-changed">${changes.map((c) => html`<li>${c}</li>`)}</ul>`
        : html`<p>The changes cannot be listed: one of the two plans has no option to compare.</p>`}
        <p class="muted">Nothing was accepted. The updated plan below needs its own Accept.</p></div>`;
    }
    const rev = state.discrepancy_status?.revision;
    return html`<div class="notice tone-warn" data-testid="stale-notice">${head}
      <p>No updated plan is available right now. Nothing was accepted. Your accepted plan, if any, stays in place.</p>
      <p>${rev?.explanation || 'The service did not say why no updated plan was made.'}</p>
      ${interventionNotice(state.operator_intervention)}
      <div class="buttons"><button data-act="retry-load">Check again</button><button class="decline" data-act="dismiss-stale">Dismiss</button></div></div>`;
  }

  function renderProposal() {
    const first = (state.pending_proposals || [])[0];
    // The refused proposal is never offered again, even while the screen still holds the state from before refusal.
    const p = stale && first?.proposal_id === stale.expired.proposal_id ? null : first;
    displayedProposalId = p ? p.proposal_id : null;
    if (!p) {
      put('proposal', html`<h2>Proposal waiting for you</h2>${staleNotice(null)}<p>No proposal is open.</p>
        ${stale ? '' : holdNotice()}`);
      return;
    }
    const option = p.options[0];
    const blocked = !option || option.requires_operator_exception;
    put('proposal', html`
      <h2>Proposal waiting for you</h2>
      ${staleNotice(p)}
      <p><span class="chip tone-warn"><span aria-hidden="true">!</span>Version ${p.revision}, not active</span></p>
      <p><strong>Why now:</strong> ${fmtRevisionReason(p.reason)}</p>
      <p class="muted">Closes at <span data-expires="${p.expires_at}"></span>. Worked out by ${p.source === 'rules' ? 'fixed scheduling rules, not an optimiser' : p.source}.</p>
      ${(p.infeasibilities || []).length ? html`<div class="notice tone-bad" data-testid="cannot-meet"><p><strong>Service shortfall: this plan cannot fully meet the request.</strong></p>
        <ul>${p.infeasibilities.map((x) => html`<li>${fmtInfeasibility(x, state.clock)}</li>`)}</ul></div>` : ''}
      ${option ? optionSummary(option, state.clock, state.active_advisory?.peak_windows, !(p.infeasibilities || []).length) : html`<p>This proposal has no option to accept.</p>`}
      ${interventionNotice(option?.operator_intervention)}
      ${option?.requires_operator_exception ? html`<div class="notice tone-warn"><p>This option goes over the allowance. Only the operator can approve that.</p></div>` : ''}
      <div class="buttons">
        <button class="primary" data-act="accept" data-proposal="${p.proposal_id}" data-run="${p.run_id}" data-gen="${generation}" ${blocked || busy ? attr('disabled') : ''}>Accept plan</button>
        <button class="decline" data-act="decline" data-proposal="${p.proposal_id}" data-run="${p.run_id}" data-gen="${generation}" ${!option || busy ? attr('disabled') : ''}>Decline</button>
      </div>
      ${busy ? html`<p class="muted" data-testid="busy" role="status">${busy}</p>` : ''}
      <p class="muted">Accepting records your decision for this version. It does not switch anything.</p>`);
  }

  // Reports and disagreements stay listed across revised plans; plain matches are only counted.
  function renderHistory() {
    const history = state.execution_history || [];
    const notable = history
      .filter((h) => h.claims.length || h.observation_status !== 'OBSERVED_MATCH')
      .sort((a, b) => b.interval_index - a.interval_index);
    if (!history.length) return '';
    const matched = history.length - notable.length;
    return html`
      <h3>History</h3>
      <div id="history">
        ${notable.map((h) => html`
          <div class="entry">
            <p><strong>${fmtClock(intervalTime(state.clock, h.interval_index))}</strong> ${chip(fmtStatus(h.observation_status))}</p>
            <p>${h.claims.length ? `You reported: ${h.claims.map((c) => (c.action_id.endsWith(':pump') ? 'pump on' : 'charging on')).join(', ')}.` : 'You did not report anything.'}</p>
            ${h.reason ? html`<p class="muted">${fmtReason(h.reason)}</p>` : ''}
          </div>`)}
      </div>
      ${matched ? html`<p class="muted">${matched} checked ${matched === 1 ? 'interval' : 'intervals'} with no report matched the plan.</p>` : ''}`;
  }

  function renderExecution() {
    const ex = state.execution;
    const claims = ex?.claims || [];
    const checked = ex?.interval_index;
    const claimsForChecked = claims.filter((c) => c.interval_index === checked);
    const status = fmtStatus(ex?.observation_status || 'UNOBSERVED');
    const waiting = claims.filter((c) => checked === null || checked === undefined || c.interval_index > checked);

    put('execution', html`
      <h2>What happened</h2>
      ${ex ? html`
        <div class="ledger">
          <div class="said">
            <h3>You reported</h3>
            ${claimsForChecked.length || waiting.length ? html`<ul>
              ${[...claimsForChecked, ...waiting].map((c) => html`<li>${c.action_id.endsWith(':pump') ? 'Pump on' : 'Charging on'} for ${fmtClock(intervalTime(state.clock, c.interval_index))}, reported ${fmtTime(c.claimed_at)}</li>`)}
            </ul>` : html`<p>Nothing reported for the last checked interval.</p>`}
            <p class="muted">A report is your word. It does not count as done.</p>
          </div>
          <div>
            <h3>Sensors saw</h3>
            <p>${chip(status)}</p>
            ${checked !== null && checked !== undefined ? html`<p class="muted">For ${fmtClock(intervalTime(state.clock, checked))} model time</p>` : ''}
            <p>${fmtReason(ex.reason) || status.detail}</p>
          </div>
        </div>
        ${waiting.length ? html`<div class="notice tone-unknown"><p><span aria-hidden="true">… </span>Sensors have not checked your latest report yet.</p></div>` : ''}
      ` : html`<p>Accept a plan to start reporting. Sensors check each interval independently.</p>`}
      ${renderHistory()}
      ${report ? html`
        <h3>So far in this run</h3>
        <dl class="facts">
          <dt>Reports made</dt><dd>${fmtNum(report.counts?.claims)}</dd>
          <dt>Reports contradicted by sensors</dt><dd>${fmtNum(report.counts?.claims_contradicted)}</dd>
          <dt>Intervals with sensor deviation</dt><dd>${fmtNum(report.counts?.observed_deviations)}</dd>
          <dt>Plans proposed / accepted</dt><dd>${fmtNum(report.counts?.proposals)} / ${fmtNum(report.counts?.acceptances)}</dd>
          <dt>Intervals with a usable reading</dt><dd>${fmtNum(report.observation_coverage?.valid)} of ${fmtNum(report.observation_coverage?.intervals_elapsed)}${report.observation_coverage?.late ? `, ${report.observation_coverage.late} late` : ''}</dd>
        </dl>` : ''}`);
  }

  function renderTank() {
    const a = state.apartment;
    const obs = state.observation;
    const missing = obs && obs.tank_l === null;
    const max = a.model_tank_max_l || 1;
    const pct = (v) => Math.max(0, Math.min(100, (v / max) * 100));
    const reserve = {
      ABOVE_MINIMUM: { icon: '✓', tone: 'good', label: 'Above reserve' },
      BELOW_MINIMUM: { icon: '✕', tone: 'bad', label: 'Below reserve' },
      UNKNOWN: { icon: '?', tone: 'unknown', label: 'Reserve unknown' },
    }[state.readiness.water_reserve_status] || { icon: '?', tone: 'unknown', label: 'Reserve unknown' };
    put('tank', html`
      <h2>Water tank</h2>
      <div class="tank-row">
        <div class="tank ${missing ? 'unknown' : ''}" role="img" aria-label="${missing ? 'Tank level unknown' : `Tank at ${fmtNum(a.model_tank_l, 'litres', 0)} of ${fmtNum(max, 'litres', 0)}`}">
          <div class="water" style="height:${pct(a.model_tank_l)}%"></div>
          <div class="reserve" style="bottom:${pct(a.model_tank_min_l)}%"></div>
        </div>
        <div>
          <p class="big">${missing ? 'Unknown now' : fmtNum(a.model_tank_l, 'L', 0)}</p>
          ${missing ? html`<p>Last valid reading ${fmtNum(a.model_tank_l, 'L', 0)}.</p>` : ''}
          <p>${chip(reserve)}</p>
          <p class="muted">Dashed line marks the ${fmtNum(a.model_tank_min_l, 'L', 0)} reserve. Tank holds ${fmtNum(max, 'L', 0)}.</p>
          <p>Pump seen as: <strong>${a.pump_state === 'UNKNOWN' ? 'Unknown' : a.pump_state}</strong></p>
          ${a.source_water_available ? '' : html`<div class="notice tone-warn"><p>The operator announced no source water for this interval.</p></div>`}
        </div>
      </div>`);
  }

  function renderCharging() {
    const a = state.apartment;
    const c = state.clock;
    const r = state.readiness;
    const sessions = a.ev_sessions.map((s) => {
      const remaining = s.remaining_model_kwh;
      const passed = c.interval_index >= s.departure_interval;
      const allocated = r.ev_energy?.allocated_by_ev_model_kwh ? r.ev_energy.allocated_by_ev_model_kwh[s.ev_id] : null;
      const verified = r.verified_ev_delivered_by_ev_model_kwh ? r.verified_ev_delivered_by_ev_model_kwh[s.ev_id] : null;
      return html`
        <h3>${s.ev_id}</h3>
        <dl class="facts">
          <dt>Requested</dt><dd>${fmtNum(s.requested_model_kwh, 'kWh (model)')}</dd>
          <dt>Deadline</dt><dd>${fmtTime(intervalTime(c, s.departure_interval))}</dd>
          <dt>Allocated to this EV (inferred)</dt><dd>${fmtNum(allocated, 'kWh (model)')}</dd>
          <dt>Verified for this EV</dt><dd>${verified === null || verified === undefined ? 'Not available' : fmtNum(verified, 'kWh (model)')}</dd>
          <dt>Still to deliver (estimate)</dt><dd>${fmtNum(remaining, 'kWh (model)')}</dd>
        </dl>
        ${passed && remaining > 1e-9 ? html`<div class="notice tone-bad"><p><strong>Service shortfall.</strong> The deadline passed with ${fmtNum(remaining, 'kWh (model)')} not delivered.</p></div>` : ''}
        <form data-ev="${s.ev_id}" class="req-form">
          <details>
            <summary>Change this requirement</summary>
            <label class="field">Energy needed (kWh, model) <input type="number" name="kwh" min="0" step="0.5" value="${s.requested_model_kwh}" required></label>
            <label class="field">Deadline (IST, model time)
              <select name="departure">
                ${Array.from({ length: c.horizon_intervals - c.interval_index + 1 }, (_, n) => c.interval_index + n).filter((n) => n > s.arrival_interval).map((n) => html`<option value="${n}" ${n === s.departure_interval ? attr('selected') : ''}>${fmtTime(intervalTime(c, n))}</option>`)}
              </select>
            </label>
            <button type="submit">Request a new proposal</button>
            <p class="muted">Your accepted plan stays in place until you accept the new proposal.</p>
          </details>
        </form>`;
    });
    put('charging', html`
      <h2>Charging</h2>
      ${aggregateOnly(r) ? html`<div class="notice tone-unknown"><p><span aria-hidden="true">? </span>Energy per EV cannot be verified: one combined meter cannot be attributed to an EV.</p></div>`
        : html`<dl class="facts" data-testid="ev-group-energy">
          <dt>EV charger group, measured</dt><dd>${fmtNum(r.ev_energy?.measured_group_model_kwh, 'kWh (model)')}</dd>
          ${r.ev_energy?.unallocated_group_model_kwh > 0 ? html`<dt>Not allocated to any EV</dt><dd>${fmtNum(r.ev_energy.unallocated_group_model_kwh, 'kWh (model)')}</dd>` : ''}
          ${r.ev_energy?.recovered_unassigned_model_kwh > 0 ? html`<dt>Recovered after missing readings</dt><dd>${fmtNum(r.ev_energy.recovered_unassigned_model_kwh, 'kWh (model)')}, not assigned to a time or an EV</dd>` : ''}
        </dl>
        <p class="muted">The meter measures the charger group as one total. Per-EV figures are allocations inferred from which EVs were declared present; no per-vehicle meter exists, so none is verified.</p>`}
      ${sessions}
      <p class="muted">This is electricity delivered to the charging circuit, not battery charge level.</p>`);
  }

  const aggregateOnly = (r) => (r.verified_ev_unavailable_reason || '').startsWith('AGGREGATE_ONLY');

  function renderAdvisory() {
    const adv = state.active_advisory;
    const a = state.apartment;
    put('advisory', html`
      <h2>Peak advisory</h2>
      ${adv ? html`
        <p class="big">${fmtWindows(adv.peak_windows).join(', ')}</p>
        <p>Avoid running flexible loads in these hours where you can.</p>
        <dl class="facts">
          <dt>Forecast region</dt><dd>${adv.geographic_scope}</dd>
          <dt>Issued</dt><dd>${fmtTime(adv.issued_at)} by the demonstration operator</dd>
          <dt>Forecast data</dt><dd>${fmtSource(adv.source_kind)}</dd>
          <dt>Advisory ID</dt><dd><code>${adv.advisory_id}</code></dd>
        </dl>` : html`<p>No advisory has been issued for this run.</p>`}
      <h3>Allowance for participating loads</h3>
      <p class="big">${fmtNum(a.allowance_model_kw, 'kW (model)')}</p>
      <p class="muted">Covers ${a.allowance_scope || 'participating loads only'}. Set by the operator. It is not a whole-building or transformer limit.</p>`);
  }

  // One meter, one row per channel. Values are the meter's; nothing is added up here except by the service.
  function meterCard(o) {
    const m = o?.meter;
    if (!m) return '';
    const total = fmtMeterTotal(m.total, m.channels);
    return html`
      <div class="meter-card" data-testid="meter-card">
        <h3>Meter <code>${m.meter_id}</code></h3>
        <p>${chip({ icon: '~', tone: 'unknown', label: METER_CHIP[m.electrical_source_kind || m.source_kind] || 'Unknown source' })} <span data-testid="meter-label">${m.label}</span></p>
        ${fmtMeterScale(m).map((line) => html`<p class="muted" data-testid="meter-scale">${line}</p>`)}
        ${m.channels.map((ch) => {
          const f = fmtMeterChannel(ch, state.clock, m);
          return html`<div class="meter-channel" data-testid="meter-channel" data-channel="${ch.channel_id}">
            <p><strong>${f.title}</strong> ${chip(f.quality)}</p>
            <p class="muted">${f.group}</p>
            <dl class="facts">${f.rows.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}<dt>Covers</dt><dd>${f.span}</dd></dl>
            ${f.restarted ? html`<p class="muted">${f.restarted}</p>` : ''}
            ${f.recovered ? html`<p class="muted" data-testid="meter-recovered">${f.recovered}</p>` : ''}
          </div>`;
        })}
        ${total ? html`<p data-testid="meter-total" class="${total.complete ? '' : 'notice tone-unknown'}"><strong>Total:</strong> ${total.text}</p>` : ''}
        <p class="muted">Pump power does not prove water was delivered; the tank sensor is separate. Charger-group energy is not any one vehicle's energy or battery charge.</p>
      </div>`;
  }

  function renderReadings() {
    const o = state.observation;
    const q = fmtQuality(o?.quality);
    put('readings', html`
      <h2>Where the readings come from</h2>
      <p>${chip(q)}</p>
      <p>${q.detail}</p>
      <dl class="facts">
        <dt>Source</dt><dd>${o ? fmtSource(o.source_kind, o.raw_bench) : 'No reading yet'}</dd>
        <dt>Meters</dt><dd>${fmtScope(o?.measurement_scope)}</dd>
        ${o ? html`
          <dt>Reading covers</dt><dd>${fmtTime(intervalTime(state.clock, o.interval_index))} model time</dd>
          <dt>Arrived in</dt><dd>${o.received_at_interval === null || o.received_at_interval === undefined ? (o.quality === 'STALE' ? 'Not arrived' : 'Not stated') : `${fmtTime(intervalTime(state.clock, o.received_at_interval))} model time`}</dd>
          <dt>Combined power</dt><dd>${fmtNum(o.aggregate_model_kw, 'kW (model)')}</dd>
          <dt>Pump power</dt><dd>${fmtNum(o.pump_model_kw, 'kW (model)')}</dd>
          <dt>Charging power</dt><dd>${fmtNum(o.ev_model_kw, 'kW (model)')}</dd>
          ${o.raw_bench ? html`<dt>Device readings</dt><dd>${fmtRawBench(o.raw_bench)} Raw units, kept separate from model values.</dd>` : ''}
        ` : ''}
        <dt>Utility smart meter</dt><dd>Not connected</dd>
      </dl>
      <p class="muted">A whole-connection consumption figure would not prove the pump or charger ran. Pump power alone does not prove water was delivered.</p>
      ${meterCard(o)}
      ${(state.warnings || []).map((w) => html`<p class="muted">${w}</p>`)}`);
  }

  // The building scene and Plan asked / Manager reported / Meter observed come from the shared building view. While the
  // service is not answering, the last received values stay, labelled, with the animation stopped.
  function renderBuilding() {
    const opts = { connectionLost: connectionWarned, lastReceived: lastUpdated };
    NW_BUILDING.renderScene($('building'), state, opts);
    const cols = NW_BUILDING.columns(state, opts);
    put('asked', cols.requested);
    put('said', cols.reported);
    put('seen', cols.observed);
    put('decision', NW_BUILDING.revision(state));
  }

  function render() {
    put('meta', html`Run <code>${state.run_id}</code>, ${state.apartment.apartment_id}, state version ${state.revision}. Model time ${fmtTime(state.clock.model_time_utc)}, interval ${state.clock.interval_index} of ${state.clock.horizon_intervals} (${state.clock.model_dt_minutes} min each). <a href="/web/scene.html?run=${encodeURIComponent(state.run_id)}&apartment=${encodeURIComponent(state.apartment.apartment_id)}" id="scene-link">3D demo view</a>`);
    renderNext();
    renderBuilding();
    renderCommitment();
    renderProposal();
    renderExecution();
    renderTank();
    renderCharging();
    renderAdvisory();
    renderReadings();
    tickExpiry();
  }

  function tickExpiry() {
    document.querySelectorAll('[data-expires]').forEach((el) => {
      const iso = el.dataset.expires;
      const mins = Math.ceil((new Date(iso).getTime() - Date.now()) / 60000);
      el.textContent = `${fmtTime(iso)} (${mins > 0 ? `about ${mins} min left` : 'closing now'})`;
    });
  }

  async function refresh() {
    const gen = generation;
    const forRun = runId;
    const current = () => gen === generation && forRun === runId;
    let next;
    try {
      next = await NW_API.state(forRun, apartmentId);
    } catch (e) {
      if (!current() || e.code === 'ABORTED' || e.status === 401) return;
      if (stale && state) {  // keep the expired-plan notice up with a retry action
        stale.loadFailed = true;
        stale.error = fmtError(e);
        lastJson = null;
        render();
        return;
      }
      if (!state) {
        showSignIn(fmtError(e));
      } else if (e.status === 403 || e.status === 404) {
        toast('bad', fmtError(e));
      } else {
        connectionWarned = true;
        renderBuilding();
        toast('warn', `${fmtError(e)} Showing the last state received at ${lastUpdated?.toLocaleTimeString() || 'an unknown time'}. It may be out of date.`);
      }
      return;
    }
    if (!current() || next.run_id !== forRun) return;  // a response for an earlier run or session never replaces this one
    if (connectionWarned) { connectionWarned = false; lastJson = null; toast(); }
    lastUpdated = new Date();
    state = next;
    if (stale) stale.loadFailed = false;

    const open = next.pending_proposals?.[0]?.proposal_id || null;
    const wasAccepted = next.active_commitment?.proposal_id === shownProposal;
    if (shownProposal && open !== shownProposal && !wasAccepted && !busy && !stale) {
      toast('warn', 'The proposal you were viewing closed: it expired or a newer version replaced it. The screen shows the latest state.');
    }
    shownProposal = open;
    if (open && !deliveredSent.has(open)) {
      deliveredSent.add(open);
      NW_API.delivered(open).catch(() => deliveredSent.delete(open));
    }

    const key = `${next.clock.interval_index}/${next.revision}/${next.execution?.claims?.length || 0}`;
    if (key !== reportKey) {
      reportKey = key;
      report = await NW_API.report(forRun).catch(() => null);
      if (!current()) return;
    }

    $('signin').hidden = true;
    $('loading').hidden = true;
    $('app').hidden = false;

    // Do not redraw under someone's fingers; the next poll catches up.
    const editing = document.activeElement?.closest?.('.req-form') || document.querySelector('.req-form details[open]');
    const json = JSON.stringify([next, report]);
    if (json !== lastJson && !editing) {
      lastJson = json;
      render();
    }
  }

  async function act(button, work, done) {
    const gen = generation;
    button.disabled = true;
    document.querySelectorAll('.req-form details[open]').forEach((d) => { d.open = false; });
    try {
      await tracked(work);
      if (gen !== generation) return;
      toast('good', done);
    } catch (e) {
      if (gen !== generation || e.code === 'ABORTED') return;
      if (e.status !== 401) toast('bad', fmtError(e));
    }
    if (gen !== generation) return;
    document.activeElement?.blur?.();
    lastJson = null;
    await refresh();
  }

  // Disables Accept/Decline visibly, with the reason, until the request has finished and the screen shows its outcome.
  function setBusy(text) {
    busy = text;
    if (state) renderProposal();
  }

  async function decide(p, accept) {
    const gen = generation;
    if (!state || p.run_id !== runId || state.run_id !== runId) return;  // never decide on another run's proposal
    setBusy(accept ? 'Sending your acceptance. Accept and Decline are paused until the service answers.'
      : 'Sending your decision. Accept and Decline are paused until the service answers.');
    try {
      await tracked(() => NW_API.decide(p, p.options[0].option_id, accept ? 'ACCEPT' : 'DECLINE'));
      if (gen !== generation) return;
      stale = null;
      toast('good', accept ? 'Plan accepted. Nothing has been switched: the actions are still yours to do.' : 'Proposal declined. Your earlier plan, if any, still applies.');
    } catch (e) {
      if (gen !== generation || e.code === 'ABORTED') return;
      if (e.code === 'PROPOSAL_STALE') {
        stale = { expired: p, loadFailed: false, error: '' };  // the refused proposal is never retried
        toast('warn', fmtError(e));
      } else if (e.status !== 401) {
        toast('bad', fmtError(e));
      }
    } finally {
      // Still busy during this refresh, so the generic "proposal closed" notice cannot replace the specific outcome
      // shown above. The buttons come back with the refreshed screen.
      if (gen === generation) {
        document.activeElement?.blur?.();
        try { await refresh(); } finally { if (gen === generation) { busy = null; if (state) renderProposal(); } }
      }
    }
  }

  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-act]');
    if (!b) return;
    // A button drawn for an earlier session or another run does nothing.
    if (b.dataset.gen !== undefined && (Number(b.dataset.gen) !== generation || b.dataset.run !== runId || !state)) return;
    if (b.dataset.act === 'accept' || b.dataset.act === 'decline') {
      if (busy || b.disabled) return;  // in flight: the buttons are shown disabled with the reason
      // Act only for the proposal these buttons were drawn for, and only while it is the one on screen. A tap from an
      // earlier screen (the refused plan, a replaced version) never acts on whatever is displayed now.
      const id = b.dataset.proposal;
      const p = id && id === displayedProposalId ? (state.pending_proposals || []).find((x) => x.proposal_id === id) : null;
      if (!p) return;
      decide(p, b.dataset.act === 'accept');
    } else if (b.dataset.act === 'retry-load') {
      lastJson = null;
      refresh();
    } else if (b.dataset.act === 'dismiss-stale') {
      stale = null;
      lastJson = null;
      render();
    } else if (b.dataset.act === 'claim') {
      const c = state.active_commitment;
      if (b.disabled || b.dataset.commitment !== c?.commitment_id || Number(b.dataset.interval) !== state.clock.interval_index) return;
      act(b, () => NW_API.claim(state.run_id, c.commitment_id, state.clock.interval_index, b.dataset.kind),
        'Report recorded. Sensors have not confirmed it.');
    }
  });

  document.addEventListener('submit', (ev) => {
    const form = ev.target.closest('.req-form');
    if (!form) return;
    ev.preventDefault();
    const button = form.querySelector('button');
    const update = { ev_id: form.dataset.ev, requested_model_kwh: Number(form.kwh.value), departure_interval: Number(form.departure.value) };
    form.querySelector('details').open = false;
    setBusy('Requesting a new proposal. Accept and Decline are paused until it arrives.');
    const gen = generation;
    act(button, () => NW_API.requirements(state.run_id, [update]), 'New proposal requested. Review it before accepting.')
      .finally(() => { if (gen === generation) { busy = null; if (state) renderProposal(); } });
  });

  $('signin-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const button = ev.target.querySelector('button');
    button.disabled = true;
    const unknownOutcome = resetSession() || pendingNoticeShown;
    runId = $('run-id').value.trim();
    apartmentId = $('apartment-id').value.trim();
    const gen = generation;
    try {
      await NW_API.signIn({ apartment_id: apartmentId, access_code: $('access-code').value });
      $('access-code').value = '';
      store.set('nivawatt.manager.run', runId);
      store.set('nivawatt.manager.apartment', apartmentId);
      tab.set('nivawatt.manager.tab_run', runId);
      tab.set('nivawatt.manager.tab_apartment', apartmentId);
      if (gen !== generation) return;
      // Actions appear only after the refresh below has fetched this run's authoritative state; nothing is resent.
      if (unknownOutcome) toast('warn', PENDING_NOTICE); else toast();
      pendingNoticeShown = false;
      $('signin').hidden = true;
      $('loading').hidden = false;
      await refresh();
    } catch (e) {
      if (gen === generation && e.code !== 'ABORTED') toast('bad', fmtError(e));
    }
    button.disabled = false;
  });

  $('sign-out').addEventListener('click', () => {
    NW_API.signOut();
    const unknownOutcome = resetSession();
    pendingNoticeShown = unknownOutcome;
    showSignIn();
    if (unknownOutcome) toast('warn', PENDING_NOTICE); else toast();
  });

  $('building-scene').innerHTML = String(NW_BUILDING.sceneMarkup());
  NW_API.init('manager', (e) => {
    const unknownOutcome = resetSession();
    pendingNoticeShown = unknownOutcome;
    showSignIn(unknownOutcome ? `${fmtError(e)} ${PENDING_NOTICE}` : fmtError(e));
  });
  if (otherBuilding) NW_API.signOut();
  if (NW_API.hasSession() && runId) {
    $('loading').hidden = false;
    refresh();
  } else {
    showSignIn();
  }
  setInterval(() => { if (state && !document.hidden) refresh(); }, POLL_MS);
  setInterval(tickExpiry, 15000);
  document.addEventListener('visibilitychange', () => { if (state && !document.hidden) refresh(); });
})();
