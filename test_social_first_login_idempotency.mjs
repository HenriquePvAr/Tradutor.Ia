// Covers the first-login-specific duplicate load that test_social_runtime_lifecycle.mjs
// does not: that harness's SIGN_IN_EVENT_BURST replays the exact same session object for
// every event, which the existing sessionKey() dedup already collapsed correctly (5 -> 1).
//
// A real first sign-in reaches static/social_community.js's single onAuthChange handler
// through TWO independent restore races (this module's own subscription, set up at boot
// while still anonymous, and auth_ui.js's separate subscription on the same shared SDK
// client) that can each hand the handler a different SHAPE of session for the very same
// sign-in - e.g. one snapshot minted before the session has a `.user` attached, another
// once it does. sessionKey() falls back user.id -> access_token -> provider, so those two
// shapes compute two different strings and each looked like a brand new identity, which is
// why the previous fix (keyed purely on that string) reduced the burst from ~5 to 2 instead
// of 1: it collapsed same-shaped replays, not different-shaped ones for the same login.
//
// The fix gates the load on the authenticated/unauthenticated EDGE (Boolean(session)),
// which is single-valued across the whole burst regardless of session shape, and reserves
// the identity string only for staleness guards. This harness proves that with the exact
// same "two shapes, one login" sequence the F5 case (single, already-complete session
// shape, so no varying-shape burst) is unaffected.
//
// Same harness shape as test_social_runtime_lifecycle.mjs: the REAL static/social_community.js
// through a Node vm module linker, a minimal fake DOM, a fake fetch, and counting stubs.
// No credentials of any kind - every session below is synthetic.
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

function makeWindow(host) {
  const documentStub = {
    readyState: 'complete', activeElement: null, body: makeElement('body'), createElement: makeElement,
    createDocumentFragment: () => makeElement('fragment'),
    getElementById: (id) => (id === 'view-community' ? host : null),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  };
  const windowStub = {
    document: documentStub,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { origin: 'http://127.0.0.1:8080', search: '', hostname: '127.0.0.1' },
    setTimeout, clearTimeout, setInterval, clearInterval,
    URL, AbortController, Promise, console,
  };
  windowStub.window = windowStub;
  windowStub.globalThis = windowStub;
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

const CURRENT = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');

// The two shapes a real first sign-in's session can arrive in, for the very same login:
// one snapshot before `.user` is attached (sessionKey falls back to the access_token), one
// once it is (sessionKey uses the user id). Same underlying sign-in, two different strings.
const SESSION_TOKEN_ONLY = { access_token: 'tok-first-login', expires_at: 9999999999 };
const SESSION_WITH_USER = { access_token: 'tok-first-login', user: { id: 'synthetic-user' }, expires_at: 9999999999 };

const FIRST_LOGIN_VARYING_SHAPE_BURST = [
  [null, 'INITIAL_SESSION'],              // boot, still anonymous
  [null, 'INITIAL_SESSION'],               // the bounded-restore race replays it (still anonymous)
  [SESSION_TOKEN_ONLY, 'SIGNED_IN'],       // this subscription's restore race lands the token-only shape
  [SESSION_WITH_USER, 'SIGNED_IN'],        // a moment later, the fully-attached shape for the same sign-in
  [SESSION_WITH_USER, 'TOKEN_REFRESHED'],  // the SDK's own trailing refresh, same identity
];

await test('a first login whose session arrives in two shapes still loads exactly once', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, FIRST_LOGIN_VARYING_SHAPE_BURST);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 1, `feed requests: ${ctx.counters.feed}`);
});

await test('a failing profile/me during a varying-shape first login is not retried automatically', async () => {
  const ctx = await bootWith(CURRENT, { profileFails: true });
  await emit(ctx.authStub, FIRST_LOGIN_VARYING_SHAPE_BURST);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
});

await test('reload with an already-established session (F5) still loads exactly once', async () => {
  const ctx = await bootWith(CURRENT);
  const EXISTING = { access_token: 'tok-existing', user: { id: 'existing-user' }, expires_at: 9999999999 };
  await emit(ctx.authStub, [
    [EXISTING, 'INITIAL_SESSION'],
    [EXISTING, 'INITIAL_SESSION'],
    [EXISTING, 'INITIAL_SESSION_RESTORED'],
  ]);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 1, `feed requests: ${ctx.counters.feed}`);
});

await test('logout then a later login each load exactly once (two edges, two loads)', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, FIRST_LOGIN_VARYING_SHAPE_BURST);
  await emit(ctx.authStub, [[null, 'SIGNED_OUT']]);
  await emit(ctx.authStub, [
    [{ access_token: 'tok-2', expires_at: 9999999999 }, 'SIGNED_IN'],
    [{ access_token: 'tok-2', user: { id: 'other-user' }, expires_at: 9999999999 }, 'SIGNED_IN'],
  ]);
  assert.equal(ctx.counters.profile, 2, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 2, `feed requests: ${ctx.counters.feed}`);
});

await test('switching tabs after the first login issues no extra feed/profile load', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, FIRST_LOGIN_VARYING_SHAPE_BURST);
  const before = { ...ctx.counters };
  // A repeated identical-shape event (e.g. a second tab-visibility-driven notification)
  // must not look like a new sign-in.
  await emit(ctx.authStub, [[SESSION_WITH_USER, 'TOKEN_REFRESHED']]);
  assert.equal(ctx.counters.feed, before.feed);
  assert.equal(ctx.counters.profile, before.profile);
});

// --- negative control: the pre-this-fix module must duplicate on the varying-shape burst --
const PREVIOUS = process.env.SOCIAL_COMMUNITY_PREVIOUS
  ? fs.readFileSync(process.env.SOCIAL_COMMUNITY_PREVIOUS, 'utf8')
  : '';

if (PREVIOUS) {
  await test('NEGATIVE CONTROL: the pre-fix module double-loads a varying-shape first login', async () => {
    const ctx = await bootWith(PREVIOUS);
    await emit(ctx.authStub, FIRST_LOGIN_VARYING_SHAPE_BURST);
    assert.ok(ctx.counters.feed > 1, `pre-fix feed requests should double, got ${ctx.counters.feed}`);
    assert.ok(ctx.counters.profile > 1, `pre-fix profile requests should double, got ${ctx.counters.profile}`);
  });
}

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
