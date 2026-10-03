// Contract for the connection indicator behind "serviço conectado".
//
// Root cause this covers: the badge was written once, at bootstrap, from
// `settings.nvidia_configured` -- a configuration fact rendered as a
// connectivity claim. Nothing ever revoked it, so an already-loaded page kept
// showing a green "serviço conectado" while the local backend had no listener
// at all. CONNECTED must now mean "a probe succeeded recently", and nothing
// else may produce it.
//
// Run: node test_service_health.mjs

import assert from 'node:assert/strict';
import {
  createServiceHealth,
  CONNECTING,
  CONNECTED,
  DISCONNECTED,
  FAILURE_THRESHOLD,
  PROBE_INTERVAL_MS,
  PROBE_TIMEOUT_MS,
  STALE_AFTER_MS,
} from './static/service_health.js';

const failures = [];
let passed = 0;

async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (err) { failures.push({name, message: String((err && err.message) || err)}); }
}

// A clock the tests move by hand: no timers, no sleeping.
function makeClock(start = 1_000_000) {
  let value = start;
  return {now: () => value, advance(ms) { value += ms; }};
}

const ok = () => Promise.resolve({ok: true, status: 200});
const serverError = () => Promise.resolve({ok: false, status: 500});
const unreachable = () => Promise.reject(new TypeError('Failed to fetch'));

// --- boot -----------------------------------------------------------------

await test('starts CONNECTING, never CONNECTED, before the first probe', () => {
  const health = createServiceHealth({fetchImpl: ok});
  assert.equal(health.state(), CONNECTING);
  assert.notEqual(health.state(), CONNECTED);
});

// --- success --------------------------------------------------------------

await test('a successful probe reaches CONNECTED', async () => {
  const health = createServiceHealth({fetchImpl: ok});
  assert.equal(await health.probe(), CONNECTED);
});

await test('probes the local health endpoint without caching', async () => {
  const calls = [];
  const health = createServiceHealth({
    fetchImpl: (path, init) => { calls.push([path, init]); return ok(); },
  });
  await health.probe();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/health');
  assert.equal(calls[0][1].cache, 'no-store');
});

// --- success -> failure ---------------------------------------------------

await test('CONNECTED is revoked once the configured threshold is reached', async () => {
  const health = createServiceHealth({fetchImpl: ok});
  await health.probe();
  assert.equal(health.state(), CONNECTED);
  health.setFetch(unreachable);
  for (let i = 1; i < FAILURE_THRESHOLD; i += 1) {
    assert.equal(await health.probe(), CONNECTED, 'must not flap below the threshold');
  }
  assert.equal(await health.probe(), DISCONNECTED);
});

await test('a rejected fetch is a failure, not a swallowed success', async () => {
  const health = createServiceHealth({fetchImpl: ok});
  await health.probe();
  health.setFetch(unreachable);
  for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await health.probe();
  assert.equal(health.state(), DISCONNECTED);
});

await test('a non-ok response is a failure', async () => {
  const health = createServiceHealth({fetchImpl: ok});
  await health.probe();
  health.setFetch(serverError);
  for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await health.probe();
  assert.equal(health.state(), DISCONNECTED);
});

// --- staleness ------------------------------------------------------------
// A hidden tab's timers are throttled or suspended: probes stop happening
// rather than start failing, so "no news" must not read as good news.

await test('CONNECTED goes stale when no probe reports back in time', async () => {
  const clock = makeClock();
  const health = createServiceHealth({fetchImpl: ok, now: clock.now});
  await health.probe();
  assert.equal(health.state(), CONNECTED);
  clock.advance(STALE_AFTER_MS - 1);
  assert.equal(health.state(), CONNECTED);
  clock.advance(2);
  assert.equal(health.state(), DISCONNECTED);
});

// --- failure -> success ---------------------------------------------------

await test('recovers to CONNECTED when the backend returns', async () => {
  const health = createServiceHealth({fetchImpl: unreachable});
  for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await health.probe();
  assert.equal(health.state(), DISCONNECTED);
  health.setFetch(ok);
  assert.equal(await health.probe(), CONNECTED);
});

// --- overlapping probes ---------------------------------------------------

await test('a slow success cannot restore CONNECTED after a newer failure', async () => {
  let release;
  const slowOk = () => new Promise((resolve) => { release = () => resolve({ok: true, status: 200}); });
  const health = createServiceHealth({fetchImpl: ok});
  await health.probe();          // reach CONNECTED first
  health.setFetch(slowOk);
  const stale = health.probe();  // starts, does not resolve yet
  health.setFetch(unreachable);
  for (let i = 0; i < FAILURE_THRESHOLD; i += 1) await health.probe();
  assert.equal(health.state(), DISCONNECTED);
  release();                     // the old success lands last
  await stale;
  assert.equal(health.state(), DISCONNECTED);
});

// --- timing policy --------------------------------------------------------

await test('polls at a bounded, non-spamming interval with a bounded timeout', () => {
  assert.ok(PROBE_INTERVAL_MS >= 1000, 'no sub-second request storm');
  assert.ok(PROBE_INTERVAL_MS <= 15000);
  assert.ok(PROBE_TIMEOUT_MS > 0 && PROBE_TIMEOUT_MS <= PROBE_INTERVAL_MS);
  assert.equal(STALE_AFTER_MS, PROBE_INTERVAL_MS * FAILURE_THRESHOLD + PROBE_TIMEOUT_MS);
});

if (failures.length) {
  for (const failure of failures) console.error(`FAIL ${failure.name}: ${failure.message}`);
  console.error(`${passed} passed, ${failures.length} failed`);
  process.exit(1);
}
console.log(`${passed} passed`);
