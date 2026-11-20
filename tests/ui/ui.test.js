// Run with: node --test tests/ui/ui.test.js
const test = require('node:test');
const assert = require('node:assert');
const ui = require('../../web/ui.js');

test('interpolated values are escaped', () => {
  assert.strictEqual(String(ui.html`<p>${'<img onerror=x>'}</p>`), '<p>&lt;img onerror=x&gt;</p>');
  assert.strictEqual(String(ui.html`<p>${ui.html`<b>ok</b>`}</p>`), '<p><b>ok</b></p>');
});

test('unknown is never rendered as zero, and measured zero stays zero', () => {
  assert.strictEqual(ui.fmtNum(null, 'kW'), 'Unknown');
  assert.strictEqual(ui.fmtNum(undefined, 'kW'), 'Unknown');
  assert.strictEqual(ui.fmtNum(0, 'kW'), '0 kW');
});

test('touching hours merge, disjoint hours stay separate', () => {
  const merged = ui.mergeWindows([
    { start: '2026-08-19T05:30:00Z', end: '2026-08-19T06:30:00Z' },
    { start: '2026-08-19T04:30:00Z', end: '2026-08-19T05:30:00Z' },
    { start: '2026-08-19T12:30:00Z', end: '2026-08-19T13:30:00Z' },
  ]);
  assert.deepStrictEqual(merged, [
    { start: '2026-08-19T04:30:00Z', end: '2026-08-19T06:30:00Z' },
    { start: '2026-08-19T12:30:00Z', end: '2026-08-19T13:30:00Z' },
  ]);
  assert.deepStrictEqual(ui.fmtWindows(merged), ['10:00 to 12:00 IST', '18:00 to 19:00 IST']);
});

test('combined execution reasons and late readings are explained', () => {
  const text = ui.fmtReason('PUMP_NOT_OBSERVED_ON,EV_CHARGING_NOT_OBSERVED (late reading)');
  assert.match(text, /pump meter saw no power/);
  assert.match(text, /charging meter saw no power/);
  assert.match(text, /arrived late/);
  assert.match(ui.fmtReason('NEW_CODE'), /NEW_CODE/);
});

test('no status other than an observed match reads as agreement', () => {
  ['UNOBSERVED', 'CLAIMED_EXECUTED', 'AMBIGUOUS', 'STALE', 'OBSERVED_DEVIATION', 'OUTAGE', 'FUTURE_CODE'].forEach((code) => {
    assert.notStrictEqual(ui.fmtStatus(code).tone, 'good', code);
  });
  assert.strictEqual(ui.fmtStatus('OBSERVED_MATCH').tone, 'good');
});

const baseState = (extra) => ({
  clock: { interval_index: 2, horizon_intervals: 96, model_dt_minutes: 15, clock_start_utc: '2026-08-18T18:30:00Z', model_time_utc: '2026-08-18T19:00:00Z' },
  apartment: { model_tank_l: 4000, model_tank_min_l: 3000, ev_sessions: [{ ev_id: 'EV_1', departure_interval: 32, remaining_model_kwh: 10 }] },
  readiness: {},
  pending_proposals: [],
  active_commitment: null,
  execution: null,
  active_advisory: null,
  observation: null,
  ...extra,
});

test('a pending proposal is never given as the action to take', () => {
  const pending = [{ proposal_id: 'p' }];
  assert.strictEqual(ui.recommendation(baseState({ pending_proposals: pending })).title, 'Review the proposal');
  const r = ui.recommendation(baseState({
    pending_proposals: pending,
    active_commitment: { commitment_id: 'c' },
    execution: { next_action: { pump: 'OFF', ev: 'EV_1' } },
  }));
  assert.strictEqual(r.title, 'Keep the pump off, charge EV_1');
  assert.ok(r.why.some((w) => /still your plan/.test(w)));
});

