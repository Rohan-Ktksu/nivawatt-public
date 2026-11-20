// One building, drawn from a run's state: the CSS 3D scene (pump, charger, tank from the latest observation only),
// "Plan asked / Manager reported / Meter observed" for the same interval, the backend's revision decision and the
// interval history. Shared by the 3D demo view, the manager page and the operator's campus. Read-only: it never posts.
const NW_BUILDING = (() => {
  const { html, fmtTime, fmtClock, intervalTime, fmtNum, fmtAction, fmtStatus, fmtReason, fmtRevisionReason, fmtQuality, sceneState, fmtReadingSource } = NW_UI;
  const chip = (s) => html`<span class="chip tone-${s.tone}"><span aria-hidden="true">${s.icon}</span>${s.label}</span>`;
  const claimText = (c) => (c.action_id.endsWith(':pump') ? 'Pump on' : 'Charging on');
  const stateWord = { on: 'Running (measured)', off: 'Off (measured)', unknown: 'Unknown' };
  const lastAt = (o) => (o.lastReceived ? o.lastReceived.toLocaleTimeString() : 'earlier');

  // Markup for a scene root. The root element itself carries data-connection; the facts list is optional.
  const sceneMarkup = (withFacts = true) => html`
    <div class="scene3d" aria-hidden="true"><div class="floor">
      <div class="obj obj-tank" data-obj="tank"><div class="box"><div class="water"></div><span class="q">?</span></div><b>Tank</b></div>
      <div class="obj obj-pump" data-obj="pump"><div class="box"><div class="fan"></div><span class="q">?</span></div><b>Pump</b></div>
      <div class="obj obj-charger" data-obj="charger"><div class="box"><div class="bolt"></div><span class="q">?</span></div><b>EV chargers</b></div>
    </div></div>
    ${withFacts ? html`<dl class="facts" data-facts></dl>` : ''}`;

  // opts: { connectionLost, lastReceived }. A lost connection keeps the last values, labelled, with motion stopped.
  function renderScene(root, st, opts = {}) {
    const s = sceneState(st.observation, st.apartment.model_tank_max_l);
    root.dataset.connection = opts.connectionLost ? 'lost' : 'ok';
    const obj = (k) => root.querySelector(`[data-obj=${k}]`);
    obj('pump').dataset.state = s.pump;
    obj('charger').dataset.state = s.charger;
    obj('tank').dataset.state = s.tankPct === null ? 'unknown' : 'known';
    obj('tank').style.setProperty('--level', `${s.tankPct ?? 0}%`);
    const facts = root.querySelector('[data-facts]');
    if (!facts) return s;
    const when = s.interval === null ? 'No reading yet' : `${fmtClock(intervalTime(st.clock, s.interval))} IST model time`;
    facts.innerHTML = String(html`
      ${opts.connectionLost ? html`<dt>Connection</dt><dd data-testid="last-received"><strong>Last received ${lastAt(opts)}</strong>. The service is not answering; the values below are not live and the equipment animation is stopped. Retrying.</dd>` : ''}
      <dt>Reading for</dt><dd data-testid="scene-interval">${when} ${s.quality ? chip(fmtQuality(s.quality)) : ''}</dd>
      ${st.observation ? html`<dt>Source</dt><dd data-testid="reading-source">${fmtReadingSource(st.observation_source, st.observation.source_kind)}</dd>` : ''}
      <dt>Pump</dt><dd data-testid="scene-pump">${stateWord[s.pump]}${s.pump === 'unknown' ? '' : `, ${fmtNum(s.pumpKw, 'kW (model)')}`}</dd>
      <dt>EV charger group</dt><dd data-testid="scene-charger">${stateWord[s.charger]}${s.charger === 'unknown' ? '' : `, ${fmtNum(s.evKw, 'kW (model)')}`}</dd>
      <dt>Tank</dt><dd data-testid="scene-tank">${s.tankL === null ? 'Unknown' : fmtNum(s.tankL, 'L', 0)}</dd>`);
    return s;
  }

  // The plan's action for interval i, only if the plan now in force covered it. A revised plan replaces the old one,
  // and the state does not carry superseded plans, so their actions are reported as not available rather than guessed.
  function requestedFor(st, i, entries) {
    const c = st.active_commitment;
    if (!c) return { text: 'No accepted plan', note: 'Nothing has been requested yet.' };
    const judgedUnder = entries.find((h) => h.observation_status !== 'UNOBSERVED')?.commitment_id;
    if (c.effective_from_interval > i || (judgedUnder && judgedUnder !== c.commitment_id)) {
      return { text: 'Earlier plan, since revised', note: 'This view shows only the plan now in force.' };
    }
    const a = c.option.actions.find((x) => x.interval_index === i);
    return { text: fmtAction(a), note: `Accepted ${fmtTime(c.accepted_at)}. Accepting does not switch anything.` };
  }

  // The interval the three columns describe: the latest observation's, else the current clock interval.
  const focusInterval = (st) => (st.observation ? st.observation.interval_index : st.clock.interval_index);

  function columns(st, opts = {}) {
    const obs = st.observation;
    const i = focusInterval(st);
    const at = `${fmtClock(intervalTime(st.clock, i))} IST`;
    const entries = (st.execution_history || []).filter((h) => h.interval_index === i);
    const req = requestedFor(st, i, entries);
    const claims = entries.flatMap((h) => h.claims);
    const judged = entries.find((h) => h.observation_status !== 'UNOBSERVED');
    const s = sceneState(obs, st.apartment.model_tank_max_l);
    return {
      requested: html`<h2>Plan asked</h2><p class="muted">Plan for ${at}</p>
        <p class="big" data-testid="requested">${req.text}</p><p class="muted">${req.note}</p>`,
      reported: html`<h2>Manager reported</h2><p class="muted">For ${at}</p>
        <p class="big" data-testid="reported">${claims.length ? claims.map(claimText).join(', ') : 'Nothing reported'}</p>
        ${claims.map((c) => html`<p class="muted">Reported ${fmtTime(c.claimed_at)}</p>`)}
        <p class="muted">A report is the manager's word. It does not count as done.</p>`,
      observed: html`<h2>Meter observed</h2><p class="muted">For ${at}${obs ? html` · <span data-testid="observed-source">${fmtReadingSource(st.observation_source, obs.source_kind)}</span>` : ''}</p>
        ${opts.connectionLost ? html`<p data-testid="observed-last-received"><strong>Last received ${lastAt(opts)}</strong>, not live.</p>` : ''}
        ${obs ? html`
          <dl class="facts">
            <dt>Pump</dt><dd>${s.pump === 'unknown' ? 'Unknown' : fmtNum(s.pumpKw, 'kW (model)')}</dd>
            <dt>EV chargers</dt><dd>${s.charger === 'unknown' ? 'Unknown' : fmtNum(s.evKw, 'kW (model)')}</dd>
            <dt>Tank</dt><dd>${s.tankL === null ? 'Unknown' : fmtNum(s.tankL, 'L', 0)}</dd>
          </dl>
          <p data-testid="verdict">${chip(fmtStatus(judged ? judged.observation_status : 'UNOBSERVED'))}</p>
          ${judged?.reason && judged.observation_status !== 'STALE' ? html`<p class="muted">${fmtReason(judged.reason)}</p>`
            : judged ? html`<p class="muted">${fmtStatus(judged.observation_status).detail}</p>` : ''}`
        : html`<p class="big" data-testid="verdict">No reading yet</p>`}`,
    };
  }

  function revision(st) {
    const g = st.revision_gate;
    const rev = st.discrepancy_status?.revision;
    const pend = st.pending_proposals || [];
    return html`<h2>Revision decision (from the service)</h2>
      ${g ? html`<dl class="facts">
          <dt>Gate decision</dt><dd><code data-testid="gate-decision">${g.decision}</code></dd>
          <dt>Trigger</dt><dd><code data-testid="gate-trigger">${g.trigger}</code> ${fmtRevisionReason(g.trigger)}</dd>
          ${g.at_interval !== undefined && g.at_interval !== null ? html`<dt>Decided at</dt><dd>${fmtClock(intervalTime(st.clock, g.at_interval))} IST model time</dd>` : ''}
          <dt>Proposal status</dt><dd><code>${g.proposal_status || 'not stated'}</code></dd>
        </dl>` : html`<p data-testid="gate-decision">No revision has been considered yet.</p>`}
      ${rev?.status && rev.status !== 'NONE' ? html`<p><code data-testid="revision-status">${rev.status}</code> ${rev.explanation || ''}</p>` : ''}
      ${pend.map((p) => html`<p data-testid="pending">Waiting for the manager: version ${p.revision}. ${fmtRevisionReason(p.reason)}</p>`)}`;
  }

  function timeline(st) {
    const hist = [...(st.execution_history || [])].sort((a, b) => b.interval_index - a.interval_index).slice(0, 8);
    return html`<h2>Interval history</h2>
      ${hist.length ? html`<ul class="timeline">${hist.map((h) => html`<li><b>${fmtClock(intervalTime(st.clock, h.interval_index))}</b> ${chip(fmtStatus(h.observation_status))}
        ${h.claims.length ? `Reported: ${h.claims.map(claimText).join(', ')}.` : ''}</li>`)}</ul>` : html`<p>No interval has been checked yet.</p>`}`;
  }

  return { sceneMarkup, renderScene, columns, revision, timeline, focusInterval, chip };
})();
