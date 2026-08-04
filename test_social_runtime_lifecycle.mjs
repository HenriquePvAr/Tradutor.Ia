// Runtime lifecycle harness for the authenticated community mount.
//
// It loads the REAL static/social_community.js through a Node vm module linker, with a
// minimal fake DOM, a fake fetch, a counting auth provider and a counting social API, so
// the assertions run the shipped functions rather than a rewritten copy.
//
// Reproduced bugs this covers:
//   * one mount emitted ~5 GET /feed and ~5 GET /profile/me, because the auth layer calls
//     its handler several times for a single sign-in and every call re-rendered and
//     re-requested the profile;
//   * a second Supabase client existed because auth_ui.js imported auth_provider.js with a
//     per-call cache key while social_* imported the bare URL.
//
// No credentials of any kind: the sessions here are synthetic objects.

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

// ---------------------------------------------------------------------------
// Minimal DOM: only what social_community.js touches while rendering.
// ---------------------------------------------------------------------------
function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    style: {},
    dataset: {},
    attributes: {},
    className: '',
    _text: '',
    get textContent() { return this._text; },
    set textContent(value) { this._text = String(value); this.children = []; },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    removeAttribute(k) { delete this.attributes[k]; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    addEventListener() {},
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { this.children.push(...kids); },
    replaceChildren(...kids) { this.children = kids.filter(Boolean); },
    remove() {},
    focus() {},
    click() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    classList: { add() {}, remove() {}, toggle() {} },
  };
  return node;
}