test('device readings describe both payload shapes, and unknown is not zero', () => {
  assert.strictEqual(ui.fmtRawBench(null), null);
  assert.match(ui.fmtRawBench({ device_id: 'bench-1', pump_power_w: 3 }), /One reading from bench-1/);
  assert.match(ui.fmtRawBench({ sample_count: 0, excluded_samples: 2 }), /No readings arrived.*2 duplicate/);
  assert.strictEqual(ui.fmtRawBench({ sample_count: 15, excluded_samples: 0, mean_pump_w: 0, mean_ev_load_w: 3300 }), '15 readings. Mean pump 0 W, mean charger 3,300 W.');
  assert.strictEqual(ui.fmtRawBench({ sample_count: 1, excluded_samples: 0, mean_pump_w: null, mean_ev_load_w: null, mean_aggregate_w: null }), '1 reading. Mean combined Unknown.');
});

test('only real shortfalls are reported', () => {
  assert.deepStrictEqual(ui.shortfalls({ ev_shortfall_model_kwh: { EV_1: 0 }, water_shortfall: { intervals_below_reserve: 0, unmet_withdrawal_l: 0 } }), []);
  const out = ui.shortfalls({ ev_shortfall_model_kwh: { EV_1: 13.6 }, water_shortfall: { intervals_below_reserve: 2, unmet_withdrawal_l: 50 } });
  assert.strictEqual(out.length, 3);
  assert.match(out[0], /13\.6 kWh/);
});

test('expired plan message and what-changed summary', () => {
  assert.strictEqual(ui.fmtError({ code: 'PROPOSAL_STALE', message: 'raw' }),
    'This plan has expired because time has moved on. Review the updated plan.');
  const clock = { clock_start_utc: '2026-08-18T18:30:00Z', model_dt_minutes: 15 };
  const act = (pump, ev) => Array.from({ length: 12 }, (_, i) => ({ interval_index: i, pump: pump.includes(i) ? 'ON' : 'OFF', ev: ev.includes(i) ? 'EV_1' : 'OFF' }));
  const old = { actions: act([4, 5, 6, 7], [1, 2, 3]), ev_shortfall_model_kwh: { EV_1: 0 }, water_shortfall: { intervals_below_reserve: 0 } };
  const neu = { actions: act([4, 5, 6, 7], [2, 3, 8]), ev_shortfall_model_kwh: { EV_1: 0.1 }, water_shortfall: { intervals_below_reserve: 0 } };
  const ch = ui.planChanges(old, neu, clock, 1);
  assert.deepStrictEqual(ch, [
    'Charging: was 00:15 to 01:00 IST; now 00:30 to 01:00 IST, 02:00 to 02:15 IST.',
    'EV_1 expected shortfall: was 0 kWh (model); now 0.1 kWh (model).',
  ]);
  assert.deepStrictEqual(ui.planChanges(old, old, clock, 0), ['Timings and targets are the same from now on; only the start time moved.']);
  assert.strictEqual(ui.fmtRevisionReason('PROPOSAL_STALE_AT_ACCEPTANCE'), 'The previous plan expired because time moved on. This is the updated plan.');
});

test('cannot-be-met items read as sentences, never raw JSON', () => {
  const clock = { clock_start_utc: '2026-08-18T18:30:00Z', model_dt_minutes: 15 };
  const ev = ui.fmtInfeasibility({ kind: 'EV_ENERGY', ev_id: 'EV_1', shortfall_model_kwh: 3.3 }, clock);
  assert.match(ev, /EV_1 is expected to be 3\.3 kWh \(model\) short/);
  const water = ui.fmtInfeasibility({ kind: 'WATER_RESERVE', intervals_below_reserve: 3, first_interval: 4, unmet_withdrawal_l: 0 }, clock);
  assert.match(water, /below the reserve for 3 intervals, starting 01:00 IST/);
  const odd = ui.fmtInfeasibility({ kind: 'NEW_KIND', detail: { a: 1 } }, clock);
  assert.doesNotMatch(odd, /[{}]/);
  assert.match(odd, /NEW_KIND/);
  assert.strictEqual(String(ui.html`<li>${ui.fmtInfeasibility({ kind: 'EV_ENERGY', ev_id: '<b>x', shortfall_model_kwh: 1 })}</li>`).includes('<b>'), false);
});

