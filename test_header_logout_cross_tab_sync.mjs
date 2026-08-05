// Cross-tab header logout propagation.
//
// Confirmed bug: two tabs authenticated with the same account. Logging out in tab A
// correctly makes tab B's Comunidade panel (static/social_community.js) show the
// logged-out state and stop private requests - that module trusts an explicit
// SIGNED_OUT event unconditionally (see applySession()). But tab B's top header
// (#authArea/#authStatus/#authOpenBtn/#authLogoutBtn, rendered by static/auth_ui.js)
// stayed on the previous authenticated identity: "Sair" button still visible, no
// "Entrar" control, stale display name.
//
// Root cause (static/auth_ui.js, renderSession()): a guard added in commit 7fd0588
// ("fix: preserve verified auth across late SDK signout") deferred ANY explicit
// SIGNED_OUT event that arrived while a bearer token was cached in memory
// (window.__tradutorAccessToken), re-validating against the backend with that same
// (now stale) token instead of clearing the header immediately. That guard was meant
// for one narrow case: a late SDK event surfacing while THIS tab's own signIn() call
// was still settling (its setSession() persistence can race). It had no way to tell
// that case apart from a genuine SIGNED_OUT delivered to an idle, already-authenticated
// tab - same-tab logout already worked because the logout button's own click handler
// updates the header directly, bypassing renderSession() entirely; cross-tab logout has
// no such handler and depends entirely on this listener.
//
// The fix scopes the deferral to an explicit "this tab's own login is in flight" flag
// (ownLoginAttemptId) set only while the submit handler's own completeLoginFlow() call
// is outstanding. A SIGNED_OUT arriving with no login of this tab's own in flight - the
// cross-tab case - now falls through to the existing unconditional clear branch, the
// same one already used for a same-tab explicit SIGNED_OUT with no cached token.
//
// This harness links the REAL static/auth_ui.js, static/auth_provider.js and
// static/supabase_auth.js modules (via a Node vm module graph, not a reimplementation).
// The only synthetic piece is the Supabase SDK's `createClient` (a CDN import), whose
// `client.auth` is a scripted fake - onAuthStateChange delivers events exactly like the
// real SDK does for both a same-tab and a cross-tab (storage-driven) signal; from a
// registered listener's point of view the two are indistinguishable, which is exactly
// why the header must not rely on any signal other than the event itself.
//
// No credentials of any kind - every session/token below is synthetic.

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

// --- minimal fake DOM: real enough to carry addEventListener/dispatchEvent/click so
// the logout button's own click handler (same-tab path) and renderAuthShell's
// hidden/textContent writes (the actual header state under test) both work for real. ---
function makeEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, handler) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(handler); },
    removeEventListener(type, handler) { listeners.get(type)?.delete(handler); },
    dispatchEvent(event) { for (const handler of [...(listeners.get(event.type) || [])]) handler(event); return true; },
  };
}

