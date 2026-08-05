// Restored-session single-load harness for a new authenticated browser tab.
//
// Confirmed bug (measured with a real login by the user): opening a NEW tab in the same
// browser profile, with a Supabase session already persisted from a previous login, and
// opening Comunidade for the first time in that tab produced 2x feed + 2x profile/me
// instead of 1x each. F5 in the same context stayed at 1x/1x.
//
// Root cause (proven below against the real, unstubbed static/supabase_auth.js +
// static/social_community.js via a Node vm module graph - not a reimplementation):
// static/supabase_auth.js's onAuthChange() races two independent signals for the SAME
// restored session - its own explicit getSession()/getUser() restore, and the Supabase
// SDK's native onAuthStateChange replay. Either can resolve first. When the native replay
// loses that race and still arrives afterwards carrying no session (its own internal
// recovery had not caught up yet at subscribe time - a normal, harmless SDK detail), it
// looked identical to a sign-out to applySession() in social_community.js, which discarded
// the event name entirely and decided everything from Boolean(session) alone. Treating that
// stray null as a sign-out wiped the mount's already-populated profile/feed cache, so the
// SIGNED_IN that followed moments later re-fired both requests - the exact 2x this harness
// reproduces deterministically (see 'NEGATIVE CONTROL' below, and 3 repeated runs of the
// same scenario to rule out log contamination).
//
// The fix (static/social_community.js, applySession): only an explicit SIGNED_OUT event (or
// the very first event a mount ever sees) may regress an already-authenticated mount to
// logged-out. A real sign-out is always reported by the SDK as SIGNED_OUT specifically;
// nothing else legitimately does, so this is the event's own semantic name being used
// instead of discarded, not a heuristic, timer, or counter.
//
// This harness links the REAL static/auth_provider.js and static/supabase_auth.js modules
// (not the simplified auth_provider stub the sibling harnesses use) so the exact restore
// race that produced the bug is exercised end to end. The only synthetic piece is the
// Supabase SDK's `createClient` (network CDN import), stubbed to a fake `client.auth`
// whose event timing is scripted per scenario.
//
// No credentials of any kind: every session/token below is synthetic.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const failures = [];
let passed = 0;

async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (err) { failures.push({ name, message: String(err && err.message || err) }); }
}

function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], style: {}, dataset: {}, attributes: {}, className: '',
    _text: '', get textContent() { return this._text; }, set textContent(v) { this._text = String(v); this.children = []; },
    setAttribute(k, v) { this.attributes[k] = String(v); }, removeAttribute(k) { delete this.attributes[k]; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; }, append(...k) { this.children.push(...k); },
    replaceChildren(...k) { this.children = k.filter(Boolean); }, remove() {}, focus() {}, click() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; },
    classList: { add() {}, remove() {}, toggle() {} },
  };
  return node;
}

function makeEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, handler) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(handler); },
    removeEventListener(type, handler) { listeners.get(type)?.delete(handler); },
    dispatchEvent(event) { for (const handler of [...(listeners.get(event.type) || [])]) handler(event); return true; },
  };
}

function makeWindow(host) {
  const documentStub = {
    readyState: 'complete', activeElement: null, body: makeElement('body'), createElement: makeElement,
    createDocumentFragment: () => makeElement('fragment'),
    getElementById: (id) => (id === 'view-community' ? host : null),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  };
  const events = makeEventTarget();
  const windowStub = {
    document: documentStub,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { origin: 'http://127.0.0.1:8080', search: '', hostname: '127.0.0.1' },
    setTimeout, clearTimeout, setInterval, clearInterval,
    URL, AbortController, Promise, console,
    addEventListener: events.addEventListener, removeEventListener: events.removeEventListener,
    dispatchEvent: events.dispatchEvent,
    __tradutorAuthState: 'authenticated', // canonical arbiter already confirmed, like a real restored-session tab
  };
  windowStub.window = windowStub;
  windowStub.globalThis = windowStub;
  windowStub.CustomEvent = function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; };
  return windowStub;
}

function makeApiStub(counters) {
  class SocialApiError extends Error { constructor(status, code) { super(code || `social_error_${status}`); this.status = status; this.code = code || ''; } }
  const page = async () => ({ items: [], next_cursor: null });
  const handler = { get(target, prop) { if (prop in target) return target[prop]; return async () => ({ items: [], next_cursor: null }); } };
  const base = {
    SocialApiError, messageForError: () => 'erro',
    getFeed: async () => { counters.feed += 1; return page(); },
    getMyProfile: async () => { counters.profile += 1; return { id: 'synthetic-user', username: 'u', display_name: 'U' }; },
    updateMyProfile: async (fields) => ({ id: 'synthetic-user', ...fields }),
  };
  return new Proxy(base, handler);
}