function makeWindow(host) {
  const documentStub = {
    readyState: 'complete',
    activeElement: null,
    body: makeElement('body'),
    createElement: makeElement,
    createDocumentFragment: () => makeElement('fragment'),
    getElementById: (id) => (id === 'view-community' ? host : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
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

// ---------------------------------------------------------------------------
// Loader: instantiates the real module with stubbed sibling modules.
// ---------------------------------------------------------------------------
async function loadCommunityModule({ source, apiStub, authStub, bootstrapProvider }) {
  const host = makeElement('div');
  const win = makeWindow(host);
  const requests = [];
  win.fetch = async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => ({ community: { social: bootstrapProvider } }) };
  };
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
  return { module, host, win, requests, namespace: module.namespace };
}

// Counting stubs -------------------------------------------------------------
function makeApiStub(counters, { profileFails = false } = {}) {
  class SocialApiError extends Error {
    constructor(status, code) { super(code || `social_error_${status}`); this.status = status; this.code = code || ''; }
  }
  const page = async () => ({ items: [], next_cursor: null });
  const handler = {
    get(target, prop) {
      if (prop in target) return target[prop];
      return async () => ({ items: [], next_cursor: null });
    },
  };
  const base = {
    SocialApiError,
    messageForError: () => 'erro',
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

function makeAuthStub(counters, { client = { provider: 'supabase' } } = {}) {
  const listeners = [];
  return {
    listeners,
    async getSupabaseClient() { counters.clients += 1; return client; },
    async onAuthChange(handler) {
      counters.subscriptions += 1;
      listeners.push(handler);
      return () => { counters.unsubscribes += 1; const i = listeners.indexOf(handler); if (i >= 0) listeners.splice(i, 1); };
    },
    async signOut() { counters.signOuts += 1; },
    async getCanonicalAccessToken() { return 'synthetic-token'; },
  };
}

const SESSION = { user: { id: 'synthetic-user' }, access_token: 'synthetic-token' };
const SUPABASE_AVAILABLE = { provider: 'supabase', available: true, reason_code: '' };

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

// How the real auth layer behaves for ONE sign-in: supabase_auth.onAuthChange replays
// INITIAL_SESSION from the SDK subscription, replays it again after the bounded restore,
// replays INITIAL_SESSION_RESTORED in the background, and the SDK adds SIGNED_IN /
// TOKEN_REFRESHED. Five handler calls, one identity.
const SIGN_IN_EVENT_BURST = [
  [SESSION, 'INITIAL_SESSION'],
  [SESSION, 'INITIAL_SESSION'],
  [SESSION, 'INITIAL_SESSION_RESTORED'],
  [SESSION, 'SIGNED_IN'],
  [SESSION, 'TOKEN_REFRESHED'],
];

async function bootWith(source, options = {}) {
  const counters = { feed: 0, profile: 0, clients: 0, subscriptions: 0, unsubscribes: 0, signOuts: 0 };
  const authStub = makeAuthStub(counters, options);
  const loaded = await loadCommunityModule({
    source,
    apiStub: makeApiStub(counters, options),
    authStub,
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

// ---------------------------------------------------------------------------
const CURRENT = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');

await test('one sign-in burst produces at most one feed load and one profile/me load', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 1, `feed requests: ${ctx.counters.feed}`);
});

await test('a failing profile/me is not retried once per auth event', async () => {
  const ctx = await bootWith(CURRENT, { profileFails: true });
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  assert.equal(ctx.counters.profile, 1, `profile/me requests: ${ctx.counters.profile}`);
});

await test('exactly one auth subscription and one Supabase client per context', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  assert.equal(ctx.counters.subscriptions, 1, `subscriptions: ${ctx.counters.subscriptions}`);
  assert.equal(ctx.counters.clients, 1, `clients created: ${ctx.counters.clients}`);
});

await test('a repeated bootstrap does not build new infrastructure', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  const before = { ...ctx.counters };
  // Re-entering boot() is what a second bootstrap / a re-mounted panel does.
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  assert.equal(ctx.counters.subscriptions, before.subscriptions);
  assert.equal(ctx.counters.clients, before.clients);
  assert.equal(ctx.counters.feed, before.feed, 'repeated events must not refetch the feed');
  assert.equal(ctx.counters.profile, before.profile, 'repeated events must not refetch the profile');
});

await test('a genuine session change reloads exactly once', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  await emit(ctx.authStub, [[null, 'SIGNED_OUT']]);
  await emit(ctx.authStub, [[{ user: { id: 'other-user' } }, 'SIGNED_IN'],
                            [{ user: { id: 'other-user' } }, 'TOKEN_REFRESHED']]);
  assert.equal(ctx.counters.profile, 2, `profile/me requests: ${ctx.counters.profile}`);
  assert.equal(ctx.counters.feed, 2, `feed requests: ${ctx.counters.feed}`);
});

await test('an unmount releases the subscription', async () => {
  const ctx = await bootWith(CURRENT);
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  ctx.namespace.unmountCommunity();
  assert.equal(ctx.counters.unsubscribes, 1, 'the auth subscription must be released');
  assert.equal(ctx.authStub.listeners.length, 0, 'no listener may survive an unmount');
});

await test('no Supabase client is built when the provider is local', async () => {
  const ctx = await bootWith(CURRENT, { bootstrapProvider: { provider: 'local', available: true } });
  assert.equal(ctx.counters.clients, 0);
  assert.equal(ctx.counters.subscriptions, 0);
});

// --- negative control: the code before the fix must duplicate --------------
const PREVIOUS = process.env.SOCIAL_COMMUNITY_PREVIOUS
  ? fs.readFileSync(process.env.SOCIAL_COMMUNITY_PREVIOUS, 'utf8')
  : '';

if (PREVIOUS) {
  await test('NEGATIVE CONTROL: the pre-fix module duplicates feed and profile requests', async () => {
    const ctx = await bootWith(PREVIOUS, { profileFails: true });
    await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
    assert.ok(ctx.counters.feed > 1, `pre-fix feed requests should burst, got ${ctx.counters.feed}`);
    assert.ok(ctx.counters.profile > 1, `pre-fix profile requests should burst, got ${ctx.counters.profile}`);
  });
}

// --- module-identity control: one auth_provider URL across the shell -------
await test('every consumer imports auth_provider.js under the same specifier', async () => {
  const specifiers = new Set();
  for (const file of ['static/social_api.js', 'static/social_community.js', 'static/auth_ui.js']) {
    const text = fs.readFileSync(path.join(ROOT, file), 'utf8');
    for (const m of text.matchAll(/['"`](\/static\/auth_provider\.js[^'"`]*)['"`]/g)) specifiers.add(m[1]);
  }
  assert.deepEqual([...specifiers], ['/static/auth_provider.js'],
    `a differing specifier mints a second module instance and a second Supabase client: ${[...specifiers]}`);
});

await test('the provider layer imports the Supabase module under a stable specifier', async () => {
  const text = fs.readFileSync(path.join(ROOT, 'static', 'auth_provider.js'), 'utf8');
  assert.ok(!/supabase_auth\.js\?/.test(text), 'a cache key here mints a second GoTrueClient');
});

// ---------------------------------------------------------------------------
console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