function makeElement(tag, id = '') {
  const events = makeEventTarget();
  const node = {
    tagName: String(tag).toUpperCase(), id, children: [], style: {}, dataset: {}, attributes: {}, className: '',
    _hidden: false,
    get hidden() { return this._hidden; }, set hidden(v) { this._hidden = Boolean(v); },
    _text: '', get textContent() { return this._text; }, set textContent(v) { this._text = String(v); },
    value: '',
    setAttribute(k, v) { this.attributes[k] = String(v); }, removeAttribute(k) { delete this.attributes[k]; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    addEventListener: events.addEventListener, removeEventListener: events.removeEventListener,
    dispatchEvent: events.dispatchEvent,
    click() { this.dispatchEvent({ type: 'click', target: this, preventDefault() {} }); },
    appendChild(c) { this.children.push(c); return c; }, append(...k) { this.children.push(...k); },
    prepend(...k) { this.children.unshift(...k); },
    replaceChildren(...k) { this.children = k.filter(Boolean); }, remove() {}, focus() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    form: null,
  };
  return node;
}

function makeDocument() {
  const byId = new Map();
  ['authArea', 'authStatus', 'authOpenBtn', 'authLogoutBtn', 'authSurface', 'authError', 'authNote',
    'boot', 'authForm', 'authEmail', 'authPassword', 'authSubmit', 'authTogglePassword']
    .forEach((id) => byId.set(id, makeElement('div', id)));
  byId.get('authForm').addEventListener = makeEventTarget().addEventListener; // real form target below
  const formEvents = makeEventTarget();
  const form = byId.get('authForm');
  form.addEventListener = formEvents.addEventListener;
  form.dispatchEvent = formEvents.dispatchEvent;
  form.setAttribute = () => {};
  const documentElement = { dataset: {} };
  const body = makeElement('body');
  const doc = {
    documentElement, body,
    readyState: 'complete', activeElement: null,
    createElement: (tag) => makeElement(tag),
    createDocumentFragment: () => makeElement('fragment'),
    getElementById: (id) => byId.get(id) || null,
    querySelector: (sel) => {
      const m = /^#([\w-]+)$/.exec(sel);
      return m ? (byId.get(m[1]) || null) : null;
    },
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
  };
  return { doc, byId };
}

function makeWindow() {
  const { doc, byId } = makeDocument();
  const events = makeEventTarget();
  const win = {
    document: doc,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { origin: 'http://127.0.0.1:8080', search: '', hostname: '127.0.0.1' },
    setTimeout, clearTimeout,
    // auth_ui.js starts a 60s heartbeat interval once authenticated. unref() so it
    // never keeps this short-lived test process alive - clearInterval still works.
    setInterval: (fn, ms) => setInterval(fn, ms).unref(),
    clearInterval,
    URL, URLSearchParams, AbortController, Promise, console, atob,
    addEventListener: events.addEventListener, removeEventListener: events.removeEventListener,
    dispatchEvent: events.dispatchEvent,
  };
  win.window = win;
  win.globalThis = win;
  win.CustomEvent = function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; };
  return { win, byId, authEvents: events };
}

// Fake `client.auth`. `nativeEvents` are delivered through the real SDK subscription
// callback, spaced with a real timer (matches genuine async SDK delivery).
function makeFakeSupabaseAuth({ nativeEvents = [], sessionForGetSession = null } = {}) {
  let cb = null;
  return {
    _fire(event, session) { cb?.(event, session); },
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
      return { data: { user: sessionForGetSession.user }, error: null };
    },
    async signOut() { sessionForGetSession = null; cb?.('SIGNED_OUT', null); },
  };
}

