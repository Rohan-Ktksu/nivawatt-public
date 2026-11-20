// Display helpers shared by both views. Everything here formats server values; nothing computes a
// schedule or a metric. Unknown stays unknown: null never renders as zero.
const NW_UI = (() => {
  const ENTITIES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ENTITIES[c]);

  class Safe { constructor(s) { this.s = s; } toString() { return this.s; } }
  const piece = (v) => {
    if (v === null || v === undefined || v === false || v === true) return '';
    if (v instanceof Safe) return v.s;
    if (Array.isArray(v)) return v.map(piece).join('');
    return esc(v);
  };
  // Tagged template: every interpolated value is escaped unless it is already html``.
  const html = (strings, ...values) => new Safe(strings.reduce((out, s, i) => out + s + (i < values.length ? piece(values[i]) : ''), ''));

  const IST = { timeZone: 'Asia/Kolkata' };
  const dateTime = new Intl.DateTimeFormat('en-IN', { ...IST, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
  const clock = new Intl.DateTimeFormat('en-IN', { ...IST, hour: '2-digit', minute: '2-digit', hour12: false });
  const fmtTime = (iso) => (iso ? `${dateTime.format(new Date(iso))} IST` : 'Unknown');
  const fmtClock = (iso) => (iso ? clock.format(new Date(iso)) : 'Unknown');

  // Touching forecast hours read as one span; disjoint hours stay separate.
  function mergeWindows(windows) {
    const out = [];
    [...(windows || [])].sort((a, b) => a.start.localeCompare(b.start)).forEach((w) => {
      const last = out[out.length - 1];
      if (last && new Date(last.end).getTime() === new Date(w.start).getTime()) last.end = w.end;
      else out.push({ ...w });
    });
    return out;
  }
  const fmtWindows = (windows) => mergeWindows(windows).map((w) => `${fmtClock(w.start)} to ${fmtClock(w.end)} IST`);

  const inWindows = (iso, windows) => {
    const t = new Date(iso).getTime();
    return (windows || []).some((w) => new Date(w.start).getTime() <= t && t < new Date(w.end).getTime());
  };

  const intervalTime = (c, i) => new Date(new Date(c.clock_start_utc).getTime() + i * c.model_dt_minutes * 60000).toISOString();

  function fmtNum(v, unit, digits = 2) {
    if (v === null || v === undefined) return 'Unknown';
    const n = Number(v).toLocaleString('en-IN', { maximumFractionDigits: digits });
    return unit ? `${n} ${unit}` : n;
  }

  const SOURCES = {
    synthetic: 'Synthetic simulation, not a measurement',
    synthetic_simulation: 'Synthetic simulation, not a measurement',
    hardware: 'Physical measurement, unscaled',
    scaled_hardware: 'Scaled from bench measurements; model values, not field measurements.',
    historical_replay: 'Historical replay, not a live connection',
  };
  // With both factors known, scaled_hardware names each scale (same sentence as the report's provenance.label), so a
  // volume-only scaled run never reads as if its electrical measurements were model-scaled.
  const scaleWord = (f) => (f === 1 ? 'unscaled (×1)' : `model-scaled (×${f})`);
  const fmtSource = (kind, scales) => {
    const p = scales?.power_scale, v = scales?.volume_scale;
    if (kind === 'scaled_hardware' && p !== null && p !== undefined && v !== null && v !== undefined) {
      return `Bench measurements: electrical measurements ${scaleWord(p)}; water volume ${scaleWord(v)}. Scaled values are model values, not field measurements.`;
    }
    return SOURCES[kind] || `Unrecognised source: ${kind || 'not stated'}`;
  };

  const REASONS = {
    PUMP_NOT_OBSERVED_ON: 'The pump was due on, but the pump meter saw no power.',
    PUMP_POWER_WITHOUT_WATER_DELIVERY: 'The pump drew power, but the tank did not rise. Power alone does not prove water was delivered.',
    PUMP_WATER_DELIVERY_NOT_OBSERVED: 'The pump was due on, but the tank sensor saw no rise in water.',
    PUMP_ON_NOT_PLANNED: 'The pump was running when the plan had it off.',
    EV_CHARGING_NOT_OBSERVED: 'Charging was due, but the charging meter saw no power.',
    EV_CHARGING_NOT_PLANNED: 'Charging was running when the plan had it off.',
    NO_PARTICIPATING_LOAD_OBSERVED: 'An action was due, but the combined meter read zero, so nothing was running.',
    AGGREGATE_ONLY_BRANCH_UNATTRIBUTED: 'The combined meter saw power, but cannot tell whether it came from the pump or the charger.',
    PUMP_CHANNEL_MISSING: 'The pump meter channel sent no reading, so pump power is unknown for this interval.',
    EV_CHANNEL_MISSING: 'The EV charger-group meter channel sent no reading, so charging power is unknown for this interval.',
    COMBINED_CHANNEL_MISSING: 'The combined meter channel sent no reading, so participating load is unknown for this interval.',
  };
  function fmtReason(reason) {
    if (!reason) return '';
    const late = /\s*\(late reading\)\s*$/.test(reason);
    const codes = reason.replace(/\s*\(late reading\)\s*$/, '').split(',').map((c) => c.trim()).filter(Boolean);
    const text = codes.map((c) => REASONS[c] || `Reason code ${c}.`).join(' ');
    return late ? `${text} This came from a reading that arrived late.`.trim() : text;
  }

  const STATUSES = {
    UNOBSERVED: { icon: '…', tone: 'unknown', label: 'Not checked yet', detail: 'No sensor reading covers this commitment yet.' },
    CLAIMED_EXECUTED: { icon: '…', tone: 'unknown', label: 'Reported only', detail: 'A report is not a measurement.' },
    OBSERVED_MATCH: { icon: '✓', tone: 'good', label: 'Sensors agree', detail: 'The readings for this interval match the plan.' },
    OBSERVED_DEVIATION: { icon: '✕', tone: 'bad', label: 'Sensors disagree', detail: 'The readings for this interval do not match the plan.' },
    AMBIGUOUS: { icon: '?', tone: 'unknown', label: 'Cannot tell', detail: 'The reading is consistent with more than one switch position.' },
    STALE: { icon: '?', tone: 'unknown', label: 'No reading', detail: 'No valid reading has arrived for this interval. Missing is not the same as zero.' },
    OUTAGE: { icon: '!', tone: 'warn', label: 'Outage', detail: 'An outage covers this interval.' },
  };
  const fmtStatus = (code) => STATUSES[code] || { icon: '?', tone: 'unknown', label: code || 'Unknown', detail: '' };

  const REVISION_REASONS = {
    PEAK_ADVISORY_ISSUED: 'The operator issued a peak advisory.',
    ALLOWANCE_CHANGED: 'The operator changed the participating-load allowance.',
    OUTAGE_ANNOUNCED: 'The operator announced a water source outage.',
    REQUIREMENT_CHANGED: 'The charging requirement was updated.',
    OBSERVED_DEVIATION: 'Sensors saw something different from the accepted plan.',
    CLAIM_WITHOUT_OBSERVED_EXECUTION: 'An action was reported done, but sensors did not see it happen.',
    PROPOSAL_STALE_AT_ACCEPTANCE: 'The previous plan expired because time moved on. This is the updated plan.',
    RECONSIDER_DEFERRED: 'A change held back earlier is now due for a decision.',
  };
  const fmtRevisionReason = (code) => REVISION_REASONS[code] || `Reason code ${code}.`;

  const QUALITY = {
    VALID: { icon: '✓', tone: 'good', label: 'On time', detail: 'Arrived in the interval it describes.' },
    LATE: { icon: '!', tone: 'warn', label: 'Arrived late', detail: 'A real reading, delivered after its interval. It is not live.' },
    STALE: { icon: '?', tone: 'unknown', label: 'Missing', detail: 'No reading for this interval. Values are unknown, not zero.' },
  };
  const fmtQuality = (q) => QUALITY[q] || { icon: '?', tone: 'unknown', label: q || 'No reading yet', detail: 'No reading has arrived yet.' };

  const fmtScope = (scope) => ({
    branch: 'Separate pump and charger meters, plus a tank sensor',
    aggregate: 'One combined meter, plus a tank sensor. It cannot prove which load ran',
  }[scope] || 'Not stated by the service');

  // Device runs send an interval summary; a reading posted to a simulator run arrives as one raw sample.
  function fmtRawBench(raw) {
    if (!raw) return null;
    if (raw.sample_count === undefined) return `One reading from ${raw.device_id || 'an unnamed device'}, in raw device units.`;
    const ignored = raw.excluded_samples ? ` ${raw.excluded_samples} duplicate or out-of-order ignored.` : '';
    if (!raw.sample_count) return `No readings arrived for this interval.${ignored}`;
    const count = `${fmtNum(raw.sample_count)} ${raw.sample_count === 1 ? 'reading' : 'readings'}.`;
    const branches = raw.mean_pump_w === null && raw.mean_ev_load_w === null
      ? `Mean combined ${fmtNum(raw.mean_aggregate_w, 'W')}.`
      : `Mean pump ${fmtNum(raw.mean_pump_w, 'W')}, mean charger ${fmtNum(raw.mean_ev_load_w, 'W')}.`;
    return `${count} ${branches}${ignored}`;
  }

  // What the 3D scene may show: only what the latest observation measured. The plan is not an input, so accepting or
  // revising a plan can never move the scene. A missing or unattributable value is 'unknown', never 'off' or zero.
  function sceneState(obs, tankMaxL) {
    const usable = Boolean(obs) && (obs.quality === 'VALID' || obs.quality === 'LATE');
    const channel = (id) => (obs?.meter?.channels || []).find((c) => c.channel_id === id);
    const kw = (field, id) => {
      if (!usable) return null;
      const ch = channel(id);
      if (ch) return ch.quality === 'MISSING' ? null : ch.average_power_model_kw;
      return obs[field] ?? null;
    };
    const onOff = (v) => (v === null || v === undefined ? 'unknown' : v > 1e-6 ? 'on' : 'off');
    const pumpKw = kw('pump_model_kw', 'pump');
    const evKw = kw('ev_model_kw', 'ev_group');
    const tankL = usable && obs.tank_l !== null && obs.tank_l !== undefined ? obs.tank_l : null;
    return {
      interval: obs ? obs.interval_index : null, quality: obs ? obs.quality : null,
      pump: onOff(pumpKw), pumpKw, charger: onOff(evKw), evKw, tankL,
      tankPct: tankL === null || !tankMaxL ? null : Math.max(0, Math.min(100, (tankL / tankMaxL) * 100)),
    };
  }

  // Where a reading came from, stated beside the values. The device emulator posts synthetic telemetry; the server's
  // simulator produces synthetic observations; anything else uses the source wording above.
  const fmtReadingSource = (observationSource, sourceKind) => (sourceKind === 'synthetic'
    ? (observationSource === 'telemetry' ? 'Synthetic emulator reading' : 'Synthetic simulator reading')
    : fmtSource(sourceKind));

  function fmtAction(action) {
    if (!action) return 'No action scheduled';
    const pump = action.pump === 'ON' ? 'Turn the pump on' : 'Keep the pump off';
    const ev = action.ev && action.ev !== 'OFF' ? `charge ${action.ev}` : 'keep charging off';
    return `${pump}, ${ev}`;
  }

  // The recommended next action comes from the accepted commitment only. A pending proposal is never advice to act on.
  function recommendation(state) {
    const pending = state.pending_proposals || [];
    const commitment = state.active_commitment;
    const ended = state.clock.interval_index >= state.clock.horizon_intervals;
    if (ended) return { title: 'This run has finished', why: ['The simulated day is over. See the outcome below.'] };
    if (!commitment) {
      if (pending.length) return { title: 'Review the proposal', why: ['Nothing applies until you accept it. Accepting does not switch any equipment.'] };
      if (!state.active_advisory) return { title: 'No action yet', why: ['The operator has not issued a peak advisory for this run.'] };
      return { title: 'Request a new proposal', why: ['There is no accepted plan and no open proposal. Use the charging requirement form to request one.'] };
    }
    const action = state.execution?.next_action;
    const why = [];
    const a = state.apartment;
    if (action?.pump === 'ON') why.push(`The tank is at ${fmtNum(a.model_tank_l, 'L', 0)} and the plan refills it to stay above the ${fmtNum(a.model_tank_min_l, 'L', 0)} reserve.`);
    if (action?.ev && action.ev !== 'OFF') {
      const s = a.ev_sessions.find((x) => x.ev_id === action.ev);
      if (s) why.push(`${s.ev_id} needs charging before ${fmtClock(intervalTime(state.clock, s.departure_interval))} IST.`);
    }
    if (action && action.pump !== 'ON' && (!action.ev || action.ev === 'OFF')) {
      why.push(inWindows(state.clock.model_time_utc, state.active_advisory?.peak_windows)
        ? 'This is an advisory peak hour and nothing needs to run.'
        : 'The accepted plan has nothing to run in this interval.');
    }
    if (state.observation && fmtQuality(state.observation.quality).tone === 'unknown') why.push('The latest reading is missing, so this advice relies on the last valid reading.');
    if (pending.length) why.push('A newer proposal is waiting below. Until you accept it, this is still your plan.');
    return { title: fmtAction(action), why };
  }

  // Shortfalls exactly as the scheduler quantified them.
  function shortfalls(option) {
    if (!option) return [];
    const out = [];
    Object.entries(option.ev_shortfall_model_kwh || {}).forEach(([ev, kwh]) => {
      if (kwh > 0) out.push(`${ev} would leave ${fmtNum(kwh, 'kWh (model)')} short of its request.`);
    });
    const w = option.water_shortfall || {};
    if (w.intervals_below_critical > 0) out.push(`The tank would be below the critical level (${fmtNum(w.critical_level_l, 'L', 0)}) for ${w.intervals_below_critical} intervals.`);
    if (w.intervals_below_reserve > 0) out.push(`The tank would be below reserve for ${w.intervals_below_reserve} intervals.`);
    if (w.unmet_withdrawal_l > 0) out.push(`${fmtNum(w.unmet_withdrawal_l, 'L', 0)} of water demand would go unmet.`);
    return out;
  }

  // Consecutive intervals as clock ranges, e.g. ['02:00 to 03:00 IST'].
  function intervalRanges(intervals, clock) {
    const sorted = [...intervals].sort((a, b) => a - b);
    const out = [];
    sorted.forEach((i) => {
      const last = out[out.length - 1];
      if (last && i === last[1] + 1) last[1] = i; else out.push([i, i]);
    });
    return out.map(([a, b]) => `${fmtClock(intervalTime(clock, a))} to ${fmtClock(intervalTime(clock, b + 1))} IST`);
  }

  // What changed between an expired plan option and its replacement: timings from now on, and targets.
  function planChanges(oldOpt, newOpt, clock, fromInterval) {
    if (!oldOpt || !newOpt) return [];
    const from = fromInterval ?? 0;
    const on = (opt, kind) => (opt.actions || [])
      .filter((a) => a.interval_index >= from && (kind === 'pump' ? a.pump === 'ON' : a.ev !== 'OFF'))
      .map((a) => a.interval_index);
    const out = [];
    [['pump', 'Pump'], ['ev', 'Charging']].forEach(([kind, label]) => {
      const a = intervalRanges(on(oldOpt, kind), clock).join(', ') || 'none';
      const b = intervalRanges(on(newOpt, kind), clock).join(', ') || 'none';
      if (a !== b) out.push(`${label}: was ${a}; now ${b}.`);
    });
    const evs = new Set([...Object.keys(oldOpt.ev_shortfall_model_kwh || {}), ...Object.keys(newOpt.ev_shortfall_model_kwh || {})]);
    evs.forEach((ev) => {
      const a = (oldOpt.ev_shortfall_model_kwh || {})[ev] ?? 0;
      const b = (newOpt.ev_shortfall_model_kwh || {})[ev] ?? 0;
      if (a !== b) out.push(`${ev} expected shortfall: was ${fmtNum(a, 'kWh (model)')}; now ${fmtNum(b, 'kWh (model)')}.`);
    });
    const wa = oldOpt.water_shortfall?.intervals_below_reserve ?? 0;
    const wb = newOpt.water_shortfall?.intervals_below_reserve ?? 0;
    if (wa !== wb) out.push(`Intervals below the water reserve: was ${wa}; now ${wb}.`);
    if (!out.length) out.push('Timings and targets are the same from now on; only the start time moved.');
    return out;
  }

  const plural = (n, one, many) => `${fmtNum(n)} ${n === 1 ? one : many}`;

  // One "cannot be fully met" item from proposal.infeasibilities, as a sentence. Unknown kinds name the kind only,
  // never raw JSON.
  function fmtInfeasibility(x, clock) {
    if (x === null || x === undefined) return 'Part of this plan cannot be fully met. The service gave no detail.';
    if (typeof x === 'string') return x;
    if (x.kind === 'WATER_RESERVE') {
      const from = Number.isInteger(x.first_interval) && clock ? `, starting ${fmtClock(intervalTime(clock, x.first_interval))} IST` : '';
      const unmet = x.unmet_withdrawal_l > 0 ? ` About ${fmtNum(x.unmet_withdrawal_l, 'L', 0)} of water demand would go unmet.` : '';
      return `The tank is expected to fall below the reserve for ${plural(x.intervals_below_reserve, 'interval', 'intervals')}${from}.${unmet}`;
    }
    if (x.kind === 'EV_ENERGY') {
      return `${x.ev_id || 'An EV'} is expected to be ${fmtNum(x.shortfall_model_kwh, 'kWh (model)')} short of its request at its deadline.`;
    }
    if (x.message) return String(x.message);
    return `Part of this plan cannot be fully met (type ${x.kind || 'not stated'}). The service gave no further detail.`;
  }

  // Shortfall triple used by operator_intervention: { intervals_below_critical, intervals_below_reserve, ev_shortfall_model_kwh }.
  function fmtServiceShortfall(s) {
    if (!s) return 'not stated';
    const parts = [];
    if (s.intervals_below_critical > 0) parts.push(`${plural(s.intervals_below_critical, 'interval', 'intervals')} below the critical water level`);
    if (s.intervals_below_reserve > 0) parts.push(`${plural(s.intervals_below_reserve, 'interval', 'intervals')} below the water reserve`);
    if (s.ev_shortfall_model_kwh > 0) parts.push(`${fmtNum(s.ev_shortfall_model_kwh, 'kWh (model)')} of EV energy short`);
    return parts.length ? parts.join(', ') : 'no shortfall';
  }

  // operator_intervention as readable lines. Information only: the manager cannot change the allowance.
  function fmtOperatorIntervention(oi) {
    if (!oi || !oi.required) return null;
    const lines = [];
    if (oi.reason === 'ALLOWANCE_BLOCKS_SERVICE') {
      lines.push(`The participating-load allowance of ${fmtNum(oi.current_allowance_model_kw, 'kW (model)')} blocks the requested service. Within it, the plan expects ${fmtServiceShortfall(oi.shortfall_within_allowance)}.`);
      if (oi.allowance_that_would_reduce_shortfall_model_kw !== null && oi.allowance_that_would_reduce_shortfall_model_kw !== undefined) {
        lines.push(`An allowance of ${fmtNum(oi.allowance_that_would_reduce_shortfall_model_kw, 'kW (model)')} would reduce this to ${fmtServiceShortfall(oi.shortfall_at_that_allowance)}.`);
      }
      if (oi.physical_check) lines.push(`Electrical check: ${oi.physical_check}.`);
      lines.push('Only the operator can change the allowance. Accepting a plan does not change it. Until the operator decides, the current allowance stays and the shortfall above is expected.');
    } else if (oi.reason === 'SERVICE_INFEASIBLE_WITHIN_LIMITS') {
      lines.push(`No schedule meets the requirement within current limits. The allowance is not the cause: the EV time window, charger rating, water supply or tank size is. Expected: ${fmtServiceShortfall(oi.shortfall_within_allowance)}.`);
      lines.push('The requirement itself needs a decision: change it, or agree the shortfall with the operator.');
    } else {
      lines.push(`The operator needs to act (reason ${oi.reason || 'not stated'}). Expected: ${fmtServiceShortfall(oi.shortfall_within_allowance)}.`);
    }
    return lines;
  }

  // Why the plan stays unchanged when a recomputed plan was withheld or deferred (discrepancy_status.revision).
  // Returns null when a proposal is awaiting the manager or no revision was considered.
  function fmtRevisionHold(rev) {
    if (!rev || (rev.status !== 'DEFERRED' && rev.status !== 'NOT_NEEDED')) return null;
    const title = rev.status === 'DEFERRED'
      ? 'A possible change is on hold. Your accepted plan stays as it is.'
      : 'No change is needed. Your accepted plan stays as it is.';
    const lines = [rev.explanation || 'The service did not say why.'];
    const a = rev.current_plan_predicted;
    const b = rev.recomputed_plan_predicted;
    if (a && b) {
      lines.push(`Current plan: ${plural(a.intervals_below_reserve, 'interval', 'intervals')} below reserve, ${fmtNum(a.ev_shortfall_model_kwh, 'kWh (model)')} EV energy short. Recomputed plan: ${plural(b.intervals_below_reserve, 'interval', 'intervals')} below reserve, ${fmtNum(b.ev_shortfall_model_kwh, 'kWh (model)')} short.`);
    }
    lines.push('There is nothing to accept.');
    return { title, lines };
  }

  // Critical level, reserve and readiness target are three different levels.
  function waterLevels(option, reserveL) {
    const w = option?.water_shortfall || {};
    const row = (cls) => (option?.constraint_report || []).find((r) => r.class === cls);
    const out = [];
    if (w.critical_level_l > 0) {
      out.push({ label: 'Critical level', value: fmtNum(w.critical_level_l, 'L', 0), note: `Must not be crossed. ${w.intervals_below_critical > 0 ? `Expected below it for ${plural(w.intervals_below_critical, 'interval', 'intervals')}.` : 'Expected to hold.'} Demonstration assumption.` });
    } else {
      out.push({ label: 'Critical level', value: 'Not set', note: 'No separate critical level is configured for this run.' });
    }
    out.push({ label: 'Reserve', value: fmtNum(reserveL, 'L', 0), note: w.intervals_below_reserve > 0 ? `Expected below it for ${plural(w.intervals_below_reserve, 'interval', 'intervals')}.` : 'Expected to hold.' });
    if (w.readiness_margin_l > 0) {
      const st = row('readiness_margin')?.status;
      const note = st === 'dropped_to_protect_service' ? 'Dropped in this plan: keeping it would have hurt water or EV service.'
        : w.intervals_below_readiness_margin > 0 ? `Not met for ${plural(w.intervals_below_readiness_margin, 'interval', 'intervals')}.` : 'Met.';
      out.push({ label: 'Readiness target', value: reserveL === null || reserveL === undefined ? `Reserve + ${fmtNum(w.readiness_margin_l, 'L', 0)}` : fmtNum(reserveL + w.readiness_margin_l, 'L', 0), note: `Reserve plus a ${fmtNum(w.readiness_margin_l, 'L', 0)} margin, filled outside peak hours if possible. A demonstration target, not a requirement. ${note}` });
    } else {
      out.push({ label: 'Readiness target', value: 'None', note: 'No extra margin above the reserve is set for this run.' });
    }
    return out;
  }

  // Minimum pump on/off durations: demonstration assumptions, and any unavoidable shorter run.
  function pumpRules(option, clock) {
    const r = option?.pump_duration_rules;
    if (!r) return [];
    const mins = (n) => (clock ? fmtNum(n * clock.model_dt_minutes, 'min') : plural(n, 'interval', 'intervals'));
    const out = [`Once started, the pump runs at least ${mins(r.min_on_intervals)} and then rests at least ${mins(r.min_off_intervals)}. These are demonstration assumptions, not a real pump's specification.`];
    (option.pump_duration_exceptions || []).forEach((e) => {
      const at = Number.isInteger(e.interval_index) && clock ? ` at ${fmtClock(intervalTime(clock, e.interval_index))} IST` : '';
      out.push(e.kind === 'MIN_ON_NOT_MET'
        ? `A shorter run is planned${at}: ${e.reason || 'no full-length run fits'}.`
        : `Exception${at}: ${e.reason || e.kind || 'not described'}.`);
    });
    return out;
  }

  // One meter channel reading as display rows. Missing values stay Unknown, never 0.
  const CHANNEL_QUALITY = {
    VALID: { icon: '✓', tone: 'good', label: 'On time' },
    LATE: { icon: '!', tone: 'warn', label: 'Arrived late' },
    MISSING: { icon: '?', tone: 'unknown', label: 'Missing' },
  };
  // Raw counters are shown only as the service stored them; they are never rebuilt from model values.
  const known = (v) => v !== null && v !== undefined;
  function rawRows(ch, m) {
    const device = !!m && m.source_kind !== 'synthetic';
    const scale = m?.power_scale;
    if (!device && (!known(scale) || scale === 1)) return []; // simulated at scale 1: nothing to separate
    if (!known(ch.raw_counter_kwh)) return [['Raw meter reading', device ? 'Raw measurement unavailable' : 'Raw emulator counter unavailable']];
    if (device && scale === 1) return []; // unscaled: the model values above are the meter's own readings
    const kind = device ? 'kWh (raw, before scale)' : 'kWh (raw emulator counter, simulated, before scale)';
    return [['Raw energy this interval', fmtNum(ch.raw_counter_increase_kwh, kind, 4)],
      ['Raw counter', fmtNum(ch.raw_counter_kwh, kind, 4)]];
  }
  // Electrical and water-volume scales are stated separately: scaling one never implies the other is scaled.
  function fmtMeterScale(m) {
    if (!m) return [];
    const device = m.source_kind !== 'synthetic';
    const p = m.power_scale, v = m.volume_scale;
    if (!device && (!known(p) || p === 1) && (!known(v) || v === 1)) return []; // simulated at scale 1: nothing to add
    if (!known(p)) return device ? ['Scale factor not recorded with these readings.'] : [];
    const what = device ? 'Electrical measurements' : 'Electrical readings (simulated)';
    const lines = [p === 1 ? `${what}: unscaled (×1). Model values are the meter's own readings.`
      : `${what}: model-scaled (×${p}). Model kWh = raw counter kWh × ${p}. Time is not scaled.`];
    if (known(v)) lines.push(v === 1 ? 'Water volume: unscaled (×1).' : `Water volume: model-scaled (×${v}).`);
    return lines;
  }
  const METER_CHIP = { synthetic: 'Simulated', hardware: 'Measured', scaled_hardware: 'Scaled' };
  function fmtMeterChannel(ch, clock, m) {
    const q = CHANNEL_QUALITY[ch.quality] || { icon: '?', tone: 'unknown', label: ch.quality || 'Unknown' };
    const span = ch.interval_start_utc && ch.interval_end_utc
      ? `${fmtClock(ch.interval_start_utc)} to ${fmtClock(ch.interval_end_utc)} IST model time` : 'Interval not stated';
    const group = ch.grouped ? `Group of ${ch.devices.length}: ${ch.devices.join(', ')}. One total for the group, not per charger or vehicle.`
      : `Device: ${(ch.devices || []).join(', ') || 'not stated'}`;
    const session = ch.meter_session !== null && ch.meter_session !== undefined ? ` (since meter start ${ch.meter_session})` : '';
    return {
      title: ch.label || ch.channel_id, group, quality: q, span,
      rows: [
        ['Power', fmtNum(ch.average_power_model_kw, 'kW (model)', 3)],
        ['Energy this interval', fmtNum(ch.interval_energy_model_kwh, 'kWh (model)', 3)],
        ['Energy counter', `${fmtNum(ch.cumulative_energy_model_kwh, 'kWh (model)', 3)}${ch.cumulative_energy_model_kwh === null || ch.cumulative_energy_model_kwh === undefined ? '' : session}`],
        ...rawRows(ch, m),
        ['Reading number', fmtNum(ch.sequence_number)],
      ],
      restarted: ch.counter_restarts > 0 ? 'The meter restarted in this interval. Energy before the restart was not counted twice.' : null,
      recovered: ch.recovered_after_missing ? `After ${ch.recovered_after_missing.missing_intervals} missing ${ch.recovered_after_missing.missing_intervals === 1 ? 'reading' : 'readings'}, the counter showed ${fmtNum(ch.recovered_after_missing.recovered_unassigned_model_kwh, 'kWh (model)', 3)}${ch.recovered_after_missing.lower_bound ? ' or more' : ''} used between ${fmtClock(ch.recovered_after_missing.span_start_utc)} and ${fmtClock(ch.recovered_after_missing.span_end_utc)} IST. When within that span is unknown, so it is not given to any interval or vehicle.${ch.recovered_after_missing.restart_in_gap ? ' The meter restarted in the gap; energy before the restart is unknown.' : ''}` : null,
    };
  }
  // Bill analysis: money is never shown for a missing value; statuses in plain words.
  const fmtRs = (v) => (v === null || v === undefined ? 'Unknown' : `Rs.${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const BILL_STATUS = {
    complete: { icon: '✓', tone: 'good', label: 'Complete' },
    partial: { icon: '?', tone: 'unknown', label: 'Partial: some charges are unknown, so no total is shown' },
    not_estimable: { icon: '?', tone: 'unknown', label: 'Cannot be estimated from the inputs available' },
  };
  const fmtBillStatus = (s) => BILL_STATUS[s] || { icon: '?', tone: 'unknown', label: `Unrecognised status: ${s || 'not stated'}` };
  function fmtSaving(diffRs, comparable) {
    if (!comparable) return 'Not estimable: the two runs did not deliver the same water and EV service.';
    if (diffRs === null || diffRs === undefined) return 'Unknown.';
    if (Math.abs(diffRs) < 0.005) return 'Zero: no difference in the charges that were calculated.';
    return diffRs < 0 ? `Lower by ${fmtRs(-diffRs)} (a saving).` : `Higher by ${fmtRs(diffRs)} (a negative saving).`;
  }
  function fmtMeterTotal(total, channels) {
    if (!total) return null;
    if (!total.complete) {
      const names = (total.missing_channels || []).map((id) => (channels || []).find((c) => c.channel_id === id)?.label || id);
      return { complete: false, text: `Incomplete: no reading from ${names.join(', ') || 'a channel'}. The total is unknown, not zero.` };
    }
    return { complete: true, text: `${fmtNum(total.average_power_model_kw, 'kW (model)', 3)}, ${fmtNum(total.interval_energy_model_kwh, 'kWh (model)', 3)} this interval. Sum of separate channels; no device is counted twice.` };
  }

  const fmtError = (e) => ({
    PROPOSAL_STALE: 'This plan has expired because time has moved on. Review the updated plan.',
    STALE_PROPOSAL: 'This proposal is no longer open. Its version changed, or it expired or was replaced. The screen now shows the latest state.',
    WRONG_RUN: 'This proposal belongs to a different run. The screen now shows the latest state.',
    OPERATOR_EXCEPTION_REQUIRED: 'This option goes over the allowance, so only the operator can decide it.',
    CLAIM_OUT_OF_RANGE: 'That interval is outside the current commitment. The screen now shows the latest state.',
    RUN_FINISHED: 'This run has reached the end of its simulated day. Create a new run to continue.',
    UNAUTHENTICATED: 'Your demo session has ended. Sign in again.',
    INVALID_ACCESS_CODE: 'That access code was not accepted. Check it and try again.',
    FORBIDDEN_ROLE: 'This session does not have permission for that action.',
    FORBIDDEN_APARTMENT: 'This session belongs to another apartment.',
    UNKNOWN_RUN: 'No run has that ID. Check the run link from the operator.',
    UNKNOWN_APARTMENT: 'That apartment is not part of this run. Check the apartment ID.',
    DATASET_UNAVAILABLE: 'The demand dataset is not on this computer. Run scripts/fetch_kptcl.py, then restart the service.',
  }[e.code] || e.message);

  return { esc, html, sceneState, fmtReadingSource, fmtTime, fmtClock, mergeWindows, fmtWindows, inWindows, intervalTime, fmtNum, fmtSource, fmtReason, fmtStatus, fmtRevisionReason, fmtQuality, fmtScope, fmtRawBench, fmtAction, recommendation, shortfalls, intervalRanges, planChanges, fmtInfeasibility, fmtServiceShortfall, fmtMeterChannel, fmtMeterScale, METER_CHIP, fmtMeterTotal, fmtRs, fmtBillStatus, fmtSaving, fmtOperatorIntervention, fmtRevisionHold, waterLevels, pumpRules, fmtError };
})();

if (typeof module !== 'undefined') module.exports = NW_UI;
