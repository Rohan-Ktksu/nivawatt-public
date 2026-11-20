// 3D demo view: one run, read-only. The scene comes from the latest observation only (NW_UI.sceneState); the three
// columns put the plan's requested action, the manager's report and the meter observation for the same interval side
// by side, and the revision panel shows the backend's own gate decision. Nothing here posts to the service.
(() => {
  const { html, fmtTime, fmtError } = NW_UI;
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 2000;
  const params = new URLSearchParams(location.search);
  let runId = params.get('run') || '';
  let apartmentId = params.get('apartment') || 'APT_1';
  let lastJson = null;
  let connectionLost = false;

  const put = (id, content) => { $(id).innerHTML = String(content); };
  const toast = (message) => { $('toast').innerHTML = message ? String(html`<div class="notice tone-bad"><p>${message}</p></div>`) : ''; };

  function showSignIn(message) {
    $('app').hidden = true;
    $('loading').hidden = true;
    $('signin').hidden = false;
    $('run-id').value = runId;
    $('apartment-id').value = apartmentId;
    toast(message);
  }

  function render(st) {
    put('meta', html`Run <code>${st.run_id}</code>, ${st.apartment.apartment_id}. Model time ${fmtTime(st.clock.model_time_utc)}, interval ${st.clock.interval_index} of ${st.clock.horizon_intervals}. Source: ${st.observation_source}.`);
    const opts = { connectionLost, lastReceived };
    NW_BUILDING.renderScene($('scene'), st, opts);
    const cols = NW_BUILDING.columns(st, opts);
    put('requested', cols.requested);
    put('reported', cols.reported);
    put('observed', cols.observed);
    put('revision', NW_BUILDING.revision(st));
    put('timeline', NW_BUILDING.timeline(st));
  }

  // One poll at a time. A session generation bumps on sign-in, sign-out and session loss; those also cancel pending
  // reads, and any response from an older generation (or for another run/apartment) is dropped, never rendered.
  let gen = 0;
  let polling = null;
  let lastState = null;
  let lastReceived = null;

  function resetSession() {
    gen += 1;
    NW_API.abortAll();
    polling = null;
    lastJson = null;
    lastState = null;
    connectionLost = false;
  }

  function refresh() {
    if (polling) return polling;
    const p = poll().finally(() => { if (polling === p) polling = null; });
    polling = p;
    return p;
  }

  async function poll() {
    const my = gen;
    const run = runId;
    const apt = apartmentId;
    let st;
    try {
      st = await NW_API.state(run, apt);
    } catch (e) {
      if (my !== gen || e.code === 'ABORTED' || e.status === 401) return;
      if (!lastState) { showSignIn(fmtError(e)); return; }
      connectionLost = true;  // keep the last state on screen, labelled, with the equipment animation stopped
      toast(`${fmtError(e)} Retrying.`);
      render(lastState);
      return;
    }
    if (my !== gen || st.run_id !== run || st.apartment?.apartment_id !== apt) return;
    const recovered = connectionLost;
    connectionLost = false;
    lastReceived = new Date();
    toast();
    $('signin').hidden = true;
    $('loading').hidden = true;
    $('app').hidden = false;
    const json = JSON.stringify(st);
    if (json !== lastJson || recovered) {
      lastJson = json;
      lastState = st;
      render(st);
    }
  }

  $('signin-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    resetSession();
    const my = gen;
    runId = $('run-id').value.trim();
    apartmentId = $('apartment-id').value.trim();
    try {
      await NW_API.signIn({ apartment_id: apartmentId, access_code: $('access-code').value });
      if (my !== gen) return;
      $('access-code').value = '';
      $('signin').hidden = true;
      $('loading').hidden = false;
      await refresh();
    } catch (e) {
      if (my === gen && e.code !== 'ABORTED') toast(fmtError(e));
    }
  });
  $('sign-out').addEventListener('click', () => { resetSession(); NW_API.signOut(); showSignIn(); });

  NW_API.init('manager', (e) => { resetSession(); showSignIn(fmtError(e)); });
  if (NW_API.hasSession() && runId) { $('loading').hidden = false; refresh(); } else showSignIn();
  setInterval(() => { if (lastState && !document.hidden) refresh(); }, POLL_MS);
})();
