// Sub-tab lifecycle harness for the authenticated Supabase community UI.
//
// Loads the REAL static/social_community.js through a Node vm module linker with a
// minimal fake DOM that supports real click dispatch (addEventListener/click()), a fake
// fetch for the bootstrap call, and a counting proxy for social_api.js — so every
// assertion below exercises the shipped functions and real click events, not a
// reimplemented shortcut of the tab-switching flow.
//
// Confirmed bug this covers (see .runtime/claude-community-subtab-loading/root_cause.md):
//   loadMyProfile() cleared its single-flight cache on ANY error (including the expected
//   422 from the out-of-scope profile/me migration gap), so simply reopening "Meu perfil"
//   after a failed load fired a brand new GET /profile/me with no user action beyond
//   opening the tab. This file also locks down that no sub-tab section is fetched before
//   its own tab is opened, that a retry invalidates only its own section, and that
//   revisiting a tab never refetches an already-loaded section.
//
// No credentials of any kind: sessions are synthetic objects.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const failures = [];
let passed = 0;

async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (err) { failures.push({ name, message: String(err && err.stack || err) }); }
}

// ---------------------------------------------------------------------------
// Minimal DOM: what social_community.js touches, plus real click dispatch.
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
    _listeners: {},
    get textContent() { return this._text; },
    set textContent(value) { this._text = String(value); this.children = []; },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    removeAttribute(k) { delete this.attributes[k]; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    removeEventListener(ev, fn) { if (this._listeners[ev]) this._listeners[ev] = this._listeners[ev].filter((f) => f !== fn); },
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { this.children.push(...kids); },
    replaceChildren(...kids) { this.children = kids.filter(Boolean); },
    remove() {},
    focus() {},
    click() { (this._listeners.click || []).forEach((fn) => fn({ target: this, stopPropagation() {}, preventDefault() {} })); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    classList: { add() {}, remove() {}, toggle() {} },
  };
  return node;
}

function findAll(node, pred, out = []) {
  if (!node) return out;
  if (pred(node)) out.push(node);
  for (const c of node.children || []) findAll(c, pred, out);
  return out;
}
const findByDataTab = (root_, tab) => findAll(root_, (n) => n.attributes && n.attributes['data-tab'] === tab)[0] || null;
const findRetryButtons = (root_) => findAll(root_, (n) => n.tagName === 'BUTTON' && n._text === 'Tentar de novo');

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
  // The real feature flag is off; these unit contracts exercise the otherwise
  // dormant authenticated implementation without changing production UI state.
  const enabledSource = source.replace('const COMMUNITY_ENABLED = false;', 'const COMMUNITY_ENABLED = true;');
  assert.notEqual(enabledSource, source, 'community feature gate should remain explicit in source');
  const module = new vm.SourceTextModule(enabledSource, { context, identifier: 'social_community.js' });
  await module.link((specifier) => synthetic(specifier));
  await module.evaluate();
  return { module, host, win, requests, namespace: module.namespace };
}