test('operator intervention is explained, separates critical/reserve/EV, and offers no override', () => {
  const lines = ui.fmtOperatorIntervention({
    required: true, reason: 'ALLOWANCE_BLOCKS_SERVICE', current_allowance_model_kw: 3,
    shortfall_within_allowance: { intervals_below_critical: 0, intervals_below_reserve: 0, ev_shortfall_model_kwh: 10 },
    allowance_that_would_reduce_shortfall_model_kw: 3.3,
    shortfall_at_that_allowance: { intervals_below_critical: 0, intervals_below_reserve: 0, ev_shortfall_model_kwh: 0 },
    physical_check: 'NOT VERIFIED: value bounded only by assumed connected-load ratings',
  }).join(' ');
  assert.match(lines, /allowance of 3 kW \(model\) blocks/);
  assert.match(lines, /10 kWh \(model\) of EV energy short/);
  assert.match(lines, /3\.3 kW \(model\) would reduce this to no shortfall/);
  assert.match(lines, /NOT VERIFIED/);
  assert.match(lines, /Only the operator can change the allowance/);
  assert.strictEqual(ui.fmtOperatorIntervention(null), null);
  assert.match(ui.fmtServiceShortfall({ intervals_below_critical: 1, intervals_below_reserve: 2, ev_shortfall_model_kwh: 0 }),
    /1 interval below the critical water level, 2 intervals below the water reserve/);
});

test('withheld or deferred revisions say why the plan is unchanged and ask for nothing', () => {
  const d = ui.fmtRevisionHold({ status: 'DEFERRED', explanation: 'A plan was accepted less than 1 hour ago.',
    current_plan_predicted: { intervals_below_reserve: 0, ev_shortfall_model_kwh: 0.5 },
    recomputed_plan_predicted: { intervals_below_reserve: 0, ev_shortfall_model_kwh: 0.4 } });
  assert.match(d.title, /on hold.*stays as it is/);
  assert.match(d.lines.join(' '), /less than 1 hour/);
  assert.match(d.lines.join(' '), /nothing to accept/);
  assert.match(ui.fmtRevisionHold({ status: 'NOT_NEEDED', explanation: null }).lines[0], /did not say why/);
  assert.strictEqual(ui.fmtRevisionHold({ status: 'AWAITING_MANAGER_ACCEPTANCE' }), null);
  assert.strictEqual(ui.fmtRevisionHold({ status: 'NONE' }), null);
});

test('critical level, reserve and readiness target stay distinct; pump rules are demonstration assumptions', () => {
  const option = {
    water_shortfall: { intervals_below_reserve: 0, intervals_below_readiness_margin: 2, readiness_margin_l: 400,
      intervals_below_critical: 0, critical_level_l: 1000 },
    constraint_report: [{ class: 'readiness_margin', status: 'not_met' }],
    pump_duration_rules: { min_on_intervals: 4, min_off_intervals: 2, basis: 'demonstration assumption' },
    pump_duration_exceptions: [{ kind: 'MIN_ON_NOT_MET', interval_index: 0, reason: 'no full-length run fits before the water need' }],
  };
  const levels = ui.waterLevels(option, 2000);
  assert.deepStrictEqual(levels.map((l) => [l.label, l.value]), [['Critical level', '1,000 L'], ['Reserve', '2,000 L'], ['Readiness target', '2,400 L']]);
  assert.match(levels[2].note, /not a requirement.*Not met for 2 intervals/);
  const none = ui.waterLevels({ water_shortfall: { readiness_margin_l: 0, critical_level_l: 0 } }, 2000);
  assert.deepStrictEqual(none.map((l) => l.value), ['Not set', '2,000 L', 'None']);
  const rules = ui.pumpRules(option, { clock_start_utc: '2026-08-18T18:30:00Z', model_dt_minutes: 15 }).join(' ');
  assert.match(rules, /at least 60 min.*at least 30 min.*demonstration assumptions/);
  assert.match(rules, /shorter run is planned at 00:00 IST/);
});

