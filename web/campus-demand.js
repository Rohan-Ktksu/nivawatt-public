/* Charts for the switch-driven day and the separate, reproducible monthly billing replay. */
const NW_CAMPUS_DEMAND = (() => {
  const { html, fmtNum, fmtRs } = NW_UI;
  const $ = (id) => document.getElementById(id);
  const peak = (values) => values.length ? Math.max(...values) : null;
  const number = (value, unit) => value == null ? 'Unavailable' : fmtNum(value, unit, 2);
  const saving = (value) => value == null ? 'Unavailable' :
    value < 0 ? `Cost increase: ${fmtRs(-value)}` : fmtRs(value);

  function chart(rows, unit, baselineName, variantName, limit = null, timeAt = null) {
    if (!rows.length) return html`<p>No intervals have closed yet.</p>`;
    const actual = rows.some((r) => r[1] != null);
    const samples = rows.flat().filter(Number.isFinite);
    if (limit != null) samples.push(limit);
    const max = Math.max(1, ...samples);
    const min = Math.min(...samples);
    const lo = Math.max(0, min - (max - min) * 0.12 - 1);
    const hi = max + (max - lo) * 0.1;
    const x = (i) => 60 + 670 * i / Math.max(1, rows.length - 1);
    const y = (v) => 176 - 145 * (v - lo) / (hi - lo);
    function path(series) {
      let started = false;
      return rows.map((row, i) => {
        const value = row[series];
        if (value == null) { started = false; return ''; }
        const command = started ? 'L' : 'M';
        started = true;
        return `${command}${x(i).toFixed(2)} ${y(value).toFixed(2)}`;
      }).join(' ');
    }
    const dots = rows.length <= 4 ? rows.map((r, i) => r.map((v, series) =>
      v == null ? '' : html`<circle cx="${x(i)}" cy="${y(v)}" r="4" class="${series ? 'demand-actual' : 'demand-base'}"/>`)) : [];
    const ticks = [...new Set([0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * (rows.length - 1))))];
    return html`
      <svg class="demand-chart" viewBox="0 0 750 200" role="img" aria-label="${baselineName} versus ${variantName} in ${unit}">
        ${[0, 0.5, 1].map((f) => lo + f * (hi - lo)).map((v) => html`
          <line x1="60" y1="${y(v)}" x2="730" y2="${y(v)}" class="demand-gridline"/>
          <text x="53" y="${y(v) + 4}" text-anchor="end">${v.toFixed(1)}</text>`)}
        ${limit == null ? '' : html`<line x1="60" y1="${y(limit)}" x2="730" y2="${y(limit)}" class="demand-limit"/>`}
        <path d="${path(0)}" class="demand-base"/><path d="${path(1)}" class="demand-actual"/>
        ${dots.flat()}
        ${timeAt ? ticks.map((i) => html`<text x="${x(i)}" y="197" text-anchor="${i === rows.length - 1 ? 'end' : 'middle'}">${timeAt(i)}</text>`) : ''}
      </svg>
      <p class="demand-legend"><span class="key-base">${baselineName}</span>
        <span class="key-actual">${variantName}</span>
        ${limit == null ? '' : html`<span class="key-limit">Contract demand ${fmtNum(limit, unit)}</span>`}</p>
      ${actual ? '' : html`<p>No usable readings for the selected run yet.</p>`}`;
  }

  function siteRows(traces) {
    const longest = Math.max(0, ...traces.map((x) => x.length));
    return Array.from({ length: longest }, (_, i) => [0, 1].map((s) => {
      const values = traces.map((x) => x[i]?.[s]);
      return values.every(Number.isFinite) ? values.reduce((a, b) => a + b, 0) : null;
    }));
  }

  function peaks(rows) {
    // Compare over the same covered intervals. Billing remains unavailable on the live one-day run.
    const matched = rows.filter((r) => r.every(Number.isFinite));
    return [0, 1].map((s) => peak(matched.map((r) => r[s])));
  }

  function live(traces, selected, clock) {
    const target = $('live-demand-content');
    if (!traces?.[selected]) {
      target.textContent = 'Waiting for the comparison trace.';
      return;
    }
    if (typeof traces[selected].error === 'string') {
      target.textContent = traces[selected].error;
      return;
    }
    const rows = traces[selected].points;
    const all = Object.values(traces);
    const site = all.length === 4 && all.every((t) => !t.error && t.start_utc === traces[selected].start_utc)
      ? siteRows(all.map((t) => t.points)) : [];
    const [a, b] = peaks(rows);
    const [c, d] = peaks(site);
    const coverage = rows.filter((r) => r[1] !== null).length;
    const siteCoverage = site.filter((r) => r.every(Number.isFinite)).length;
    const start = Date.parse(traces[selected].start_utc);
    const timeAt = (i) => new Date(start + i * 15 * 60000).toLocaleTimeString('en-GB', {
      timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit',
    });
    target.innerHTML = String(html`
      <div class="columns wide-left">
        <div><h3>${selected} · Whole-building demand (kW)</h3>${chart(rows, 'kW', 'Normal operation', 'Your switch settings', null, timeAt)}</div>
        <dl class="facts">
          <dt>Intervals covered</dt><dd>${coverage} of ${clock}</dd>
          <dt>Normal peak</dt><dd>${number(a, 'kW')}</dd>
          <dt>Run peak</dt><dd>${number(b, 'kW')}</dd>
          <dt>Building peak reduction</dt><dd>${a === null ? 'Unavailable' : number(a - b, 'kW')}</dd>
          <dt>Site peak reduction</dt><dd>${c === null ? 'Unavailable' : number(c - d, 'kW')}</dd>
          <dt>Site coverage</dt><dd>${siteCoverage} of ${clock} · ${siteCoverage === clock && clock > 0 ? 'Complete' : 'Partial data'}</dd>
        </dl>
      </div>
      <p class="muted">Both traces use the same synthetic building background. Normal operation uses tank control and starts charging on arrival. Gaps show missing readings. Peaks use matched 15-minute intervals. See the monthly replay for estimated bill savings.</p>`);
  }

  let data, shown = 0, timer = null;
  function stop() {
    if (timer !== null) clearInterval(timer);
    timer = null;
    $('billing-play').textContent = 'Play month';
  }

  function renderBilling() {
    const members = data.buildings;
    const size = members[0].blocks.length;
    const finished = shown === size;
    const selected = $('billing-view').value;
    const building = members.find((x) => x.apartment_id === selected);
    const site = siteRows(members.map((b) => b.blocks.slice(0, shown)));
    const rows = building ? building.blocks.slice(0, shown).map((r) => r.slice(2)) : site;
    const [baselinePeak, runPeak] = peaks(rows);
    const unit = building ? 'kVA' : 'kW';
    const daySize = data.blocks_per_day;
    const dayNumber = Math.ceil(shown / daySize);
    const dayStart = (dayNumber - 1) * daySize;
    const timeAt = (i) => {
      const minute = (data.start_hour_ist * 60 + i * 30) % 1440;
      return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
    };
    const compare = members.every((m) => m.comparable && m.saving_rs != null);
    const total = compare && finished ? members.reduce((n, m) => n + m.saving_rs, 0) : null;
    const sitePeaks = peaks(site);
    const avoided = members.reduce((n, m) => n + m.blocks.slice(0, shown).filter(
      (v) => v[2] > m.contract_demand_kva && v[3] <= m.contract_demand_kva).length, 0);
    $('billing-time').textContent = `Day ${dayNumber} of ${size / daySize} · ${shown} of ${size} half-hour intervals`;
    $('billing-position').value = shown;
    $('billing-chart').innerHTML = String(html`
      <div class="columns wide-left">
        <div><h3>${building ? building.apartment_id : 'Campus'} · Demand on day ${dayNumber} (${unit})</h3>
          ${chart(rows.slice(dayStart), unit, 'Normal operation', 'Demand-aware schedule', building?.contract_demand_kva, timeAt)}
          <p class="muted">Each replay day starts at ${String(data.start_hour_ist).padStart(2, '0')}:00 IST.</p>
        </div>
        <dl class="facts">
          <dt>Normal peak so far</dt><dd>${number(baselinePeak, unit)}</dd>
          <dt>Demand-aware peak so far</dt><dd>${number(runPeak, unit)}</dd>
          <dt>Peak reduction so far</dt><dd>${baselinePeak === null ? 'Unavailable' : number(baselinePeak - runPeak, unit)}</dd>
          ${building && finished ? html`
            <dt>Normal excess-demand charge</dt><dd>${fmtRs(building.baseline.excess_demand_rs)}</dd>
            <dt>Coordinated excess-demand charge</dt><dd>${fmtRs(building.coordinated.excess_demand_rs)}</dd>
          ` : ''}
          <dt>${building ? building.apartment_id : 'Total site'} estimated savings</dt>
          <dd>${finished ? saving(building ? building.saving_rs : total) : 'Available at month end'}</dd>
        </dl>
      </div>
      <p><strong>Site coincident peak reduction:</strong> ${sitePeaks[0] === null ? 'Unavailable' : number(sitePeaks[0] - sitePeaks[1], 'kW')}
      · <strong>Total estimated savings:</strong> ${finished ? saving(total) : 'Available at month end'}</p>`);
    $('billing-rows').innerHTML = String(html`<p><strong>Excess half-hours avoided across individual buildings:</strong> ${avoided}</p><table class="members billing-rows">
      <thead><tr><th>Building</th><th>Normal → coordinated peak</th><th>Excess windows avoided</th><th>New excess windows</th><th>Monthly saving</th></tr></thead>
      <tbody>${members.map((b) => {
        const r = b.blocks.slice(0, shown);
        const [p, q] = peaks(r.map((v) => v.slice(2)));
        const avoided = r.filter((v) => v[2] > b.contract_demand_kva && v[3] <= b.contract_demand_kva).length;
        const newExcess = r.filter((v) => v[2] <= b.contract_demand_kva && v[3] > b.contract_demand_kva).length;
        return html`<tr><th scope="row">${b.apartment_id}</th><td>${number(p, 'kVA')} → ${number(q, 'kVA')}</td>
          <td>${avoided}</td><td>${newExcess}</td><td>${!finished ? 'Pending' : b.comparable ? saving(b.saving_rs) : 'Service differs'}</td></tr>`;
      })}</tbody></table>`);
  }

  async function load() {
    if (data) return;
    $('billing-replay-controls').hidden = true;
    $('billing-status').textContent = 'Loading the synthetic month…';
    try {
      const response = await fetch('/web/campus-billing-data.json');
      if (!response.ok) throw new Error('missing data');
      data = await response.json();
      shown = data.buildings[0].blocks.length;
      $('billing-position').max = shown;
      $('billing-replay-controls').hidden = false;
      $('billing-status').textContent = 'Synthetic monthly comparison, separate from the live campus controls. Four separately billed HT-4 connections. Results assume the recorded contract demand and equal water and EV service.';
      renderBilling();
    } catch {
      data = null;
      $('billing-status').textContent = 'The monthly replay data could not be loaded. Close and reopen this section to retry.';
    }
  }

  $('billing-replay').addEventListener('toggle', (e) => { if (e.target.open) load(); else stop(); });
  $('billing-view').addEventListener('change', renderBilling);
  $('billing-position').addEventListener('input', (e) => { stop(); shown = Number(e.target.value); renderBilling(); });
  $('billing-end').addEventListener('click', () => { stop(); shown = data.buildings[0].blocks.length; renderBilling(); });
  $('billing-play').addEventListener('click', () => {
    if (timer !== null) { stop(); return; }
    const end = data.buildings[0].blocks.length;
    if (shown >= end) shown = 1;
    $('billing-play').textContent = 'Pause';
    renderBilling();
    timer = setInterval(() => {
      shown = Math.min(end, shown + Number($('billing-speed').value));
      renderBilling();
      if (shown === end) stop();
    }, 320);
  });
  return { live };
})();