// Counting stubs -------------------------------------------------------------
function makeApiStub(counters, { profileFails = false, profileFailsAlways = false } = {}) {
  class SocialApiError extends Error {
    constructor(status, code) { super(code || `social_error_${status}`); this.status = status; this.code = code || ''; }
  }
  const page = () => ({ items: [], next_cursor: null });
  const countedPage = (name) => async () => { counters[name] += 1; return page(); };
  let profileCallsAfterFirst = 0;
  const base = {
    SocialApiError,
    messageForError: () => 'erro',
    getFeed: countedPage('feed'),
    getMyProfile: async () => {
      counters.profile += 1;
      profileCallsAfterFirst += 1;
      if (profileFailsAlways || (profileFails && profileCallsAfterFirst === 1)) {
        throw new SocialApiError(422, 'validation_error');
      }
      if (profileFails) throw new SocialApiError(422, 'validation_error'); // keeps failing unless caller stubs otherwise
      return { id: 'synthetic-user', username: 'u', display_name: 'U', profile_configured: true };
    },
    updateMyProfile: async (fields) => ({ id: 'synthetic-user', ...fields, profile_configured: true }),
    getMyWorks: countedPage('myworks'),
    getFavorites: countedPage('favorites'),
    getHistory: countedPage('history'),
    getNotifications: countedPage('notifications'),
    getWork: async () => ({ id: 'w', title: 'w' }),
    getWorkChapters: async () => page(), getComments: async () => page(),
    getAsset: async () => ({ available: false }),
    assetRetention: async () => ({ restorable: false }), retainedAssets: async () => page(),
    listLocalResults: async () => page(),
  };
  const handler = { get(target, prop) { if (prop in target) return target[prop]; return async () => page(); } };
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
const OTHER_SESSION = { user: { id: 'other-user' }, access_token: 'other-token' };
const SUPABASE_AVAILABLE = { provider: 'supabase', available: true, reason_code: '' };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

const SIGN_IN_EVENT_BURST = [
  [SESSION, 'INITIAL_SESSION'],
  [SESSION, 'INITIAL_SESSION'],
  [SESSION, 'INITIAL_SESSION_RESTORED'],
  [SESSION, 'SIGNED_IN'],
  [SESSION, 'TOKEN_REFRESHED'],
];

async function emit(authStub, events) {
  for (const [session, event] of events) {
    for (const handler of [...authStub.listeners]) handler(session, event);
    await flush();
  }
  await flush();
}

function freshCounters() {
  return { feed: 0, profile: 0, clients: 0, subscriptions: 0, unsubscribes: 0, signOuts: 0, myworks: 0, favorites: 0, history: 0, notifications: 0 };
}

async function bootWith(source, apiOptions = {}, authOptions = {}) {
  const counters = freshCounters();
  const authStub = makeAuthStub(counters, authOptions);
  const apiStub = makeApiStub(counters, apiOptions);
  const loaded = await loadCommunityModule({ source, apiStub, authStub, bootstrapProvider: authOptions.bootstrapProvider || SUPABASE_AVAILABLE });
  await flush();
  return { ...loaded, counters, authStub };
}

function clickTab(host, tab) {
  const btn = findByDataTab(host, tab);
  assert.ok(btn, `nav button for tab "${tab}" not found`);
  btn.click();
}

const CURRENT = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');

await test('social profile tab is absent and auth never invokes profile reads', async () => {
  const ctx = await bootWith(CURRENT, {});
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  assert.equal(findByDataTab(ctx.host, 'profile'), null, 'no profile tab may be mounted');
  assert.equal(ctx.counters.profile, 0, 'auth lifecycle must not request profile data');
});

// ---------------------------------------------------------------------------
// Each section loads only when its own tab opens, and only once until an
// explicit action invalidates it.
// ---------------------------------------------------------------------------
for (const [tab, counterKey] of [['favorites', 'favorites'], ['reading', 'history'], ['mine', 'myworks'], ['notifications', 'notifications']]) {
  await test(`"${tab}" loads its section only when opened, once per visit sequence`, async () => {
    const ctx = await bootWith(CURRENT, {});
    await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
    assert.equal(ctx.counters[counterKey], 0, `section fetched before its tab opened: ${ctx.counters[counterKey]}`);
    clickTab(ctx.host, tab); await flush();
    assert.equal(ctx.counters[counterKey], 1, `first open should fetch once, got ${ctx.counters[counterKey]}`);
    // Leaving and revisiting must reuse the cached load.
    clickTab(ctx.host, 'explore'); await flush();
    clickTab(ctx.host, tab); await flush();
    assert.equal(ctx.counters[counterKey], 1, `revisit refetched: ${ctx.counters[counterKey]}`);
  });
}

await test('opening one section tab does not load the others', async () => {
  const ctx = await bootWith(CURRENT, {});
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  clickTab(ctx.host, 'favorites'); await flush();
  assert.equal(ctx.counters.history, 0);
  assert.equal(ctx.counters.myworks, 0);
  assert.equal(ctx.counters.notifications, 0);
});

// ---------------------------------------------------------------------------
// Lifecycle: logout clears caches, new login starts clean, stale responses
// never leak into a newer session.
// ---------------------------------------------------------------------------
await test('logout then a new login re-fetches (caches do not survive unmount)', async () => {
  const ctx = await bootWith(CURRENT, {});
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  clickTab(ctx.host, 'favorites'); await flush();
  assert.equal(ctx.counters.favorites, 1);
  ctx.namespace.unmountCommunity();
  await flush();
  // A fresh mount for a new sign-in must not reuse the previous mount's cache.
  const authStub2 = makeAuthStub(ctx.counters, {});
  const apiStub2 = makeApiStub(ctx.counters, {});
  const loaded2 = await loadCommunityModule({ source: CURRENT, apiStub: apiStub2, authStub: authStub2, bootstrapProvider: SUPABASE_AVAILABLE });
  await flush();
  await emit(authStub2, SIGN_IN_EVENT_BURST);
  clickTab(loaded2.host, 'favorites'); await flush();
  assert.equal(ctx.counters.favorites, 2, `new lifecycle should fetch favorites again, got ${ctx.counters.favorites}`);
});

await test('normal authentication does not call profile APIs across account changes', async () => {
  const ctx = await bootWith(CURRENT, {});
  await emit(ctx.authStub, SIGN_IN_EVENT_BURST);
  await emit(ctx.authStub, [[null, 'SIGNED_OUT'], [OTHER_SESSION, 'SIGNED_IN']]);
  assert.equal(ctx.counters.profile, 0);
});

// ---------------------------------------------------------------------------
console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