test('meter channels: grouped chargers labelled, missing stays unknown, totals never shown when incomplete', () => {
  const ch = {
    meter_id: 'meter-1', channel_id: 'ev_group', label: 'EV charger group', devices: ['charger-1', 'charger-2'], grouped: true,
    interval_start_utc: '2026-08-18T18:30:00Z', interval_end_utc: '2026-08-18T18:45:00Z',
    average_power_model_kw: 6.6, interval_energy_model_kwh: 1.65, cumulative_energy_model_kwh: 3.3, meter_session: 2,
    sequence_number: 4, quality: 'VALID', counter_restarts: 1,
  };
  const f = ui.fmtMeterChannel(ch);
  assert.match(f.group, /Group of 2: charger-1, charger-2\. One total for the group, not per charger or vehicle\./);
  assert.deepStrictEqual(f.rows.slice(0, 2), [['Power', '6.6 kW (model)'], ['Energy this interval', '1.65 kWh (model)']]);
  assert.match(f.rows[2][1], /3\.3 kWh \(model\) \(since meter start 2\)/);
  assert.match(f.span, /00:00 to 00:15 IST/);
  assert.match(f.restarted, /not counted twice/);
  const missing = ui.fmtMeterChannel({ ...ch, quality: 'MISSING', average_power_model_kw: null, interval_energy_model_kwh: null, cumulative_energy_model_kwh: null, counter_restarts: 0 });
  assert.strictEqual(missing.quality.label, 'Missing');
  assert.deepStrictEqual(missing.rows.slice(0, 3).map((r) => r[1]), ['Unknown', 'Unknown', 'Unknown']);
  const inc = ui.fmtMeterTotal({ complete: false, missing_channels: ['ev_group'], average_power_model_kw: null }, [ch]);
  assert.match(inc.text, /Incomplete: no reading from EV charger group\. The total is unknown, not zero\./);
  assert.match(ui.fmtMeterTotal({ complete: true, average_power_model_kw: 8.8, interval_energy_model_kwh: 2.2 }).text, /8\.8 kW \(model\), 2\.2 kWh \(model\) this interval\. Sum of separate channels/);
  assert.match(ui.fmtReason('EV_CHANNEL_MISSING'), /charging power is unknown/);
});

test('recovered counter energy is shown once, with its span, not given to an interval or a vehicle', () => {
  const base = { channel_id: 'ev_group', label: 'EV charger group', devices: ['charger-1', 'charger-2'], grouped: true, quality: 'VALID',
    average_power_model_kw: 3.3, interval_energy_model_kwh: 0.825, cumulative_energy_model_kwh: 3.3, meter_session: 1, sequence_number: 4 };
  const rec = { recovered_unassigned_model_kwh: 1.65, span_start_utc: '2026-08-18T18:45:00Z', span_end_utc: '2026-08-18T19:15:00Z',
    missing_intervals: 2, restart_in_gap: false, lower_bound: false };
  const f = ui.fmtMeterChannel({ ...base, recovered_after_missing: rec });
  assert.match(f.recovered, /After 2 missing readings, the counter showed 1\.65 kWh \(model\) used between 00:15 and 00:45 IST/);
  assert.match(f.recovered, /not given to any interval or vehicle/);
  const r2 = ui.fmtMeterChannel({ ...base, recovered_after_missing: { ...rec, missing_intervals: 1, restart_in_gap: true, lower_bound: true } });
  assert.match(r2.recovered, /1\.65 kWh \(model\) or more/);
  assert.match(r2.recovered, /energy before the restart is unknown/);
  assert.strictEqual(ui.fmtMeterChannel(base).recovered, null);
});