// Fake `client.auth`, scripted per scenario: `nativeEvents` are delivered through the real
// SDK subscription callback (spaced with a real timer, like genuine async SDK delivery -
// microtask-only spacing let the explicit-restore race collapse differently and hid the bug),
// `sessionForGetSession` is what the module's own getSession()/getUser() restore finds.
function makeFakeSupabaseAuth({ nativeEvents, sessionForGetSession, userIdMatches = true }) {
  let cb = null;
  return {
    onAuthStateChange(fn) {
      cb = fn;
      (async () => {
        for (const [session, event] of nativeEvents) {
          await new Promise((resolve) => setTimeout(resolve, 5));
          cb(event, session);
        }
      })();
      return { data: { subscription: { unsubscribe() {} } } };
    },
    async getSession() { return { data: { session: sessionForGetSession }, error: null }; },
    async getUser() {
      if (!sessionForGetSession) return { data: { user: null }, error: new Error('no user') };
      return { data: { user: userIdMatches ? sessionForGetSession.user : { id: 'other' } }, error: null };
    },
  };
}

async function loadRealModules({ communitySource, nativeEvents, sessionForGetSession }) {
  const host = makeElement('div');
  const win = makeWindow(host);
  const origFetch = async (url) => {
    if (String(url).includes('/api/community/auth/config')) {
      return { ok: true, json: async () => ({ provider: 'supabase', supabase_url: 'https://proj.supabase.co', publishable_key: 'pk' }) };
    }
    return { ok: true, json: async () => ({ community: { social: { provider: 'supabase', available: true, reason_code: '' } } }) };
  };
  win.fetch = origFetch;
  const context = vm.createContext(win);
  const counters = { feed: 0, profile: 0 };
  const fakeClient = { auth: makeFakeSupabaseAuth({ nativeEvents, sessionForGetSession }) };

  const stubs = new Map([
    ['/static/social_api.js', makeApiStub(counters)],
    ['https://esm.sh/@supabase/supabase-js@2.58.0', { createClient: () => fakeClient }],
  ]);
  const synthetic = (specifier) => {
    const exportsObject = stubs.get(specifier);
    return new vm.SyntheticModule(Object.keys(exportsObject), function () {
      for (const k of Object.keys(exportsObject)) this.setExport(k, exportsObject[k]);
    }, { context, identifier: specifier });
  };

  async function linker(specifier) {
    if (specifier === '/static/auth_provider.js') return authProviderModule;
    if (specifier === '/static/supabase_auth.js') return supabaseAuthModule;
    if (stubs.has(specifier)) return synthetic(specifier);
    throw new Error('unresolved: ' + specifier);
  }
  async function importModuleDynamically(specifier) {
    const mod = await linker(specifier);
    if (mod.status === 'unlinked') await mod.link(linker);
    if (mod.status === 'linked') await mod.evaluate();
    return mod;
  }

  const authProviderModule = new vm.SourceTextModule(
    fs.readFileSync(path.join(ROOT, 'static', 'auth_provider.js'), 'utf8'),
    { context, identifier: '/static/auth_provider.js', importModuleDynamically });
  const supabaseAuthModule = new vm.SourceTextModule(
    fs.readFileSync(path.join(ROOT, 'static', 'supabase_auth.js'), 'utf8'),
    { context, identifier: '/static/supabase_auth.js', importModuleDynamically });
  const communityModule = new vm.SourceTextModule(
    communitySource, { context, identifier: 'social_community.js', importModuleDynamically });

  await authProviderModule.link(linker);
  await supabaseAuthModule.link(linker);
  await communityModule.link(linker);
  await authProviderModule.evaluate();
  await supabaseAuthModule.evaluate();
  await communityModule.evaluate();

  // Native events are spaced 5ms apart (see makeFakeSupabaseAuth); wait long enough for the
  // whole scripted sequence plus the module's own async restore chain to fully settle.
  await new Promise((resolve) => setTimeout(resolve, 250));
  for (let i = 0; i < 40; i++) await Promise.resolve();

  return { counters, win, host };
}

const CURRENT = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');
const SESSION = { access_token: 'tok-restored', user: { id: 'synthetic-restored-user' }, expires_at: 9999999999 };

// ---------------------------------------------------------------------------
// The real new-tab sequence: the SDK's own native replay loses the race against
// supabase_auth.js's explicit restore and arrives afterwards with no session yet.
// ---------------------------------------------------------------------------
const NEW_TAB_EVENTS = [[null, 'INITIAL_SESSION'], [SESSION, 'SIGNED_IN']];

await test('new tab with a restored session loads the feed exactly once', async () => {
  const { counters } = await loadRealModules({ communitySource: CURRENT, nativeEvents: NEW_TAB_EVENTS, sessionForGetSession: SESSION });
  assert.equal(counters.feed, 1, `feed requests: ${counters.feed}`);
});

