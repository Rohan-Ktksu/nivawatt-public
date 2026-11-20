// Run one browser check against its own throwaway service: a free port, a fresh database, and cleanup that stops only
// the process tree this runner started, also when the check fails, throws or is interrupted.
//
//   set NW_PYTHON=.venv\Scripts\python   (and NODE_PATH, NW_OPERATOR_CODE, NW_MANAGER_CODE as the check needs)
//   node tests/ui/run_isolated.cjs tests/ui/e2e_flow.cjs
//
// The service uses config/local.json unless NIVAWATT_CODES is set. The check gets NW_BASE and NW_DB.
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

const REPO = path.join(__dirname, '..', '..');
const PY = process.env.NW_PYTHON;
const CHECK = process.argv[2];

const freePort = () => new Promise((resolve, reject) => {
  const s = net.createServer();
  s.once('error', reject);
  s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
});
const answering = (port) => new Promise((resolve) => {
  const c = net.connect(port, '127.0.0.1');
  c.once('connect', () => { c.destroy(); resolve(true); });
  c.once('error', () => resolve(false));
});
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

let service = null;
let port = null;
function stopService() {
  if (!service || service.exitCode !== null || service.signalCode) return;
  // Only the tree rooted at the PID this runner spawned (the venv launcher and its child Python).
  if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(service.pid), '/T', '/F'], { stdio: 'ignore' });
  else try { process.kill(-service.pid, 'SIGTERM'); } catch { /* already gone */ }
}
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => { stopService(); process.exit(130); });
process.on('exit', stopService);

(async () => {
  if (!PY || !CHECK) throw new Error('usage: NW_PYTHON=<python> node tests/ui/run_isolated.cjs <check.cjs>');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'nw-check-'));
  port = await freePort();
  const db = path.join(dir, 'service.db');
  service = spawn(PY, ['app.py', '--port', String(port)], {
    cwd: REPO, detached: process.platform !== 'win32', stdio: ['ignore', fs.openSync(path.join(dir, 'service.log'), 'w'), 'pipe'],
    env: { ...process.env, NIVAWATT_DB: db, NIVAWATT_RESULTS_DIR: path.join(dir, 'experiments') },
  });
  let stderr = '';
  service.stderr.on('data', (d) => { stderr += d; });
  for (let k = 0; k < 120 && !(await answering(port)); k++) {
    if (service.exitCode !== null) throw new Error(`service exited before listening:\n${stderr.slice(-2000)}`);
    await pause(250);
  }
  if (!(await answering(port))) throw new Error('service did not start within 30 s');
  console.log(`[isolated] service pid ${service.pid} on port ${port}, database ${db}`);
  const code = await new Promise((resolve) => {
    const c = spawn(process.execPath, [CHECK], { cwd: REPO, stdio: 'inherit',
      env: { ...process.env, NW_BASE: `http://127.0.0.1:${port}`, NW_DB: db } });
    c.on('exit', (n) => resolve(n === null ? 1 : n));
  });
  return code;
})().then(async (code) => {
  stopService();
  await pause(1000);
  const left = port !== null && await answering(port);
  console.log(`[isolated] check exit ${code}; service stopped: ${left ? 'NO (port still answering)' : 'yes'}`);
  process.exitCode = left ? 3 : code;
}, async (e) => {
  console.error(`[isolated] ${e.message}`);
  stopService();
  process.exitCode = 2;
});
