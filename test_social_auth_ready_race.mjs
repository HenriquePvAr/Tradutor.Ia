// Auth-ready race harness for the authenticated community mount.
//
// Reproduces (and fails against 842532c to prove) a real race: static/social_community.js
// used to decide whether to fire profile/me + feed purely from Boolean(session) on the raw
// Supabase SDK event. auth_ui.js runs an INDEPENDENT subscription to the same SDK and only
// flips the canonical, backend-confirmed window.__tradutorAuthState to 'authenticated' once
// /api/community/auth/session (or /api/ui/bootstrap) agrees - it explicitly regresses to
// 'auth_loading' on every SDK event first. Nothing stopped social_community.js from firing
// its private request while the canonical state was still anonymous/authenticating, which is
// exactly what an out-of-band 401 on an otherwise normal first login looks like.
//
// This harness models both signals explicitly: a synthetic Supabase auth event stream (the
// same shape test_social_runtime_lifecycle.mjs / test_social_first_login_idempotency.mjs
// use) AND a synthetic window.__tradutorAuthState + 'tradutor-auth-changed' event (the real
// canonical signal auth_ui.js drives). Real functions from the shipped module are exercised
// through a Node vm module linker - nothing here is a reimplementation of the fix.
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

// A real (tiny) event target so window.dispatchEvent('tradutor-auth-changed', ...) actually
// reaches the module's own window.addEventListener subscription - the same mechanism
// tradutor_ui.js and social_community.js use for real in the browser.
function makeEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(handler);
    },
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
    __tradutorAuthState: 'auth_loading', // canonical arbiter present from the first tick, like the real shell
  };
  windowStub.window = windowStub;
  windowStub.globalThis = windowStub;
  // Minimal CustomEvent so the test can dispatch the same shape auth_ui.js dispatches.
  windowStub.CustomEvent = function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; };
  return windowStub;
}

async function loadCommunityModule({ source, apiStub, authStub, bootstrapProvider }) {
  const host = makeElement('div');
  const win = makeWindow(host);
  win.fetch = async () => ({ ok: true, json: async () => ({ community: { social: bootstrapProvider } }) });
  const context = vm.createContext(win);
  const stubs = new Map([
    ['/static/social_api.js', apiStub],
    ['/static/auth_provider.js', authStub],
  ]);
  const synthetic = (specifier) => {
    const exportsObject = stubs.get(specifier);
    return new vm.SyntheticModule(
      Object.keys(exportsObject),
      function () { for (const k of Object.keys(exportsObject)) this.setExport(k, exportsObject[k]); },
      { context, identifier: specifier },
    );
  };
  const module = new vm.SourceTextModule(source, { context, identifier: 'social_community.js' });
  await module.link((specifier) => synthetic(specifier));
  await module.evaluate();
  return { module, host, win, namespace: module.namespace };
}

function makeApiStub(counters, { profileFails = false } = {}) {
  class SocialApiError extends Error {
    constructor(status, code) { super(code || `social_error_${status}`); this.status = status; this.code = code || ''; }
  }
  const page = async () => ({ items: [], next_cursor: null });
  const handler = { get(target, prop) { if (prop in target) return target[prop]; return async () => ({ items: [], next_cursor: null }); } };
  const base = {
    SocialApiError, messageForError: () => 'erro',
    getFeed: async () => { counters.feed += 1; return page(); },
    getMyProfile: async () => {
      counters.profile += 1;
      if (profileFails) throw new SocialApiError(422, 'validation_error');
      return { id: 'synthetic-user', username: null, display_name: null, profile_configured: false };
    },
    updateMyProfile: async (fields) => ({ id: 'synthetic-user', ...fields, profile_configured: true }),
    getMyWorks: page, getFavorites: page, getHistory: page, getNotifications: page,
    getWork: async () => ({ id: 'w', title: 'w' }),
    getWorkChapters: page, getComments: page, getAsset: async () => ({ available: false }),
    assetRetention: async () => ({ restorable: false }), retainedAssets: page,
    listLocalResults: page,
  };
  return new Proxy(base, handler);
}

function makeAuthStub(counters) {
  const listeners = [];
  return {
    listeners,
    async getSupabaseClient() { counters.clients += 1; return { provider: 'supabase' }; },
    async onAuthChange(handler) {
      counters.subscriptions += 1;
      listeners.push(handler);
      return () => { counters.unsubscribes += 1; const i = listeners.indexOf(handler); if (i >= 0) listeners.splice(i, 1); };
    },
    async signOut() { counters.signOuts += 1; },
    async getCanonicalAccessToken() { return 'synthetic-token'; },
  };
}

const SUPABASE_AVAILABLE = { provider: 'supabase', available: true, reason_code: '' };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

async function bootWith(source, options = {}) {
  const counters = { feed: 0, profile: 0, clients: 0, subscriptions: 0, unsubscribes: 0, signOuts: 0 };
  const authStub = makeAuthStub(counters, options);
  const loaded = await loadCommunityModule({
    source, apiStub: makeApiStub(counters, options), authStub,
    bootstrapProvider: options.bootstrapProvider || SUPABASE_AVAILABLE,
  });
  await flush();
  return { ...loaded, counters, authStub };
}

async function emit(authStub, events) {
  for (const [session, event] of events) {
    for (const handler of [...authStub.listeners]) handler(session, event);
    await flush();
  }
  await flush();
}