test('meter provenance: simulated, unscaled measured and scaled bench values stay distinct; raw never invented', () => {
  const ch = { channel_id: 'pump', label: 'Pump', devices: ['pump-1'], quality: 'VALID', average_power_model_kw: 4.4,
    interval_energy_model_kwh: 1.1, cumulative_energy_model_kwh: 2.2, meter_session: 1, sequence_number: 16,
    raw_counter_increase_kwh: 0.55, raw_counter_kwh: 1.1 };
  const labels = (f) => f.rows.map((r) => r[0]);
  // simulator run (no device) and emulator at scale 1: concise, no raw rows, no scale line
  assert.ok(!labels(ui.fmtMeterChannel(ch, null, { source_kind: 'synthetic', power_scale: null })).some((l) => /Raw/.test(l)));
  assert.deepStrictEqual(ui.fmtMeterScale({ source_kind: 'synthetic', power_scale: 1, volume_scale: 1 }), []);
  assert.strictEqual(ui.METER_CHIP.synthetic, 'Simulated');
  // physical meter at scale 1: one scale line, no duplicated raw rows
  const hw1 = { source_kind: 'hardware', power_scale: 1, volume_scale: 1 };
  assert.ok(!labels(ui.fmtMeterChannel(ch, null, hw1)).some((l) => /Raw/.test(l)));
  assert.deepStrictEqual(ui.fmtMeterScale(hw1), ["Electrical measurements: unscaled (×1). Model values are the meter's own readings.", 'Water volume: unscaled (×1).']);
  assert.strictEqual(ui.fmtSource('hardware'), 'Physical measurement, unscaled');
  // scaled bench: factor, raw and model values all shown and labelled apart; raw is the stored value, model unchanged
  const sc = { source_kind: 'scaled_hardware', power_scale: 2, volume_scale: 1 };
  const f = ui.fmtMeterChannel(ch, null, sc);
  assert.deepStrictEqual(f.rows.slice(0, 2), [['Power', '4.4 kW (model)'], ['Energy this interval', '1.1 kWh (model)']]);
  assert.deepStrictEqual(f.rows.filter((r) => /Raw/.test(r[0])),
    [['Raw energy this interval', '0.55 kWh (raw, before scale)'], ['Raw counter', '1.1 kWh (raw, before scale)']]);
  assert.match(ui.fmtMeterScale(sc)[0], /Electrical measurements: model-scaled \(×2\)\. Model kWh = raw counter kWh × 2\. Time is not scaled\./);
  assert.strictEqual(ui.fmtMeterScale(sc)[1], 'Water volume: unscaled (×1).');
  assert.strictEqual(ui.fmtSource('scaled_hardware'), 'Scaled from bench measurements; model values, not field measurements.');
  assert.strictEqual(ui.fmtSource('scaled_hardware', { power_scale: 1, volume_scale: 10 }),
    'Bench measurements: electrical measurements unscaled (×1); water volume model-scaled (×10). Scaled values are model values, not field measurements.');
  assert.match(ui.fmtSource('scaled_hardware', { power_scale: 2, volume_scale: 1 }), /electrical measurements model-scaled \(×2\); water volume unscaled \(×1\)/);
  assert.strictEqual(ui.fmtSource('hardware', { power_scale: 1, volume_scale: 1 }), 'Physical measurement, unscaled');
  // missing raw (missing channel, or a run stored without raw fields): unavailable, never zero or model / scale
  const noRaw = { ...ch, raw_counter_increase_kwh: undefined, raw_counter_kwh: undefined };
  for (const m of [sc, hw1]) {
    const rows = ui.fmtMeterChannel(noRaw, null, m).rows.filter((r) => /Raw/.test(r[0]));
    assert.deepStrictEqual(rows, [['Raw meter reading', 'Raw measurement unavailable']]);
  }
  assert.deepStrictEqual(ui.fmtMeterScale({ source_kind: 'scaled_hardware' }), ['Scale factor not recorded with these readings.']);
  // volume-only scaling: electrical stays unscaled, no raw duplication, and the volume factor is stated on its own
  const vol = { source_kind: 'scaled_hardware', electrical_source_kind: 'hardware', power_scale: 1, volume_scale: 10 };
  assert.deepStrictEqual(ui.fmtMeterScale(vol), ["Electrical measurements: unscaled (×1). Model values are the meter's own readings.", 'Water volume: model-scaled (×10).']);
  assert.ok(!ui.fmtMeterChannel(ch, null, vol).rows.some((r) => /Raw/.test(r[0])));
  assert.strictEqual(ui.METER_CHIP[vol.electrical_source_kind], 'Measured');
  // emulator at scale != 1: raw is labelled simulated, not a measurement
  const emu = ui.fmtMeterChannel(ch, null, { source_kind: 'synthetic', power_scale: 2 }).rows.filter((r) => /Raw/.test(r[0]));
  assert.match(emu[0][1], /raw emulator counter, simulated, before scale/);
});

