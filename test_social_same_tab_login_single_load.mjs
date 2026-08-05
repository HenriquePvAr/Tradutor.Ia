// Covers the confirmed same-tab-login regression: authenticated smoke found
// 2x /api/community/social/feed + 2x /api/community/social/profile/me on the FIRST
// Community open right after completing login in the same browser tab, while F5 and a
// new authenticated tab stayed 1x/1x (already covered by test_social_restored_session_single_load.mjs
// and test_social_first_login_idempotency.mjs).
//
// Root cause reproduced deterministically below: static/social_community.js's two
// independent Supabase restore races (this module's own onAuthChange subscription and
// auth_ui.js's separate one, racing the same SDK client - see the comments above
// applySession()) can surface a real SIGNED_OUT event for an account that is actually
// signing IN, immediately followed by the SIGNED_IN that reflects the settled restore, for
// the exact same account. Before the fix, applySession() wiped active.profileRequest and
// active.sections on every authenticated-edge crossing regardless of *which* account was
// involved, so that SIGNED_OUT-then-SIGNED_IN pair (same account, same login) unconditionally
// re-ran the full load twice. The fix keys that wipe on the account id (session.user.id)
// instead: a same-account replay reuses the cache, a genuine account change still resets it.
//
// Same harness shape as the other test_social_*.mjs files in this repo: the REAL
// static/social_community.js through a Node vm module linker, a minimal fake DOM, a fake
// fetch, and counting stubs. No credentials of any kind - every session below is synthetic.
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
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
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
      return { id: 'same-tab-user', username: null, display_name: null, profile_configured: false };
    },
    updateMyProfile: async (fields) => ({ id: 'same-tab-user', ...fields, profile_configured: true }),
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

// The exact same account (session.user.id), arriving via a spurious SIGNED_OUT sandwiched
// between two SIGNED_IN events - the shape the two independent restore races can produce for
// a single real first login in the same tab (see comment block above).
const SESSION_A = { access_token: 'tok-same-tab-a', user: { id: 'same-tab-user' }, expires_at: 9999999999 };
const SESSION_B = { access_token: 'tok-same-tab-b', user: { id: 'same-tab-user' }, expires_at: 9999999999 };

const SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT = [
  [SESSION_A, 'SIGNED_IN'],
  [null, 'SIGNED_OUT'],       // restore-race artifact, not a real logout
  [SESSION_B, 'SIGNED_IN'],   // the settled restore for the SAME account, moments later
];

await test('same-tab login with a spurious SIGNED_OUT loads the feed exactly once', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT);
  assert.equal(ctx.counters.feed, 1, `feed requests: ${ctx.counters.feed}`);
});

await test('same-tab login with a spurious SIGNED_OUT loads profile/me exactly once', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
});

await test('the loaded feed/profile survive the spurious SIGNED_OUT (no 401, no stale error)', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT);
  assert.equal(ctx.counters.feed, 1);
  assert.equal(ctx.counters.profile, 1);
  // A further identical-shape replay (a stray TOKEN_REFRESHED, common right after) must not
  // add a third call.
  await emit(ctx.authStub, [[SESSION_B, 'TOKEN_REFRESHED']]);
  assert.equal(ctx.counters.feed, 1);
  assert.equal(ctx.counters.profile, 1);
});

await test('a genuine account change after the spurious signout still reloads once for the new account', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT);
  await emit(ctx.authStub, [[null, 'SIGNED_OUT']]);
  const OTHER_ACCOUNT = { access_token: 'tok-other', user: { id: 'a-different-user' }, expires_at: 9999999999 };
  await emit(ctx.authStub, [[OTHER_ACCOUNT, 'SIGNED_IN']]);
  assert.equal(ctx.counters.feed, 2, `feed requests: ${ctx.counters.feed}`);
  assert.equal(ctx.counters.profile, 2, `profile/me requests: ${ctx.counters.profile}`);
});

await test('a failing profile/me during the spurious-signout burst is not retried automatically', async () => {
  const ctx = await bootWith(CURRENT, { profileFails: true });
  await emit(ctx.authStub, SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
});

// --- negative control: the pre-fix module must double-load on this exact burst --
const PREVIOUS = process.env.SOCIAL_COMMUNITY_PREVIOUS
  ? fs.readFileSync(process.env.SOCIAL_COMMUNITY_PREVIOUS, 'utf8')
  : '';

if (PREVIOUS) {
  await test('NEGATIVE CONTROL: the pre-fix module double-loads on the spurious-signout burst', async () => {
    const ctx = await bootWith(PREVIOUS);
    await emit(ctx.authStub, SAME_TAB_LOGIN_WITH_SPURIOUS_SIGNOUT);
    assert.ok(ctx.counters.feed > 1, `pre-fix feed requests should double, got ${ctx.counters.feed}`);
    assert.ok(ctx.counters.profile > 1, `pre-fix profile requests should double, got ${ctx.counters.profile}`);
  });
}

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
