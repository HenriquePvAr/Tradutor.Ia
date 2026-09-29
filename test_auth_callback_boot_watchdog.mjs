import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const html = fs.readFileSync(new URL('./ui/auth_callback.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/gi)];
const watchdogSource = scripts.find(([_, attrs]) => !/type\s*=\s*["']module/i.test(attrs))?.[2];
const moduleSource = scripts.find(([_, attrs]) => /type\s*=\s*["']module/i.test(attrs))?.[2];
assert.ok(watchdogSource, 'classic pre-module watchdog exists');
assert.ok(moduleSource, 'auth callback module exists');

function harness() {
  const callbacks = new Map();
  const listeners = new Map();
  let nextTimer = 1;
  const moduleScript = {id: 'authCallbackModule'};
  const spinner = {hidden: false};
  const message = {textContent: 'Concluindo autenticação…', dataset: {}, children: [], appendChild(node) { this.children.push(node); }};
  const historyCalls = [];
  const window = {
    __yomuAuthCallbackStarted: false,
    __yomuAuthCallbackBootFailed: false,
    setTimeout(callback, delay) {
      const id = nextTimer++;
      callbacks.set(id, {callback, delay});
      return id;
    },
    clearTimeout(id) { callbacks.delete(id); },
    addEventListener(type, callback, capture) {
      const entries = listeners.get(type) || [];
      entries.push({callback, capture});
      listeners.set(type, entries);
    },
    removeEventListener(type, callback) {
      listeners.set(type, (listeners.get(type) || []).filter((entry) => entry.callback !== callback));
    },
  };
  const context = {
    window,
    document: {getElementById(id) {
      return {authCallbackModule: moduleScript, authCallbackSpinner: spinner, msg: message}[id] || null;
    }},
    history: {replaceState(...args) { historyCalls.push(args); }},
  };
  vm.runInNewContext(watchdogSource, context, {timeout: 1000});
  return {
    window, spinner, message, callbacks, listeners, historyCalls, moduleScript, context,
    fireTimer(id = 1) { callbacks.get(id)?.callback(); },
    fireModuleError() {
      for (const entry of listeners.get('error') || []) {
        entry.callback({target: moduleScript});
      }
    },
    startModule() {
      const handshake = /window\.__yomuAuthCallbackStarted\s*=\s*true;\s*window\.clearTimeout\(window\.__yomuAuthCallbackBootTimer\);/.exec(moduleSource);
      assert.ok(handshake, 'module performs the watchdog handshake before callback work');
      vm.runInNewContext(handshake[0], context, {timeout: 1000});
    },
  };
}

test('module starts normally and disarms only the boot watchdog', () => {
  const h = harness();
  assert.equal(h.callbacks.get(1)?.delay, 12000);
  h.startModule();
  assert.equal(h.window.__yomuAuthCallbackStarted, true);
  assert.equal(h.callbacks.size, 0);
  assert.equal(h.message.textContent, 'Concluindo autenticação…');
});

test('module never starts: bounded watchdog shows terminal retry guidance and scrubs URL', () => {
  const h = harness();
  h.fireTimer();
  assert.equal(h.spinner.hidden, true);
  assert.match(h.message.textContent, /Tente abrir o link novamente/);
  assert.equal(h.message.dataset.state, 'error');
  assert.equal(h.window.__yomuAuthCallbackBootFailed, true);
  assert.deepEqual(h.historyCalls, [[null, '', '/auth/callback']]);
  assert.equal(h.callbacks.size, 0);
});

test('module import error is caught only for the callback module element', () => {
  const h = harness();
  for (const entry of h.listeners.get('error') || []) entry.callback({target: {id: 'unrelated'}});
  assert.equal(h.spinner.hidden, false);
  h.fireModuleError();
  assert.equal(h.spinner.hidden, true);
  assert.match(h.message.textContent, /Não foi possível iniciar a autenticação/);
  assert.equal(h.historyCalls.length, 1);
});

test('inner callback failures and exchange timeouts remain owned by the module state machine', () => {
  assert.match(moduleSource, /withCallbackTimeout\(\s*getSupabaseClient\([\s\S]*?'client_initialization'/);
  assert.match(moduleSource, /withCallbackTimeout\(\s*client\.auth\.exchangeCodeForSession\(code\)[\s\S]*?'code_exchange'/);
  assert.match(moduleSource, /session_confirmation', 8000/);
  assert.match(moduleSource, /catch \(err\)[\s\S]*?msg\.textContent = err\?\.code === 'auth_link_expired'/);
  assert.match(moduleSource, /CALLBACK_TIMEOUT_MS = 15000/);
});

async function runInnerCallback(exchange, {search = '?code=synthetic-auth-code', verifier = true} = {}) {
  const h = harness();
  let clientCalls = 0;
  const storage = new Map();
  const client = {auth: {
    onAuthStateChange() { return {data: {subscription: {unsubscribe() {}}}}; },
    exchangeCodeForSession: exchange,
    async getSession() { return {data: {session: {user: {id: 'synthetic'}}}}; },
  }};
  const location = {search, replace: () => {}};
  Object.assign(h.context, {
    URLSearchParams, location,
    getSupabaseClient: async () => { clientCalls++; return client; },
    hasPkceCodeVerifier: async () => verifier,
    document: {...h.context.document, createElement: () => ({href: '', textContent: ''})},
    sessionStorage: {getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key)},
    localStorage: {getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key)},
    fetch: async () => ({ok: true}),
    setTimeout: h.window.setTimeout.bind(h.window),
    clearTimeout: h.window.clearTimeout.bind(h.window),
  });
  const executableModule = moduleSource.replace(/^\s*import\s+\{[^}]+\}\s+from\s+'[^']+';\s*/m, '');
  vm.runInNewContext(executableModule, h.context, {timeout: 1000});
  for (let i = 0; i < 20; i++) await Promise.resolve();
  h.clientCalls = clientCalls;
  return h;
}

test('callback exchange rejection reaches the inner terminal error state', async () => {
  const h = await runInnerCallback(() => Promise.reject(Object.assign(new Error('synthetic exchange failure'), {code: 'synthetic_failure'})));
  assert.equal(h.window.__yomuAuthCallbackStarted, true);
  assert.equal(h.spinner.hidden, true);
  assert.match(h.message.textContent, /Não foi possível concluir a autenticação/);
  assert.equal(h.message.dataset.state, 'error');
  assert.deepEqual(h.historyCalls, [[null, '', '/auth/callback']]);
});

test('callback exchange timeout reaches the inner terminal error state, not the boot watchdog', async () => {
  const h = await runInnerCallback(() => new Promise(() => {}));
  const exchangeTimer = [...h.callbacks.entries()].find(([, timer]) => timer.delay === 15000);
  assert.ok(exchangeTimer, 'code-exchange timeout was armed');
  exchangeTimer[1].callback();
  for (let i = 0; i < 20; i++) await Promise.resolve();
  assert.equal(h.window.__yomuAuthCallbackStarted, true);
  assert.equal(h.spinner.hidden, true);
  assert.match(h.message.textContent, /Não foi possível concluir a autenticação/);
  assert.equal(h.message.dataset.state, 'error');
  assert.notEqual(h.message.textContent, 'Concluindo autenticação…');
  assert.deepEqual(h.historyCalls, [[null, '', '/auth/callback']]);
});

test('signup confirmation without PKCE code succeeds as confirmation-only and creates no session', async () => {
  for (const search of ['?email_confirmation=signup', '?email_confirmation=signup&code=unused-one-time-code']) {
    const h = await runInnerCallback(() => assert.fail('confirmation must never exchange a code'), {search});
    assert.equal(h.clientCalls, 0, 'confirmation does not initialize a session client');
    assert.equal(h.spinner.hidden, true);
    assert.match(h.message.textContent, /E-mail confirmado com sucesso/);
    assert.equal(h.message.dataset.state, 'confirmation_success');
    assert.deepEqual(h.historyCalls, [[null, '', '/auth/callback']]);
  }
});

test('missing auth code does not initialize client or attempt an empty exchange', async () => {
  let exchanges = 0;
  const h = await runInnerCallback(() => { exchanges++; }, {search: ''});
  assert.equal(exchanges, 0);
  assert.equal(h.clientCalls, 0);
  assert.equal(h.spinner.hidden, true);
  assert.equal(h.message.dataset.state, 'error');
  assert.match(h.message.textContent, /Não foi possível concluir a autenticação/);
});

test('PKCE code with verifier performs exactly one exchange', async () => {
  let exchanges = 0;
  const h = await runInnerCallback(async (code) => {
    exchanges++;
    assert.equal(code, 'synthetic-auth-code');
    return {data: {session: {user: {id: 'synthetic'}}}, error: null};
  });
  assert.equal(exchanges, 1);
  assert.equal(h.window.__yomuAuthCallbackStarted, true);
});

test('PKCE code without verifier fails locally without an exchange request', async () => {
  let exchanges = 0;
  const h = await runInnerCallback(() => { exchanges++; }, {verifier: false});
  assert.equal(exchanges, 0);
  assert.equal(h.spinner.hidden, true);
  assert.equal(h.message.dataset.state, 'error');
  assert.match(h.message.textContent, /aberto em outro contexto/);
});

test('Supabase callback error and expired link are not echoed into UI or diagnostics', async () => {
  const secretLike = 'private-code-value-must-not-appear';
  const h = await runInnerCallback(() => assert.fail('error callback must not exchange'), {
    search: `?error_code=otp_expired&error_description=${secretLike}`,
  });
  assert.match(h.message.textContent, /link expirou ou já foi usado/);
  assert.doesNotMatch(h.message.textContent, new RegExp(secretLike));
  assert.deepEqual(h.historyCalls, [[null, '', '/auth/callback']]);
});