await test('new tab with a restored session loads profile/me exactly once', async () => {
  const { counters } = await loadRealModules({ communitySource: CURRENT, nativeEvents: NEW_TAB_EVENTS, sessionForGetSession: SESSION });
  assert.equal(counters.profile, 1, `profile/me requests: ${counters.profile}`);
});

// Repeated 3x (distinct simulated tabs/module instances) to rule out log contamination.
for (let i = 1; i <= 3; i += 1) {
  await test(`new tab restored session is deterministic across independent runs (run ${i}/3)`, async () => {
    const { counters } = await loadRealModules({ communitySource: CURRENT, nativeEvents: NEW_TAB_EVENTS, sessionForGetSession: SESSION });
    assert.equal(counters.feed, 1, `feed requests: ${counters.feed}`);
    assert.equal(counters.profile, 1, `profile/me requests: ${counters.profile}`);
  });
}

await test('F5 (native fires a single already-resolved INITIAL_SESSION) still loads exactly once', async () => {
  const { counters } = await loadRealModules({
    communitySource: CURRENT,
    nativeEvents: [[SESSION, 'INITIAL_SESSION']],
    sessionForGetSession: SESSION,
  });
  assert.equal(counters.feed, 1, `feed requests: ${counters.feed}`);
  assert.equal(counters.profile, 1, `profile/me requests: ${counters.profile}`);
});

await test('a genuinely anonymous tab (no session anywhere, first event is not SIGNED_OUT) issues no request', async () => {
  const { counters } = await loadRealModules({
    communitySource: CURRENT,
    nativeEvents: [[null, 'INITIAL_SESSION']],
    sessionForGetSession: null,
  });
  assert.equal(counters.feed, 0);
  assert.equal(counters.profile, 0);
});

await test('a SIGNED_OUT followed by a SIGNED_IN for the SAME account reuses the cache (not doubled)', async () => {
  const { counters } = await loadRealModules({
    communitySource: CURRENT,
    nativeEvents: [[SESSION, 'INITIAL_SESSION'], [null, 'SIGNED_OUT'], [SESSION, 'SIGNED_IN']],
    sessionForGetSession: SESSION,
  });
  // Was intentionally 2 ("an explicit SIGNED_OUT is always a real generation boundary").
  // Production evidence (the same-tab-login smoke) disproved that assumption: the SDK's two
  // independent restore races can surface a real SIGNED_OUT event for an account that is
  // signing IN, not out, and the SIGNED_IN that follows moments later carries the exact same
  // account - doubling the feed + profile/me load for a single login. The account (session.
  // user.id) is what actually decides whether the cache is still valid, not the event name;
  // see accountId()/sameAccount in applySession() in static/social_community.js.
  assert.equal(counters.feed, 1, `feed requests: ${counters.feed}`);
  assert.equal(counters.profile, 1, `profile/me requests: ${counters.profile}`);
});

await test('a SIGNED_OUT-only tail (real sign-out, no further sign-in) issues no extra request', async () => {
  const { counters } = await loadRealModules({
    communitySource: CURRENT,
    nativeEvents: [[SESSION, 'INITIAL_SESSION'], [null, 'SIGNED_OUT']],
    sessionForGetSession: SESSION,
  });
  assert.equal(counters.feed, 1, `feed requests: ${counters.feed}`);
  assert.equal(counters.profile, 1, `profile/me requests: ${counters.profile}`);
});

// --- negative control: the pre-this-fix module (417c88f) must reproduce 2x on the exact
// same new-tab scenario, proving the duplication this harness targets is real. ---
const PREVIOUS_PATH = process.env.SOCIAL_COMMUNITY_PREVIOUS
  || path.join(ROOT, '.runtime', 'claude-community-restored-session-load', 'social_community_before.js');
const PREVIOUS = fs.existsSync(PREVIOUS_PATH) ? fs.readFileSync(PREVIOUS_PATH, 'utf8') : '';

if (PREVIOUS) {
  await test('NEGATIVE CONTROL: the pre-fix module (417c88f) duplicates feed+profile on the new-tab sequence', async () => {
    const { counters } = await loadRealModules({ communitySource: PREVIOUS, nativeEvents: NEW_TAB_EVENTS, sessionForGetSession: SESSION });
    assert.equal(counters.feed, 2, `pre-fix feed requests should be 2 (the bug): ${counters.feed}`);
    assert.equal(counters.profile, 2, `pre-fix profile/me requests should be 2 (the bug): ${counters.profile}`);
  });
} else {
  failures.push({ name: 'negative control setup', message: `missing baseline file at ${PREVIOUS_PATH}` });
}

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
