(() => {
  const { html, fmtTime, fmtClock, fmtWindows, fmtNum, fmtSource, fmtReason, fmtRs, fmtBillStatus, fmtSaving, fmtStatus, fmtQuality, fmtScope, fmtAction, fmtError } = NW_UI;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 5000;
  const SCENARIOS = {
    branch: null,
    aggregate: { observation_scope: 'aggregate' },
    dry: { source_outage_intervals: Array.from({ length: 96 }, (_, i) => i), tank_init_l: 3100 },
    // server-side presets (fictional 100-flat building with a synthetic whole-connection reading)
    flats100_advisory_only: null,
    flats100_demand_aware_experimental: null,
  };
  const CONFIDENCE = { normal: 'Normal', reduced: 'Reduced', unavailable: 'Unavailable' };
  const SOURCE = { default: 'default for new runs', explicit: 'chosen explicitly', legacy: 'run created before the default changed' };

  function schedulingBlock(s) {
    const st = s.status;
    const fallback = s.effective_mode === 'fallback_advisory_only';
    return html`<h3>Scheduling policy</h3>
      <div data-testid="scheduling-policy">
        ${fallback ? html`<div class="notice tone-warn" data-testid="planning-fallback"><p><span aria-hidden="true">! </span><strong>Fallback in use: advisory-only planning.</strong> ${(st?.reasons || []).join('; ')}.</p></div>` : ''}
        <dl class="facts">
          <dt>Requested policy</dt><dd>${s.requested_policy_label} (${SOURCE[s.requested_source] || s.requested_source})</dd>
          <dt>Effective planning mode</dt><dd>${s.effective_mode_label}</dd>
        </dl>
        ${st ? html`<dl class="facts">
          <dt>Forecast confidence</dt><dd>${CONFIDENCE[st.confidence] || st.confidence}</dd>
          <dt>Demand target</dt><dd>${st.target_kva == null ? 'None while in fallback' : fmtNum(st.target_kva, 'kVA, a planning target', 1)}</dd>
          <dt>Whole-building history</dt><dd>${st.forecast.history_source ? `${fmtNum(st.forecast.history_days_available)} days (${st.forecast.history_source})` : 'None: no whole-building measurement'}</dd>
        </dl>
        ${fallback ? '' : st.reasons.map((r) => html`<div class="notice tone-warn"><p><span aria-hidden="true">! </span>${r}</p></div>`)}
        <p class="muted">${s.target_note}</p>
        <p class="muted">${s.fallback_note}</p>` : ''}
        ${s.whole_connection_measurement ? html`<p class="muted">Whole-building reading: ${s.whole_connection_measurement}</p>` : ''}
      </div>`;
  }

  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* the run link still works */ } },
  };
  let runId = new URLSearchParams(location.search).get('run') || store.get('nivawatt.operator.run');
  let forecastId = store.get('nivawatt.operator.forecast');
  let forecast = null;
  let actuals = null;
  let evaluation = null;
  let op = null;
  let apt = null;
  let lastJson = null;
  let connectionWarned = false;
  let experiment = null;
  let comparisonScenario = null;

  let toastTimer;
  const toast = (tone, message) => {
    clearTimeout(toastTimer);
    $('toast').innerHTML = message ? String(html`<div class="notice tone-${tone}"><p>${message}</p></div>`) : '';
    if (tone === 'good') toastTimer = setTimeout(() => toast(), 8000);
  };
  const chip = (s) => html`<span class="chip tone-${s.tone}"><span aria-hidden="true">${s.icon}</span>${s.label}</span>`;
  const put = (id, content) => { $(id).innerHTML = String(content); };
  const on = (attr) => html([attr]);

  function showSignIn(message) {
    $('app').hidden = true;
    $('signin').hidden = false;
    if (message) toast('bad', message);
  }

  function renderForecast() {
    if (!forecast) {
      put('forecast', html`<p class="muted" style="margin-top:.75rem">Load a saved forecast to see its source and peak hours.</p>`);
      return;
    }
    const f = forecast;
    const actualBy = Object.fromEntries((actuals?.points || []).map((p) => [p.start, p.actual_mw]));
    const values = f.points.flatMap((p) => [p.predicted_mw, actualBy[p.start] ?? 0]);
    const top = Math.max(...values, 1);
    const peaks = new Set(f.peak_windows.map((w) => w.start));
    const test = evaluation?.test?.[f.model_name];
    const dv = evaluation?.data_validation;
    put('forecast', html`
      <div class="columns wide-left" style="margin-top:1rem">
        <div>
          <h3>Forecast peak hours</h3>
          <p class="big">${fmtWindows(f.peak_windows).join(', ')}</p>
          <div class="axis"><span>${fmtNum(top, f.demand_unit, 0)}</span></div>
          <div class="chart" id="chart" role="img" aria-label="Hourly demand forecast. The table below has the same values.">
            ${f.points.map((p) => {
              const a = actualBy[p.start];
              const tip = `${fmtClock(p.start)} IST: forecast ${fmtNum(p.predicted_mw, f.demand_unit, 0)}${actuals ? `, actual ${fmtNum(a, f.demand_unit, 0)}` : ''}${peaks.has(p.start) ? ', forecast peak hour' : ''}`;
              return html`<button type="button" class="bar ${peaks.has(p.start) ? 'peak' : ''}" data-tip="${tip}" aria-label="${tip}">
                <i style="height:${(p.predicted_mw / top) * 100}%"></i>
                ${a === null || a === undefined ? '' : html`<b style="bottom:${(a / top) * 100}%"></b>`}
              </button>`;
            })}
          </div>
          <div class="axis"><span>${fmtClock(f.forecast_start)} IST</span><span>${fmtClock(f.forecast_end)} IST</span></div>
          <div class="legend">
            <span>Forecast</span><span class="l-peak">Forecast peak hour</span>
            ${actuals ? html`<span class="l-actual">Actual, for evaluation only</span>` : ''}
          </div>
          <p class="readout" id="readout" aria-live="polite">Hover or tap a bar for its values.</p>
          ${actuals ? html`<p>Forecast error on this day: <strong>${fmtNum(actuals.mae_mw, f.demand_unit, 1)}</strong> mean absolute error. Actuals are never given to the scheduler.</p>` : ''}
          <details>
            <summary>Show values as a table</summary>
            <div class="scroll"><table>
              <thead><tr><th>Hour (IST)</th><th class="n">Forecast (${f.demand_unit})</th>${actuals ? html`<th class="n">Actual (${f.demand_unit})</th>` : ''}<th>Peak</th></tr></thead>
              <tbody>${f.points.map((p) => html`<tr><td>${fmtClock(p.start)}</td><td class="n">${fmtNum(p.predicted_mw, '', 1)}</td>${actuals ? html`<td class="n">${fmtNum(actualBy[p.start], '', 1)}</td>` : ''}<td>${peaks.has(p.start) ? 'Yes' : ''}</td></tr>`)}</tbody>
            </table></div>
          </details>
        </div>
        <div>
          <h3>Where this comes from</h3>
          <dl class="facts">
            <dt>Region</dt><dd>${f.geographic_scope}</dd>
            <dt>Source</dt><dd>${f.source_url ? html`<a href="${f.source_url}" rel="noopener">${f.source_name}</a>` : f.source_name}</dd>
            <dt>Kind</dt><dd>${fmtSource(f.source_kind)}</dd>
            <dt>Model</dt><dd>${f.model_name} (${f.model_kind})</dd>
            <dt>Issued</dt><dd>${fmtTime(f.issued_at)}</dd>
            <dt>Covers</dt><dd>${fmtTime(f.forecast_start)} to ${fmtTime(f.forecast_end)}</dd>
            <dt>Last data used</dt><dd>${fmtTime(f.last_observation_time)}</dd>
            <dt>Assumptions</dt><dd>${f.availability_assumptions}</dd>
            <dt>Forecast ID</dt><dd><code>${f.forecast_id}</code></dd>
          </dl>
          ${test ? html`
            <h3>Backtest for this model</h3>
            <p>On ${test.days} held-out days: mean absolute error ${fmtNum(test.mae_mw, 'MW', 1)}, top-3 peak-hour overlap ${fmtNum(test.top3_peak_hour_overlap, '', 3)}. Method: ${evaluation.kind}.</p>` : ''}
          ${dv ? html`
            <div class="notice tone-warn"><p><span aria-hidden="true">! </span><strong>Data quality.</strong> ${fmtNum(dv.missing_hours)} of ${fmtNum(dv.expected_hours)} hours are missing and not filled in. ${fmtNum(dv.duplicate_timestamps_with_differing_values)} duplicate hours had differing values. Publication lag: ${dv.publication_lag}</p></div>` : ''}
        </div>
      </div>`);
  }

  function renderAdvisory() {
    renderRunGuide();
    const adv = op?.active_advisory;
    if (adv) {
      put('advisory', html`
        <h2>Peak advisory</h2>
        <p><span class="chip tone-good"><span aria-hidden="true">✓</span>Issued ${fmtTime(adv.issued_at)}</span></p>
        <p class="big">${fmtWindows(adv.peak_windows).join(', ')}</p>
        <dl class="facts">
          <dt>Approved by</dt><dd>${adv.approved_by_demo_operator}</dd>
          <dt>Apartments</dt><dd>${adv.target_apartments.join(', ')}</dd>
          <dt>Valid</dt><dd>${fmtTime(adv.valid_from)} to ${fmtTime(adv.valid_until)}</dd>
          <dt>Forecast ID</dt><dd><code>${adv.forecast_id}</code></dd>
          <dt>Advisory ID</dt><dd><code>${adv.advisory_id}</code></dd>
        </dl>
        <p class="muted">A regional forecast does not set a transformer rating or an apartment limit.</p>`);
      return;
    }
    if (!forecast || !op) {
      put('advisory', html`<h2>Peak advisory</h2><p>${forecast ? 'Start a run to issue an advisory.' : 'Load a forecast and start a run to issue an advisory.'}</p>`);
      return;
    }
    if (document.getElementById('advisory-form')?.dataset.forecast === forecast.forecast_id) return; // keep ticks
    put('advisory', html`
      <h2>Peak advisory</h2>
      <p>No advisory is active. Nothing reaches the apartment until you approve one.</p>
      <form id="advisory-form" data-forecast="${forecast.forecast_id}">
        <fieldset><legend>Peak hours to include</legend>
          ${forecast.peak_windows.map((w) => html`<label class="choice"><input type="checkbox" name="w" value="${w.start}" ${on('checked')}> ${fmtWindows([w])[0]}</label>`)}
        </fieldset>
        <p class="muted">For ${op.apartments.map((a) => a.apartment_id).join(', ')}. The allowance stays at the configured value; it is not worked out from MW.</p>
        <button class="primary" type="submit">Approve and issue advisory</button>
      </form>`);
  }

  function renderApartments() {
    if (!op) {
      put('apartments', html`<h2>Participating apartments</h2><p>Start a run to see apartments.</p>`);
      return;
    }
    const o = apt?.observation;
    const q = fmtQuality(o?.quality);
    const cov = op.observation_coverage;
    const warnings = [];
    if (cov.intervals_elapsed > cov.valid_observations) warnings.push(`${cov.intervals_elapsed - cov.valid_observations} of ${cov.intervals_elapsed} intervals have no usable reading. They are unknown, not zero.`);
    if (o?.quality === 'LATE') warnings.push('The latest reading arrived late, so it is not live.');
    if (apt?.apartment?.pump_state === 'UNKNOWN') warnings.push('Pump state is unknown.');
    if (apt?.readiness?.verified_ev_unavailable_reason) warnings.push('Charging energy per EV cannot be verified from one combined meter.');
    if (apt?.readiness?.water_reserve_status === 'BELOW_MINIMUM') warnings.push('The tank is below its reserve.');
    put('apartments', html`
      <h2>Participating apartments</h2>
      <p class="muted">${op.participating_scope}</p>
      ${op.apartments.map((a) => {
        const s = fmtStatus(a.execution?.observation_status || 'UNOBSERVED');
        return html`
          <h3>${a.apartment_id}</h3>
          <p>${chip(s)}</p>
          ${a.execution?.reason ? html`<p>${fmtReason(a.execution.reason)}</p>` : ''}
          <dl class="facts">
            <dt>Allowance</dt><dd>${fmtNum(a.allowance_model_kw, 'kW (model)')}</dd>
            <dt>Latest participating load</dt><dd>${fmtNum(a.latest_aggregate_model_kw, 'kW (model)')}</dd>
            <dt>Tank</dt><dd>${o && o.tank_l === null ? 'Unknown' : fmtNum(apt?.apartment?.model_tank_l, 'L', 0)}</dd>
            <dt>Accepted plan</dt><dd>${a.active_commitment_id ? html`<code>${a.active_commitment_id}</code>` : 'None'}</dd>
            <dt>Proposals waiting</dt><dd>${fmtNum(a.pending_proposals)}</dd>
            <dt>Manager reports</dt><dd>${a.execution ? `${fmtNum(a.execution.claims?.length)} on this plan. Reports are not measurements` : 'None'}</dd>
          </dl>`;
      })}
      ${apt?.scheduling ? schedulingBlock(apt.scheduling) : ''}
      <h3>Measurement evidence</h3>
      <p>${chip(q)}</p>
      <dl class="facts">
        <dt>Source</dt><dd>${o ? fmtSource(o.source_kind, o.raw_bench) : 'No reading yet'}</dd>
        <dt>Meters</dt><dd>${fmtScope(o?.measurement_scope)}</dd>
        <dt>Usable readings</dt><dd>${fmtNum(cov.valid_observations)} of ${fmtNum(cov.intervals_elapsed)} intervals</dd>
        <dt>Utility smart meter</dt><dd>Not connected</dd>
      </dl>
      ${warnings.map((w) => html`<div class="notice tone-warn"><p><span aria-hidden="true">! </span>${w}</p></div>`)}
      ${op.warnings.map((w) => html`<p class="muted">${w}</p>`)}`);
  }

  // Bill analysis: this run's participating-load charges (labelled by provenance) and the committed illustrative
  // monthly scenario. Never another run's figures: stale responses are dropped and the panel is tagged with its run.
  let scenario = null;
  async function renderBilling() {
    if (!op) { put('billing', ''); return; }
    const forRun = runId;
    if ($('billing-run')?.dataset.runId !== forRun) put('billing', html`<h2>Electricity cost (pump and EV circuits)</h2><p class="muted">Loading circuit costs for run <code>${forRun}</code>…</p>`);
    let b;
    try {
      b = await NW_API.runBilling(forRun);
      if (!scenario) scenario = await NW_API.billingScenario().catch(() => null);
    } catch (e) {
      if (runId !== forRun) return;
      put('billing', html`<h2>Electricity cost (pump and EV circuits)</h2><p>Not available: ${fmtError(e)}</p>`);
      return;
    }
    if (runId !== forRun) return;
    const calc = b.calculation;
    const st = fmtBillStatus(calc ? calc.status : b.status);
    const lines = calc ? calc.lines : [];
    put('billing', html`
      <h2>Electricity cost (pump and EV circuits)</h2>
      <div id="billing-run" data-run-id="${forRun}">
        <h3>${b.label}</h3>
        <p data-testid="amount-kind"><strong>What these amounts are:</strong> ${calc?.amount_kind_label || "Energy-related charges of the participating circuits (pump and EV chargers) only: not the apartment's bill"}.</p>
        <p><span class="chip tone-${st.tone}"><span aria-hidden="true">${st.icon}</span>${st.label}</span></p>
        <dl class="facts bill-facts">
          <dt>What this covers</dt><dd>Energy-related charges of the pump and EV charger circuits only. Fixed and demand charges belong to the whole connection and are not shown.</dd>
          <dt>Tariff category</dt><dd>${calc?.category_label || b.category} (assumed for illustration; the real category comes from the connection's bill)</dd>
          <dt>Energy measured so far</dt><dd>${b.energy_kwh_known === null || b.energy_kwh_known === undefined ? 'Unknown' : fmtNum(b.energy_kwh_known, 'kWh', 3)} over ${fmtNum(b.elapsed_hours, 'h', 2)}${b.intervals_missing ? `; ${b.intervals_missing} interval(s) without a usable reading (unknown, not zero)` : ''}</dd>
          ${b.recovered_unassigned_kwh ? html`<dt>Recovered after lost readings</dt><dd>${fmtNum(b.recovered_unassigned_kwh, 'kWh', 3)}, not placed in time, not added</dd>` : ''}
        </dl>
        ${b.reason ? html`<p class="muted">${b.reason}</p>` : ''}
        ${lines.length ? html`<table class="bill" data-testid="bill-lines"><tbody>${lines.map((l) => html`<tr><th scope="row">${l.label}</th><td>${fmtRs(l.amount_rs)}</td><td class="muted">${l.amount_rs === null ? 'Unknown: ' + l.formula : l.formula}</td></tr>`)}</tbody></table>
          <p data-testid="bill-total"><strong>Circuit energy charges, total:</strong> ${calc.total_rs === null ? 'Not shown: at least one charge is unknown, so these are partial circuit charges.' : `${fmtRs(calc.total_rs)} (circuits only, not the apartment's bill)`}</p>` : ''}
        ${(calc?.unavailable || []).map((u) => html`<p class="muted">Missing input: ${u}</p>`)}
        ${(calc?.assumptions || []).map((a) => html`<p class="muted">Assumption: ${a}</p>`)}
      </div>
      ${scenario ? scenarioBlock(scenario) : html`<p class="muted">The illustrative monthly scenario is not available.</p>`}`);
  }

  function scenarioBlock(s) {
    const row = s.scenarios.find((r) => r.scenario === 'base') || s.scenarios[0];
    const c = row.comparison;
    return html`<div data-testid="billing-scenario">
      <h3>Illustrative simulation: a month of baseline vs coordinated operation</h3>
      <p><strong>What these amounts are:</strong> energy-related charges of the pump and EV circuits in a synthetic simulation, partial (FPPCA unknown). Not the apartment's bill.</p>
      <p class="muted">Synthetic plants, ${s.protocol.days} consecutive days${s.protocol.carry_equipment_state ? ', pump on/off state and its minimum on/off history carried across midnight' : ''}, same withdrawals and EV requests for both policies. Not an estimate for any real apartment; not a bill. Tariff: ${s.protocol.tariff.category}, ${s.protocol.tariff.period_start} to ${s.protocol.tariff.period_end}.</p>
      <dl class="facts bill-facts">
        <dt>Equal service</dt><dd>${c.comparable ? 'Yes: same water delivered, no unmet demand, same EV energy by departure' : c.comparability}</dd>
        <dt>Baseline (float switch, charge on arrival)</dt><dd>${fmtNum(row.baseline.kwh, 'kWh', 1)}; known circuit energy charges ${fmtRs(c.costs.baseline.known_components_rs)} (partial: FPPCA unknown)</dd>
        <dt>Coordinated (advisory with metered replanning)</dt><dd>${fmtNum(row.coordinated.kwh, 'kWh', 1)}; storage-adjusted known circuit energy charges ${fmtRs(c.costs.coordinated_storage_adjusted.known_components_rs)} (partial: FPPCA unknown)</dd>
        <dt>Water left in the tank at month end</dt><dd>${fmtNum(row.baseline.final_tank_l, 'L', 0)} vs ${fmtNum(row.coordinated.final_tank_l, 'L', 0)} (difference credited as pump energy)</dd>
        <dt>Water lost as overflow</dt><dd>${fmtNum(c.overflow_l.baseline, 'L', 0)} vs ${fmtNum(c.overflow_l.coordinated, 'L', 0)}</dd>
        <dt>Headline difference (overflow artefact excluded)</dt><dd data-testid="billing-saving">${fmtSaving(c.cost_difference_rs_excluding_overflow, c.comparable)}</dd>
        <dt>Time-of-day effect</dt><dd>${fmtRs(c.time_shifting_effect_rs)}: LT-1 has one flat energy rate.</dd>
        <dt>Demand charge effect</dt><dd>Not estimable: needs the whole connection's maximum demand.</dd>
        <dt>Fuel cost adjustment (FPPCA)</dt><dd>Unknown: no verified monthly rate; not included.</dd>
        <dt>Energy in advisory hours</dt><dd>${fmtNum(c.peak_window_kwh_difference, 'kWh', 1)} (grid relevance only; no bill value under LT-1)</dd>
        <dt>Pump starts (wear trade-off)</dt><dd>${row.baseline.pump_starts} vs ${row.coordinated.pump_starts}</dd>
      </dl>
      <p class="muted">The baseline's extra energy is water pumped past a full tank, an artefact of the model's 15-minute steps, so it is not counted as a saving. Low/base/high scenario differences after removing overflow: ${s.scenarios.map((r) => `${r.scenario} ${fmtSaving(r.comparison.cost_difference_rs_excluding_overflow, r.comparison.comparable).split(':')[0].toLowerCase()}`).join(', ')}. These are scenario assumptions, not confidence intervals.</p>
    </div>`;
  }

  async function renderReport() {
    if (!op) { put('report', html`<h2>Run outcome</h2><p>Start a run to see its outcome.</p>`); return; }
    // Never show another run's outcome: replace it while this run's report loads, and drop a response that arrives
    // after the operator has switched runs.
    const forRun = runId;
    if ($('outcome-basis')?.dataset.runId !== forRun) put('report', html`<h2>Run outcome</h2><p class="muted" id="report-loading">Loading the outcome for run <code>${forRun}</code>…</p>`);
    let m;
    try { m = await NW_API.report(forRun); } catch (e) {
      if (runId !== forRun) return;
      put('report', html`<h2>Run outcome</h2><p>The report is not available: ${fmtError(e)}</p>`);
      return;
    }
    if (runId !== forRun) return;
    const ev = Object.keys(m.ev_delivered_model_kwh || {});
    put('report', html`
      <h2>Run outcome</h2>
      <div class="notice tone-unknown" id="outcome-basis" data-run-id="${forRun}"><p>${fmtSource(m.provenance?.source_kind, { power_scale: m.provenance?.meter?.telemetry_power_scale, volume_scale: m.provenance?.meter?.telemetry_volume_scale })}${m.provenance?.source_kind === 'scaled_hardware' ? '' : '.'} Worked out by the service from ${m.provenance?.plant || 'an unstated source'}.</p></div>
      <dl class="facts">
        <dt>Intervals run</dt><dd>${fmtNum(m.intervals_simulated)} (${m.model_dt_minutes} min each)</dd>
        <dt>Highest participating load</dt><dd>${fmtNum(m.peak_participating_model_kw, 'kW (model)')}</dd>
        <dt>Over allowance</dt><dd>${fmtNum(m.allowance_exceedance_model_kwh, 'kWh (model)')}</dd>
        <dt>Energy in peak hours</dt><dd>${fmtNum(m.peak_window_energy_model_kwh, 'kWh (model)')}</dd>
        <dt>Total energy</dt><dd>${fmtNum(m.total_participating_energy_model_kwh, 'kWh (model)')}</dd>
        <dt>Below tank reserve</dt><dd>${fmtNum(m.reserve_violation_intervals)} intervals</dd>
        <dt>Unmet water</dt><dd>${fmtNum(m.unmet_withdrawal_l, 'L', 0)}</dd>
        <dt>Final tank</dt><dd>${fmtNum(m.final_tank_l, 'L', 0)}</dd>
        <dt>Pump starts</dt><dd>${fmtNum(m.pump_starts)}</dd>
        ${ev.map((id) => html`
          <dt>${id} delivered</dt><dd>${fmtNum(m.ev_delivered_model_kwh[id], 'kWh (model)')}</dd>
          <dt>${id} shortfall at deadline</dt><dd>${m.ev_shortfall_at_departure_model_kwh?.[id] === null ? 'Deadline not reached' : fmtNum(m.ev_shortfall_at_departure_model_kwh?.[id], 'kWh (model)')}</dd>`)}
        <dt>Reports / contradicted</dt><dd>${fmtNum(m.counts?.claims)} / ${fmtNum(m.counts?.claims_contradicted)}</dd>
        <dt>Deviations seen by sensors</dt><dd>${fmtNum(m.counts?.observed_deviations)}</dd>
        <dt>Proposals / acceptances</dt><dd>${fmtNum(m.counts?.proposals)} / ${fmtNum(m.counts?.acceptances)}</dd>
      </dl>
      <details><summary>How each value is defined</summary>
        <dl class="facts">${Object.entries(m.definitions || {}).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>
      </details>`);
  }

  function runType(run) {
    if (run.observation_source !== 'telemetry') return 'Browser simulation';
    return run.meter_clock === 'simulated' ? 'Terminal emulator' : 'Device telemetry';
  }

  function renderRunGuide() {
    if (!op || op.run_id !== runId) {
      put('run-link', html`<p>${runId ? `Loading ${runId}…` : 'Select the run ID printed by your emulator, or create a browser simulation below.'}</p>`);
      return;
    }
    const run = op.run_id;
    const apartment = op.apartments[0];
    const query = new URLSearchParams({ run, apartment: apartment.apartment_id });
    const type = runType({ observation_source: op.observation_source, meter_clock: apt?.meter_clock });
    const time = fmtClock(op.clock.model_time_utc);
    const action = apartment.execution?.next_action;
    const chargeDue = action?.ev && action.ev !== 'OFF';
    const reported = apartment.execution?.claims?.some((c) =>
      c.interval_index === op.clock.interval_index && c.action_id.endsWith(':ev'));
    const terminal = type === 'Terminal emulator';
    const advance = terminal ? 'Press Enter at the emulator prompt.' : 'Use Simulation control to advance one interval.';
    const intervalEnded = op.clock.interval_index >= op.clock.horizon_intervals;
    let next;
    if (intervalEnded) next = html`This run is complete. <a href="#report">View results</a>.`;
    else if (!forecast && !op.active_advisory) next = html`<a href="#forecast-panel">Load the saved forecast</a> for this run.`;
    else if (!op.active_advisory) next = html`<a href="#advisory">Review and issue the peak advisory</a>.`;
    else if (apartment.pending_proposals) next = html`Open the manager page and review the waiting plan.`;
    else if (!apartment.active_commitment_id) next = html`The manager needs to accept a plan before reporting an action.`;
    else if (type === 'Device telemetry') next = html`Use the physical switches for this interval and wait for a device reading.`;
    else if (chargeDue && reported) next = terminal
      ? html`Charging was reported. To rehearse a missed charge, type <code>ev off</code> and press Enter. Watch the 3D view for the meter verdict.`
      : html`Charging was reported. To rehearse a missed charge, leave the charger Off and select <strong>Advance one interval</strong>.`;
    else if (chargeDue) next = html`To rehearse a missed charge, tap <strong>I started charging ${action.ev}</strong> on the manager page before advancing.`;
    else next = html`${action ? fmtAction(action) + '. ' : ''}${advance}`;
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
    put('run-link', html`
      <div class="demo-progress">
        <p><strong>${type}</strong> <code>${run}</code> · ${time} IST · Interval ${op.clock.interval_index} / ${op.clock.horizon_intervals}</p>
        <p><strong>Next step</strong> ${next}</p>
        ${chargeDue && !reported && !apartment.pending_proposals && !intervalEnded && type !== 'Device telemetry'
          ? html`<p class="muted">For a normal check, follow the accepted action and advance. ${advance}</p>` : ''}
        <nav class="demo-links" aria-label="Views for this run">
          <a href="${location.origin}/manager?${query}" id="manager-link">Manager page</a>
          <a href="/web/scene.html?${query}" id="scene-link">3D demo view</a>
          ${op.observation_source !== 'telemetry' ? html`<a href="#sim">Simulation controls</a>` : ''}
        </nav>
        ${loopback ? html`<p class="muted">For your phone, open this operator page with the laptop’s Wi-Fi IP first, then share the manager link above.</p>` : ''}
      </div>`);
  }

  function renderSim() {
    $('sim').hidden = !op;
    if (!op) return;
    const ex = op.apartments[0]?.execution;
    const ended = op.clock.interval_index >= op.clock.horizon_intervals;
    put('due', html`
      <p><strong>Model time ${fmtTime(op.clock.model_time_utc)}</strong>, interval ${op.clock.interval_index} of ${op.clock.horizon_intervals}.</p>
      <p>Accepted plan for this interval: ${ex?.next_action ? fmtAction(ex.next_action) : 'none'}.</p>`);
    const device = op.observation_source === 'telemetry';
    $('sim-title').textContent = device ? 'Device run' : 'Simulation control';
    $('device-note').hidden = !device;
    $('sim-controls').hidden = device;
    $('step').disabled = ended;
    $('match-plan').disabled = !ex?.next_action;
  }

  const POLICY_LABELS = {
    uncoordinated: 'No coordination',
    demand_cap: 'Demand-cap rule',
    fixed_plan: 'Fixed plan, never revised',
    ack_replan: 'Revises from done reports',
    measured_replan: 'Revises from separate meters',
    aggregate_replan: 'Revises from one combined meter',
  };
  const SCENARIO_LABELS = {
    perfect: 'Every action done as advised',
    missed_ev_false_done: 'Charging missed but reported done',
    missed_pump_false_done: 'Pump missed but reported done',
    stale_telemetry: 'Charging missed, some readings lost',
    extra_withdrawal: 'Water use differs from the estimate',
    daytime_ev_tight_allowance: 'Daytime charging with a tight allowance',
    missed_ev_overlapping_pump: 'Charging missed while the pump also runs',
    infeasible_ev: 'Charging request that cannot be met',
  };
  const METRIC_LABELS = {
    peak_participating_model_kw: 'Highest load (kW, model)',
    allowance_exceedance_model_kwh: 'Over allowance (kWh, model)',
    peak_window_energy_model_kwh: 'Energy in peak hours (kWh, model)',
    reserve_violation_intervals: 'Intervals below reserve',
    unmet_withdrawal_l: 'Unmet water (L)',
    ev_shortfall_model_kwh: 'EV shortfall (kWh, model)',
    pump_starts: 'Pump starts',
  };
  const PAIRS = {
    paired_measured_minus_ack: 'Separate meters compared with done reports',
    paired_branch_minus_aggregate: 'Separate meters compared with one combined meter',
  };

  function renderComparison() {
    if (!experiment) {
      put('comparison', '');
      return;
    }
    const names = Object.keys(experiment.summary);
    if (!names.includes(comparisonScenario)) [comparisonScenario] = names;
    const scenario = experiment.summary[comparisonScenario];
    const metrics = Object.keys(METRIC_LABELS);
    const policies = Object.keys(scenario).filter((k) => !k.startsWith('paired_'));
    const info = (policy) => experiment.runs.find((r) => r.policy === policy) || {};
    const pairs = Object.keys(PAIRS).filter((k) => scenario[k]);
    put('comparison', html`
      <div class="notice tone-unknown" style="margin-top:.75rem"><p>${fmtSource(experiment.source_kind)}. Experiment <code>${experiment.experiment_id}</code>, generated ${fmtTime(experiment.generated_at)}.</p></div>
      <label class="field">Scenario
        <select id="comparison-scenario">
          ${names.map((n) => html`<option value="${n}" ${n === comparisonScenario ? on('selected') : ''}>${SCENARIO_LABELS[n] || n}</option>`)}
        </select>
      </label>
      <h3>${SCENARIO_LABELS[comparisonScenario] || comparisonScenario}: average per run, with standard deviation</h3>
      <div class="scroll"><table id="comparison-table">
        <thead><tr><th>Policy</th><th>Who acts</th><th class="n">Electrical meters</th><th class="n">Runs</th>${metrics.map((m) => html`<th class="n">${METRIC_LABELS[m]}</th>`)}</tr></thead>
        <tbody>${policies.map((p) => html`<tr data-policy="${p}">
          <td>${POLICY_LABELS[p] || p}</td>
          <td>${info(p).authority === 'automatic_local' ? 'Automatic local control' : 'People following advice'}</td>
          <td class="n">${fmtNum(info(p).electrical_meters)}</td>
          <td class="n">${fmtNum(scenario[p].runs)}</td>
          ${metrics.map((m) => html`<td class="n" data-metric="${m}">${fmtNum(scenario[p][m]?.mean, '', 2)} (sd ${fmtNum(scenario[p][m]?.sd, '', 2)})</td>`)}
        </tr>`)}</tbody>
      </table></div>
      <p class="muted">Lower is better in every column. Automatic control is a different kind of authority: it is not subject to people missing actions, so it is context rather than a like-for-like baseline. Meter costs are unknown until the parts list exists.</p>
      ${pairs.map((k) => html`
        <h3>${PAIRS[k]}, same seeds</h3>
        <div class="scroll"><table data-pair="${k}">
          <thead><tr><th>Measure</th><th class="n">Average difference</th><th class="n">Separate meters better</th><th class="n">Ties</th><th class="n">Other method better</th></tr></thead>
          <tbody>${metrics.map((m) => html`<tr><td>${METRIC_LABELS[m]}</td><td class="n">${fmtNum(scenario[k][m].mean, '', 3)}</td><td class="n">${fmtNum(scenario[k][m].measured_better_runs)}</td><td class="n">${fmtNum(scenario[k][m].ties)}</td><td class="n">${fmtNum(scenario[k][m].baseline_better_runs)}</td></tr>`)}</tbody>
        </table></div>`)}
      ${pairs.length ? html`<p class="muted">A negative difference means separate meters came out lower. These are our experimental baselines, not reproductions of published methods.</p>` : ''}
      <h3>Limits of this comparison</h3>
      <ul>${(experiment.limitations || []).map((l) => html`<li>${l}</li>`)}</ul>
      <dl class="facts">
        <dt>Seeds per varied scenario</dt><dd>${fmtNum(experiment.config?.seeds_per_stochastic_scenario)}</dd>
        <dt>Advisory intervals</dt><dd>${(experiment.config?.peak_intervals || []).join(', ') || 'None'}</dd>
        <dt>Software version</dt><dd><code>${experiment.software_commit || 'Unknown'}</code></dd>
      </dl>`);
  }

  async function renderRunPicker() {
    try {
      const { runs } = await NW_API.listRuns();
      put('run-picker', html`
        <option value="">${runs.length ? 'Choose a run' : 'No earlier runs'}</option>
        ${runs.map((r) => html`<option value="${r.run_id}" ${r.run_id === runId ? on('selected') : ''}>${runType(r)} · ${r.run_id} · interval ${r.interval_index}</option>`)}`);
    } catch (e) {
      if (e.status !== 401) put('run-picker', html`<option value="">Could not list runs</option>`);
    }
  }

  function renderMeta() {
    put('meta', op
      ? html`Run <code>${runId}</code>, state version ${op.revision}. Model time ${fmtTime(op.clock.model_time_utc)}.`
      : html`No run selected.`);
  }

  async function refresh() {
    const selected = runId;
    if (selected) {
      try {
        const nextOp = await NW_API.operatorState(selected);
        const nextApt = await NW_API.state(selected, nextOp.apartments[0].apartment_id);
        if (selected !== runId || !NW_API.hasSession()) return;
        const json = JSON.stringify([nextOp, nextApt]);
        op = nextOp;
        apt = nextApt;
        // An opened run keeps showing the forecast its advisory was issued from.
        const issuedFrom = nextOp.active_advisory?.forecast_id;
        if (issuedFrom && issuedFrom !== forecast?.forecast_id) {
          const snapshot = await NW_API.forecast(issuedFrom);
          if (selected !== runId || !NW_API.hasSession()) return;
          await loadForecast(snapshot);
        }
        if (selected !== runId || !NW_API.hasSession()) return;
        if (json !== lastJson) {
          lastJson = json;
          renderMeta();
          renderAdvisory();
          renderApartments();
          renderSim();
          if (document.activeElement !== $('run-picker')) renderRunPicker();
          await renderReport();
          await renderBilling();
        }
        if (connectionWarned) { connectionWarned = false; toast(); }
      } catch (e) {
        if (selected !== runId || e.status === 401) return;
        if (e.status === 404) {
          runId = null;
          op = null;
          apt = null;
          store.set('nivawatt.operator.run', '');
          toast('warn', 'The saved run no longer exists. Start a new run.');
          renderAll();
        } else {
          connectionWarned = true;
          toast('warn', `${fmtError(e)} Values on screen may be out of date.`);
        }
      }
    }
  }

  function renderAll() {
    renderMeta();
    renderForecast();
    renderAdvisory();
    renderApartments();
    renderSim();
    renderReport();
    renderBilling();
  }

  async function loadForecast(snapshot) {
    forecast = snapshot;
    forecastId = snapshot.forecast_id;
    store.set('nivawatt.operator.forecast', forecastId);
    actuals = await NW_API.actuals(forecastId).catch(() => null);
    $('forecast-start').value = snapshot.forecast_start;
    renderForecast();
    renderAdvisory();
  }

  async function enter() {
    $('signin').hidden = true;
    $('app').hidden = false;
    evaluation = await NW_API.evaluation().catch(() => null);
    if (evaluation?.demo_forecast_start && !$('forecast-start').value) $('forecast-start').value = evaluation.demo_forecast_start;
    if (!NW_API.hasSession()) return;
    if (forecastId) {
      try { await loadForecast(await NW_API.forecast(forecastId)); } catch { forecastId = null; }
    }
    const savedExperiment = store.get('nivawatt.operator.experiment');
    if (savedExperiment) {
      experiment = await NW_API.experiment(savedExperiment).catch(() => null);
      renderComparison();
    }
    renderRunPicker();
    renderAll();
    await refresh();
  }

  async function busy(button, work) {
    button.disabled = true;
    try {
      await work();
    } catch (e) {
      if (e.status !== 401) toast('bad', fmtError(e));
    }
    button.disabled = false;
    lastJson = null;
    await refresh();
  }

  $('forecast-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    busy(ev.target.querySelector('button'), async () => {
      await loadForecast(await NW_API.replayForecast($('model').value, $('forecast-start').value.trim()));
      toast('good', 'Saved forecast loaded. It is not issued until you approve an advisory.');
    });
  });

  function selectRun(id) {
    runId = id;
    op = null;
    apt = null;
    lastJson = null;
    store.set('nivawatt.operator.run', id);
    history.replaceState(null, '', `?run=${encodeURIComponent(id)}`);
    put('step-result', '');
    renderMeta();
    renderAdvisory();
    renderSim();
  }

  $('create-run').addEventListener('click', (ev) => {
    if (!forecast) { toast('warn', 'Load a forecast first, so the run starts on the forecast day.'); return; }
    busy(ev.target, async () => {
      const scenario = $('scenario').value;
      const made = await NW_API.createRun(forecast.forecast_start, scenario, SCENARIOS[scenario]);
      selectRun(made.run_id);
      renderRunPicker();
      toast('good', 'Run started. Approve an advisory, then share the manager link.');
    });
  });

  $('run-picker').addEventListener('change', (ev) => {
    if (!ev.target.value) return;
    selectRun(ev.target.value);
    put('report', html`<h2>Run outcome</h2><p class="muted" id="report-loading">Loading the outcome for run <code>${runId}</code>…</p>`);
    put('billing', html`<h2>Electricity cost (pump and EV circuits)</h2><p class="muted">Loading circuit costs for run <code>${runId}</code>…</p>`);
    busy(ev.target, async () => {});
  });

  $('run-comparison').addEventListener('click', (ev) => {
    busy(ev.target, async () => {
      // With an active advisory the comparison uses that run's peak hours; otherwise the service's default.
      const made = await NW_API.createExperiment(op?.active_advisory ? runId : null);
      experiment = await NW_API.experiment(made.experiment_id);
      store.set('nivawatt.operator.experiment', made.experiment_id);
      renderComparison();
      toast('good', `Comparison finished: ${made.runs} simulated runs.`);
    });
  });

  document.addEventListener('change', (ev) => {
    if (ev.target.id !== 'comparison-scenario') return;
    comparisonScenario = ev.target.value;
    renderComparison();
    $('comparison-scenario').focus();
  });

  document.addEventListener('submit', (ev) => {
    if (ev.target.id !== 'advisory-form') return;
    ev.preventDefault();
    const picked = new Set([...ev.target.querySelectorAll('input[name=w]:checked')].map((x) => x.value));
    const windows = forecast.peak_windows.filter((w) => picked.has(w.start));
    if (!windows.length) { toast('warn', 'Pick at least one peak hour to issue an advisory.'); return; }
    busy(ev.target.querySelector('button'), async () => {
      await NW_API.issueAdvisory(runId, op.clock.interval_index, forecast, windows, op.apartments.map((a) => a.apartment_id));
      toast('good', 'Advisory issued. The apartment now has a proposal to review.');
    });
  });

  const radio = (name) => document.querySelector(`input[name=${name}]:checked`).value;
  const setRadio = (name, value) => { document.querySelector(`input[name=${name}][value=${value}]`).checked = true; };

  $('match-plan').addEventListener('click', () => {
    const a = op.apartments[0]?.execution?.next_action;
    if (!a) return;
    setRadio('pump', a.pump === 'ON' ? 'ON' : 'OFF');
    setRadio('ev', a.ev && a.ev !== 'OFF' ? 'ON' : 'OFF');
  });

  $('step').addEventListener('click', (ev) => {
    busy(ev.target, async () => {
      const reading = radio('reading');
      const out = await NW_API.step(runId, op.clock.interval_index, { pump: radio('pump'), ev: radio('ev') }, reading);
      setRadio('reading', 'normal');
      const verdict = (v, label) => (v ? html`<p>${label}: ${chip(fmtStatus(v.observation_status))} ${fmtReason(v.reason)}</p>` : '');
      put('step-result', html`
        <h3 style="margin-top:.75rem">Interval ${out.stepped_interval} checked</h3>
        ${verdict(out.execution, 'This interval')}
        ${verdict(out.late_execution, `Interval ${out.late_execution?.interval_index}, from a late reading`)}
        ${!out.execution && !out.late_execution ? html`<p>No accepted plan covered this interval, so there was nothing to check.</p>` : ''}
        ${out.state?.pending_proposals?.length ? html`<p>A proposal is waiting for the manager.</p>` : ''}`);
    });
  });

  const tip = (ev) => {
    const bar = ev.target.closest?.('.bar');
    if (bar) $('readout').textContent = bar.dataset.tip;
  };
  document.addEventListener('mouseover', tip);
  document.addEventListener('focusin', tip);
  document.addEventListener('click', tip);

  $('signin-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const button = ev.target.querySelector('button');
    button.disabled = true;
    try {
      await NW_API.signIn({ access_code: $('access-code').value });
      $('access-code').value = '';
      toast();
      await enter();
    } catch (e) {
      toast('bad', fmtError(e));
    }
    button.disabled = false;
  });

  $('sign-out').addEventListener('click', () => {
    NW_API.signOut();
    lastJson = null;
    op = null;
    showSignIn();
  });

  NW_API.init('operator', (e) => { lastJson = null; showSignIn(fmtError(e)); });
  if (NW_API.hasSession()) enter(); else showSignIn();
  setInterval(() => { if (NW_API.hasSession() && !document.hidden && !$('app').hidden) refresh(); }, POLL_MS);
})();