test('bill analysis: missing money is Unknown, statuses readable, savings signed honestly', () => {
  assert.strictEqual(ui.fmtRs(null), 'Unknown');
  assert.strictEqual(ui.fmtRs(undefined), 'Unknown');
  assert.strictEqual(ui.fmtRs(0), 'Rs.0.00');
  assert.strictEqual(ui.fmtRs(1674.4), 'Rs.1,674.40');
  assert.match(ui.fmtBillStatus('partial').label, /no total is shown/);
  assert.match(ui.fmtBillStatus('weird').label, /Unrecognised status/);
  assert.match(ui.fmtSaving(0, true), /^Zero/);
  assert.match(ui.fmtSaving(-12.5, true), /Lower by Rs\.12\.50 \(a saving\)/);
  assert.match(ui.fmtSaving(12.5, true), /Higher by Rs\.12\.50 \(a negative saving\)/);
  assert.match(ui.fmtSaving(-12.5, false), /^Not estimable/);
  assert.strictEqual(ui.fmtSaving(null, true), 'Unknown.');
});

test('3D scene shows only what was measured; missing or unattributable values are unknown, never off', () => {
  const branch = { quality: 'VALID', interval_index: 1, tank_l: 4376, pump_model_kw: 0, ev_model_kw: 0,
    meter: { channels: [{ channel_id: 'pump', quality: 'VALID', average_power_model_kw: 0 },
      { channel_id: 'ev_group', quality: 'VALID', average_power_model_kw: 0 }] } };
  // false done: the plan asked for charging and the manager said done, but the meter measured 0 kW
  assert.deepStrictEqual([ui.sceneState(branch, 10000).pump, ui.sceneState(branch, 10000).charger], ['off', 'off']);
  const charging = { ...branch, meter: { channels: [branch.meter.channels[0], { channel_id: 'ev_group', quality: 'VALID', average_power_model_kw: 3.31 }] } };
  assert.strictEqual(ui.sceneState(charging, 10000).charger, 'on');
  // a lost channel is unknown even though the legacy field might say 0
  const lost = { ...branch, meter: { channels: [branch.meter.channels[0], { channel_id: 'ev_group', quality: 'MISSING', average_power_model_kw: null }] } };
  assert.strictEqual(ui.sceneState(lost, 10000).charger, 'unknown');
  // a missing reading (STALE) makes everything unknown, including the tank
  const stale = ui.sceneState({ quality: 'STALE', interval_index: 2, tank_l: null, pump_model_kw: null, ev_model_kw: null }, 10000);
  assert.deepStrictEqual([stale.pump, stale.charger, stale.tankL, stale.tankPct], ['unknown', 'unknown', null, null]);
  // aggregate-only metering cannot attribute power to the pump or chargers
  const agg = ui.sceneState({ quality: 'VALID', interval_index: 0, tank_l: 5000, pump_model_kw: null, ev_model_kw: null, aggregate_model_kw: 5.5 }, 10000);
  assert.deepStrictEqual([agg.pump, agg.charger, agg.tankPct], ['unknown', 'unknown', 50]);
  // no reading yet
  assert.deepStrictEqual([ui.sceneState(null, 10000).pump, ui.sceneState(null, 10000).interval], ['unknown', null]);
});

test('reading source is stated beside meter values: emulator readings are labelled synthetic emulator readings', () => {
  assert.strictEqual(ui.fmtReadingSource('telemetry', 'synthetic'), 'Synthetic emulator reading');
  assert.strictEqual(ui.fmtReadingSource('simulator', 'synthetic'), 'Synthetic simulator reading');
  assert.strictEqual(ui.fmtReadingSource('telemetry', 'scaled_hardware'), ui.fmtSource('scaled_hardware'));
});