async function loadAuthUi({ authUiSource, nativeEvents, sessionForGetSession, sessionEndpointAuthenticated = true, onWindowReady }) {
  const { win, byId, authEvents } = makeWindow();
  onWindowReady?.(win);
  let sessionCallCount = 0;
  win.fetch = async (url, options = {}) => {
    const u = String(url);
    if (u.includes('/api/community/auth/config')) {
      return { ok: true, json: async () => ({ provider: 'supabase', supabase_url: 'https://proj.supabase.co', publishable_key: 'pk' }) };
    }
    if (u.includes('/api/community/auth/session')) {
      sessionCallCount += 1;
      const hasBearer = Boolean((options.headers || {}).Authorization);
      const authenticated = hasBearer && sessionEndpointAuthenticated;
      return {
        ok: authenticated || !hasBearer, status: authenticated ? 200 : 401,
        json: async () => (authenticated ? { authenticated: true, user_id: 'cross-tab-user' } : { authenticated: false }),
      };
    }
    return { ok: true, json: async () => ({}) };
  };
  const context = vm.createContext(win);
  const fakeClient = { auth: makeFakeSupabaseAuth({ nativeEvents, sessionForGetSession }) };

  const stubs = new Map([
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
    // ./auth_presentation.js is an optional module auth_ui.js swallows failures for.
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
  const authUiModule = new vm.SourceTextModule(
    authUiSource, { context, identifier: '/static/auth_ui.js', importModuleDynamically, initializeImportMeta(meta) { meta.url = 'http://127.0.0.1:8080/static/auth_ui.js'; } });

  await authProviderModule.link(linker);
  await supabaseAuthModule.link(linker);
  await authUiModule.link(linker);
  await authProviderModule.evaluate();
  await supabaseAuthModule.evaluate();
  await authUiModule.evaluate();

  await new Promise((resolve) => setTimeout(resolve, 250));
  for (let i = 0; i < 40; i++) await Promise.resolve();

  return { win, byId, fakeClient, authEvents, sessionCalls: () => sessionCallCount };
}

const CURRENT = fs.readFileSync(path.join(ROOT, 'static', 'auth_ui.js'), 'utf8');
const SESSION = { access_token: 'tok-header-a', user: { id: 'cross-tab-user' }, expires_at: 9999999999 };

function headerSnapshot(byId) {
  return {
    logoutHidden: byId.get('authLogoutBtn').hidden,
    openHidden: byId.get('authOpenBtn').hidden,
    status: byId.get('authStatus').textContent,
  };
}

// ---------------------------------------------------------------------------
// Scenario: tab starts authenticated (own restore), then receives an explicit
// SIGNED_OUT with no login of its own in flight - the cross-tab shape.
// ---------------------------------------------------------------------------
const CROSS_TAB_LOGOUT_EVENTS = [[SESSION, 'INITIAL_SESSION'], [null, 'SIGNED_OUT']];

await test('cross-tab logout hides the Sair button in the other tab', async () => {
  const { byId } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION });
  assert.equal(byId.get('authLogoutBtn').hidden, true, 'authLogoutBtn should be hidden after cross-tab logout');
});

await test('cross-tab logout shows the Entrar button in the other tab', async () => {
  const { byId } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION });
  assert.equal(byId.get('authOpenBtn').hidden, false, 'authOpenBtn should be visible after cross-tab logout');
});

await test('cross-tab logout clears the stale status text (no leftover identity)', async () => {
  const { byId } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION });
  assert.equal(byId.get('authStatus').textContent, 'visitante');
});

await test('cross-tab logout marks the shell unauthenticated (drives sidebar/private-UI via applyCanonicalAuthSurface)', async () => {
  const { win } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION });
  assert.notEqual(win.document.documentElement.dataset.shellState, 'authenticated');
});

await test('cross-tab logout dispatches the canonical tradutor-auth-changed event with authenticated:false', async () => {
  const seen = [];
  const { win } = await loadAuthUi({
    authUiSource: CURRENT, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION,
    sessionEndpointAuthenticated: false,
    onWindowReady: (w) => w.addEventListener('tradutor-auth-changed', (e) => seen.push(e.detail)),
  });
  assert.ok(seen.length > 0, 'tradutor-auth-changed must fire at least once');
  const last = seen[seen.length - 1];
  assert.equal(last.authenticated, false, `last event should report authenticated:false, got ${JSON.stringify(last)}`);
  assert.equal(win.__tradutorAuthState === 'authenticated', false, 'auth state must not still read authenticated');
});

await test('the old event name tradutor:auth-changed is never required (only tradutor-auth-changed is used)', async () => {
  assert.ok(!CURRENT.includes("'tradutor:auth-changed'"), 'auth_ui.js must not use the incorrect legacy event name');
});

await test('window.__tradutorAccessToken is cleared after cross-tab logout (no stale bearer survives)', async () => {
  const { win } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION });
  assert.equal(win.__tradutorAccessToken, '');
});