// The real canonical signal auth_ui.js drives: it sets window.__tradutorAuthState directly
// AND dispatches 'tradutor-auth-changed' - the exact two mechanisms
// waitForCanonicalAuthReady() in the fix consults.
function announceCanonical(win, state, detail = {}) {
  win.__tradutorAuthState = state;
  win.dispatchEvent(new win.CustomEvent('tradutor-auth-changed', {
    detail: { state, authenticated: state === 'authenticated', ...detail },
  }));
}

const CURRENT = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');
const SESSION = { access_token: 'tok-first-login', user: { id: 'synthetic-user' }, expires_at: 9999999999 };

// ---------------------------------------------------------------------------
// The real first-login sequence: canonical starts anonymous/loading, the SDK session
// arrives, the canonical confirmation lags behind, THEN it catches up.
// ---------------------------------------------------------------------------

await test('an SDK session with the canonical state still auth_loading issues no private request', async () => {
  const ctx = await bootWith(CURRENT);
  ctx.win.__tradutorAuthState = 'auth_loading'; // canonical has not confirmed yet
  await emit(ctx.authStub, [[SESSION, 'SIGNED_IN']]);
  assert.equal(ctx.counters.profile, 0, `profile/me fired before auth-ready: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 0, `feed fired before auth-ready: ${ctx.counters.feed}`);
});

await test('the pending request executes exactly once as soon as the canonical state catches up', async () => {
  const ctx = await bootWith(CURRENT);
  ctx.win.__tradutorAuthState = 'auth_loading';
  await emit(ctx.authStub, [[SESSION, 'SIGNED_IN']]);
  assert.equal(ctx.counters.profile, 0);
  announceCanonical(ctx.win, 'authenticated', { user_id: 'synthetic-user' });
  await flush();
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 1, `feed requests: ${ctx.counters.feed}`);
});

await test('a stray auth event without authenticated:true does not release the pending load', async () => {
  const ctx = await bootWith(CURRENT);
  ctx.win.__tradutorAuthState = 'auth_loading';
  await emit(ctx.authStub, [[SESSION, 'SIGNED_IN']]);
  ctx.win.dispatchEvent(new ctx.win.CustomEvent('tradutor-auth-changed', { detail: { state: 'auth_error', authenticated: false } }));
  await flush();
  assert.equal(ctx.counters.profile, 0, 'an unauthenticated canonical event must not release the pending load');
});

await test('F5 with an already-authenticated canonical state loads exactly once, no wait', async () => {
  const ctx = await bootWith(CURRENT);
  ctx.win.__tradutorAuthState = 'authenticated'; // bootstrap already confirmed before the SDK event arrives
  await emit(ctx.authStub, [[SESSION, 'INITIAL_SESSION']]);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 1, `feed requests: ${ctx.counters.feed}`);
});

await test('logout while a load is pending cancels it: no request ever fires for that generation', async () => {
  const ctx = await bootWith(CURRENT);
  ctx.win.__tradutorAuthState = 'auth_loading';
  await emit(ctx.authStub, [[SESSION, 'SIGNED_IN']]);
  assert.equal(ctx.counters.profile, 0);
  await emit(ctx.authStub, [[null, 'SIGNED_OUT']]);
  announceCanonical(ctx.win, 'authenticated', { user_id: 'synthetic-user' }); // late/stale wakeup for the old generation
  await flush();
  assert.equal(ctx.counters.profile, 0, 'a canonical confirmation for a signed-out generation must not fire a request');
  assert.equal(ctx.counters.feed, 0);
});

await test('a new sign-in after logout still loads exactly once for the new generation', async () => {
  const ctx = await bootWith(CURRENT);
  ctx.win.__tradutorAuthState = 'authenticated';
  await emit(ctx.authStub, [[SESSION, 'SIGNED_IN']]);
  assert.equal(ctx.counters.profile, 1);
  await emit(ctx.authStub, [[null, 'SIGNED_OUT']]);
  ctx.win.__tradutorAuthState = 'auth_loading';
  await emit(ctx.authStub, [[{ access_token: 'tok-2', user: { id: 'other-user' }, expires_at: 9999999999 }, 'SIGNED_IN']]);
  assert.equal(ctx.counters.profile, 1, 'still pending: canonical has not confirmed the new generation yet');
  announceCanonical(ctx.win, 'authenticated', { user_id: 'other-user' });
  await flush();
  assert.equal(ctx.counters.profile, 2, `profile/me requests across both generations: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 2, `feed requests across both generations: ${ctx.counters.feed}`);
});

await test('profile/me failing (422, migration pending) after auth-ready is not retried automatically', async () => {
  const ctx = await bootWith(CURRENT, { profileFails: true });
  ctx.win.__tradutorAuthState = 'authenticated';
  await emit(ctx.authStub, [[SESSION, 'INITIAL_SESSION']]);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
});

// --- negative control: the pre-this-fix module (842532c) must fire the private request
// before the canonical state confirms, proving the race this harness targets is real. ---
const PREVIOUS = process.env.SOCIAL_COMMUNITY_PREVIOUS
  ? fs.readFileSync(process.env.SOCIAL_COMMUNITY_PREVIOUS, 'utf8')
  : '';

if (PREVIOUS) {
  await test('NEGATIVE CONTROL: the pre-fix module fires the private request while still auth_loading', async () => {
    const ctx = await bootWith(PREVIOUS);
    ctx.win.__tradutorAuthState = 'auth_loading';
    await emit(ctx.authStub, [[SESSION, 'SIGNED_IN']]);
    assert.ok(
      ctx.counters.profile > 0 || ctx.counters.feed > 0,
      'pre-fix module should have fired profile/feed before the canonical state confirmed',
    );
  });
}

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