// ---------------------------------------------------------------------------
// Same-tab logout (button click) must keep working exactly as before: immediate,
// synchronous header clear via the button's own handler.
// ---------------------------------------------------------------------------
await test('same-tab logout (button click) hides Sair immediately', async () => {
  const { byId } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: [[SESSION, 'INITIAL_SESSION']], sessionForGetSession: SESSION });
  assert.equal(byId.get('authLogoutBtn').hidden, false, 'precondition: logout button visible while authenticated');
  byId.get('authLogoutBtn').click();
  for (let i = 0; i < 20; i++) await Promise.resolve();
  assert.equal(byId.get('authLogoutBtn').hidden, true);
  assert.equal(byId.get('authOpenBtn').hidden, false);
  assert.equal(byId.get('authStatus').textContent, 'visitante');
});

// ---------------------------------------------------------------------------
// The narrow protection this fix must NOT regress: a late SIGNED_OUT that
// surfaces WHILE this tab's own signIn() is still in flight (setSession()
// persistence race) must still be deferred, not treated as a real logout.
// ---------------------------------------------------------------------------
await test('a late SIGNED_OUT during this tab\'s own in-flight login attempt is still deferred', async () => {
  const { win, byId, fakeClient } = await loadAuthUi({ authUiSource: CURRENT, nativeEvents: [], sessionForGetSession: null, sessionEndpointAuthenticated: true });
  // Simulate an in-flight login attempt directly at the module-state level is not
  // possible from outside (ownLoginAttemptId is module-private, by design). Instead
  // drive it the same way the real form submit does: start signIn via the SDK, then
  // fire a spurious SIGNED_OUT before the flow settles, through the real submit
  // handler wiring (authForm submit -> completeLoginFlow -> authApi.signIn).
  byId.get('authEmail').value = 'user@example.com';
  byId.get('authPassword').value = 'irrelevant-for-this-harness';
  fakeClient.auth.signInWithPassword = async ({ email }) => {
    // The spurious late SIGNED_OUT fires from "inside" the SDK call, before it resolves -
    // exactly the race the original fix (commit 7fd0588) targeted.
    fakeClient.auth._fire('SIGNED_OUT', null);
    return { data: { session: { ...SESSION, user: { id: 'cross-tab-user' }, access_token: 'tok-login-inflight' } }, error: null };
  };
  win.document.getElementById('authForm').dispatchEvent({ type: 'submit', preventDefault() {} });
  for (let i = 0; i < 40; i++) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 20));
  for (let i = 0; i < 40; i++) await Promise.resolve();
  // The login must still be allowed to complete authenticated - the spurious SIGNED_OUT
  // must not have wiped the header to visitante mid-flight.
  assert.equal(byId.get('authLogoutBtn').hidden, false, 'a genuine login in progress must not be cancelled by the SDK race');
});

// --- negative control: the pre-this-fix auth_ui.js (66a0681) must keep the header
// stuck on the previous authenticated identity after a cross-tab SIGNED_OUT. ---
const PREVIOUS_PATH = process.env.AUTH_UI_PREVIOUS
  || path.join(ROOT, '.runtime', 'claude-header-logout-cross-tab', 'auth_ui_before.js');
const PREVIOUS = fs.existsSync(PREVIOUS_PATH) ? fs.readFileSync(PREVIOUS_PATH, 'utf8') : '';

if (PREVIOUS) {
  await test('NEGATIVE CONTROL: the pre-fix auth_ui.js leaves the Sair button visible after cross-tab logout', async () => {
    const { byId } = await loadAuthUi({ authUiSource: PREVIOUS, nativeEvents: CROSS_TAB_LOGOUT_EVENTS, sessionForGetSession: SESSION, sessionEndpointAuthenticated: true });
    assert.equal(byId.get('authLogoutBtn').hidden, false, 'pre-fix: Sair should incorrectly remain visible (the bug)');
    assert.equal(byId.get('authOpenBtn').hidden, true, 'pre-fix: Entrar should incorrectly remain hidden (the bug)');
  });
} else {
  failures.push({ name: 'negative control setup', message: `missing baseline file at ${PREVIOUS_PATH}` });
}

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
