// Browser-side contract for resuming an interrupted job (TDD #56).
//
// This harness loads the REAL static/tradutor_ui.js into a small DOM model and feeds it
// the same runtime payload /api/ui/state returns. It then drives the real click path on
// the rendered control instead of calling an internal function, so what is proven is what
// a user actually gets: the "Retomar" button appears only for a job the BACKEND marked
// can_resume, targets that job's canonical id, sends exactly one request per activation
// and never fabricates a resumed state locally.
//
// Fully isolated: no network, no worker, no jobs database, no credentials. Every job id
// and chapter name below is synthetic.

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
  catch (err) { failures.push({ name, message: String((err && err.stack) || err) }); }
}

// ---------------------------------------------------------------------------
// Minimal DOM with real event dispatch.
// ---------------------------------------------------------------------------
function makeClassList(node) {
  const tokens = () => String(node.className || '').split(/\s+/).filter(Boolean);
  return {
    add(...values) { node.className = Array.from(new Set([...tokens(), ...values])).join(' '); },
    remove(...values) {
      const drop = new Set(values);
      node.className = tokens().filter((value) => !drop.has(value)).join(' ');
    },
    contains(value) { return tokens().includes(value); },
    toggle(value, force) {
      const enabled = force === undefined ? !this.contains(value) : Boolean(force);
      if (enabled) this.add(value); else this.remove(value);
      return enabled;
    },
  };
}

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentElement = null;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.style = { setProperty(name, value) { this[name] = String(value); } };
    this._listeners = {};
    this._text = '';
    this._innerHTML = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.type = '';
    this.className = '';
    this.id = '';
    this.classList = makeClassList(this);
  }

  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent || '').join('')
      : this._text;
  }
  set textContent(value) { this._text = String(value ?? ''); this.children = []; }
  get innerText() { return this.textContent; }
  set innerText(value) { this.textContent = value; }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) { this._innerHTML = String(value ?? ''); this.children = []; }
  get offsetParent() { return this.hidden ? null : (this.parentElement || this.ownerDocument); }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    if (child.id) this.ownerDocument._ids.set(child.id, child);
    return child;
  }
  append(...children) { children.filter(Boolean).forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) {
    this.children = [];
    children.filter(Boolean).forEach((child) => this.appendChild(child));
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
    this.parentElement = null;
  }
  contains(candidate) {
    let node = candidate;
    while (node) { if (node === this) return true; node = node.parentElement; }
    return false;
  }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 32, width: 100, height: 32 }; }
  getContext() {
    return {
      clearRect() {}, fillRect() {}, beginPath() {}, arc() {}, fill() {}, stroke() {},
      moveTo() {}, lineTo() {},
      createLinearGradient: () => ({ addColorStop() {} }),
      createRadialGradient: () => ({ addColorStop() {} }),
    };
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === 'id') { this.id = text; this.ownerDocument._ids.set(text, this); }
    if (name === 'class') this.className = text;
    if (name === 'disabled') this.disabled = true;
    if (name === 'hidden') this.hidden = true;
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = text;
    }
  }
  getAttribute(name) {
    if (name === 'class') return this.className || null;
    if (name === 'id') return this.id || null;
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name === 'hidden') this.hidden = false;
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    if (this._listeners[type]) this._listeners[type] = this._listeners[type].filter((f) => f !== fn);
  }
  dispatchEvent(event) {
    const chain = [];
    let node = event.target || this;
    while (node) { chain.push(node); node = node.parentElement; }
    if (!chain.includes(this.ownerDocument)) chain.push(this.ownerDocument);
    const invoke = (target) => { for (const fn of target._listeners?.[event.type] || []) fn(event); };
    chain.slice().reverse().forEach(invoke);
    chain.forEach(invoke);
    return !event.defaultPrevented;
  }
  click() {
    this.ownerDocument.dispatchEvent({
      type: 'click',
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
    });
  }

  matches(selector) {
    if (!selector) return false;
    if (selector === '*') return true;
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) {
      const own = String(this.className || '').split(/\s+/);
      return selector.split('.').filter(Boolean).every((cls) => own.includes(cls));
    }
    if (/^[a-z]+$/i.test(selector)) return this.tagName === selector.toUpperCase();
    return false;
  }
  closest(selector) {
    let node = this;
    while (node) { if (node.matches?.(selector)) return node; node = node.parentElement; }
    return null;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const selectors = String(selector).split(',').map((v) => v.trim()).filter(Boolean);
    const out = [];
    const visit = (node) => {
      if (selectors.some((sel) => node.matches?.(sel))) out.push(node);
      (node.children || []).forEach(visit);
    };
    this.children.forEach(visit);
    return out;
  }
}

class FakeDocument extends FakeElement {
  constructor() {
    super('#document', null);
    this.ownerDocument = this;
    this._ids = new Map();
    this.documentElement = new FakeElement('html', this);
    this.body = new FakeElement('body', this);
    this.appendChild(this.documentElement);
    this.documentElement.appendChild(this.body);
    this.activeElement = null;
    this.cookie = '';
    this.hidden = false;
    this.readyState = 'complete';
  }
  createElement(tag) { return new FakeElement(tag, this); }
  createDocumentFragment() { return new FakeElement('fragment', this); }
  getElementById(id) { return this._ids.get(id) || null; }
  ensureId(id, tag = 'div') {
    if (this._ids.has(id)) return this._ids.get(id);
    const node = this.createElement(tag);
    node.setAttribute('id', id);
    this.body.appendChild(node);
    return node;
  }
  querySelector(selector) {
    if (selector.startsWith('#')) return this.ensureId(selector.slice(1));
    return this.body.querySelector(selector);
  }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
}

// ---------------------------------------------------------------------------
// Load the real UI module with a recording transport.
// ---------------------------------------------------------------------------
function makeRecord(id, overrides = {}) {
  return {
    id,
    job_id: id,
    run_id: `run-${id}`,
    operation_kind: 'chapter',
    chapter_name: 'Capítulo sintético',
    slug: 'capitulo-sintetico',
    status: 'interrupted',
    recoverable: true,
    can_resume: true,
    interrupted_reason: 'worker_process_lost',
    attempt: 1,
    progress_current: 7,
    progress_total: 20,
    ...overrides,
  };
}

function makeRuntime(overrides = {}) {
  return {
    status: 'ready',
    pending: false,
    blocked: false,
    active: null,
    latest: null,
    latest_result: null,
    progress: { stage: 'ocr', stage_key: 'ocr', current: 0, total: 0 },
    logs: [],
    log_cursor: 0,
    queue: [],
    queue_running: false,
    resumable: [],
    source_review: null,
    source_ready: null,
    quality_review: null,
    worker: { online: true, worker_id: 'w1', pid: 0 },
    history_revision: 1,
    ...overrides,
  };
}

async function loadUi({ resumeResponse } = {}) {
  const document = new FakeDocument();
  ['boot', 'bootNodes', 'bootNodeLabels', 'appStatus', 'railIndicator', 'histList', 'histCount',
    'stageList', 'interruptedJobsPanel', 'interruptedJobsList', 'runStatusCard',
  ].forEach((id) => document.ensureId(id));
  ['inicio', 'nova', 'queue', 'hist', 'community', 'cfg', 'logs', 'profile'].forEach((tabName) => {
    document.ensureId(`view-${tabName}`)
      .setAttribute('class', tabName === 'inicio' ? 'panel-view active' : 'panel-view');
    const tab = document.createElement('button');
    tab.setAttribute('class', tabName === 'inicio' ? 'rail-tab active' : 'rail-tab');
    tab.setAttribute('data-tab', tabName);
    tab.textContent = tabName;
    document.body.appendChild(tab);
  });

  const calls = [];
  const toasts = [];
  const window = {
    document,
    location: { search: '', hostname: '127.0.0.1', origin: 'http://127.0.0.1:8080' },
    URLSearchParams,
    URL,
    CustomEvent: class CustomEvent {
      constructor(type, options = {}) {
        this.type = type;
        Object.assign(this, options);
      }
    },
    crypto: { randomUUID: () => 'synthetic-correlation' },
    performance: { now: () => 0 },
    console,
    confirm: () => true,
    setTimeout: (fn) => { queueMicrotask(fn); return 1; },
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    addEventListener() {},
    dispatchEvent() { return true; },
    AbortController,
    sessionStorage: { getItem: () => '[]', setItem() {}, removeItem() {}, clear() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    fetch: async (url, init = {}) => {
      const endpoint = String(url).split('?')[0];
      calls.push({ endpoint, body: init.body ? JSON.parse(init.body) : null });
      if (endpoint === '/api/ui/resume') {
        const response = resumeResponse || { status: 200, payload: { ok: true } };
        return {
          status: response.status,
          ok: response.status < 400,
          json: async () => response.payload,
        };
      }
      return { status: 200, ok: true, json: async () => ({}) };
    },
  };
  window.window = window;
  window.globalThis = window;
  const context = vm.createContext(window);
  const source = fs.readFileSync(path.join(ROOT, 'static', 'tradutor_ui.js'), 'utf8')
    .replace('  refreshBootstrap();', '  // refreshBootstrap disabled by the resume UI harness')
    .replace(/\}\)\(\);\s*$/, `
  if (window.__resumeUiHarness) {
    window.__resumeUiHarness.renderRuntime = renderRuntime;
    window.__resumeUiHarness.showToast = (message, kind) => window.__resumeUiHarness.toasts.push({message, kind});
    window.__resumeUiHarness.appState = appState;
    window.__resumeUiHarness.authenticate = () => {
      appState.bootstrap = {community: {authenticated: true, user_id: 'owner'}};
      setGlobal('__tradutorAuthState', 'authenticated');
      setGlobal('__tradutorCommunityAuthenticated', true);
    };
  }
})();`);
  window.__resumeUiHarness = { toasts };
  vm.runInContext(source, context, { filename: 'static/tradutor_ui.js' });
  // A synthetic signed-in session: polling is gated behind canonical authentication and
  // would otherwise never run. No credential of any kind is involved.
  window.__resumeUiHarness.authenticate();
  return { window, document, calls, toasts, harness: window.__resumeUiHarness };
}

function resumeButtons(document) {
  return document.getElementById('interruptedJobsList').children
    .map((row) => row.children.find((child) => child.tagName === 'BUTTON'))
    .filter(Boolean);
}

const RECOVERABLE_ID = 'aaaaaaaabbbbccccddddeeeeffff0001';
const OTHER_RECOVERABLE_ID = 'aaaaaaaabbbbccccddddeeeeffff0002';
const BLOCKED_ID = 'aaaaaaaabbbbccccddddeeeeffff0003';

await test('a recoverable interrupted job offers a real Retomar button', async () => {
  const { document, harness } = await loadUi();
  harness.renderRuntime(makeRuntime({
    resumable: [makeRecord(RECOVERABLE_ID, { chapter_name: 'Capítulo interrompido' })],
  }));
  assert.equal(document.getElementById('interruptedJobsPanel').hidden, false);
  const buttons = resumeButtons(document);
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].tagName, 'BUTTON');
  assert.equal(buttons[0].type, 'button');
  assert.equal(buttons[0].textContent, 'Retomar');
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[0].getAttribute('aria-label'), 'Retomar Capítulo interrompido');
});

await test('an interrupted job the backend refuses offers no action', async () => {
  const { document, harness } = await loadUi();
  harness.renderRuntime(makeRuntime({
    resumable: [makeRecord(BLOCKED_ID, { recoverable: false, can_resume: false })],
  }));
  assert.equal(document.getElementById('interruptedJobsPanel').hidden, true);
  assert.equal(resumeButtons(document).length, 0);
});

await test('running, queued, completed and cancelled jobs never grow the panel', async () => {
  for (const status of ['running', 'queued', 'finished', 'cancelled', 'failed']) {
    const { document, harness } = await loadUi();
    harness.renderRuntime(makeRuntime({
      latest: makeRecord(RECOVERABLE_ID, { status, can_resume: false, recoverable: false }),
      resumable: [],
    }));
    assert.equal(document.getElementById('interruptedJobsPanel').hidden, true, status);
  }
});

await test('the resume request carries the canonical id of the clicked job only', async () => {
  const { document, harness, calls } = await loadUi();
  harness.renderRuntime(makeRuntime({
    resumable: [
      makeRecord(RECOVERABLE_ID, { chapter_name: 'Primeiro' }),
      makeRecord(OTHER_RECOVERABLE_ID, { chapter_name: 'Segundo' }),
    ],
  }));
  const buttons = resumeButtons(document);
  assert.equal(buttons.length, 2);
  buttons[1].click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const resumeCalls = calls.filter((call) => call.endpoint === '/api/ui/resume');
  assert.equal(resumeCalls.length, 1);
  assert.deepEqual(resumeCalls[0].body, { job_id: OTHER_RECOVERABLE_ID });
});

await test('two rapid activations produce exactly one resume request', async () => {
  const { document, harness, calls } = await loadUi();
  harness.renderRuntime(makeRuntime({ resumable: [makeRecord(RECOVERABLE_ID)] }));
  const button = resumeButtons(document)[0];
  button.click();
  button.click();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, 'Retomando…');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls.filter((call) => call.endpoint === '/api/ui/resume').length, 1);
});

await test('a successful resume refreshes authoritative state instead of faking it', async () => {
  const { document, harness, calls } = await loadUi();
  harness.renderRuntime(makeRuntime({ resumable: [makeRecord(RECOVERABLE_ID)] }));
  resumeButtons(document)[0].click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(calls.some((call) => call.endpoint.startsWith('/api/ui/state')),
    `expected an authoritative state refresh, saw ${JSON.stringify(calls.map((c) => c.endpoint))}`);
  assert.equal(harness.appState.resumeBusyJobId, '');
  // The job is only shown as resumed once the backend says so; the local record is not
  // rewritten to "running" by the click.
  assert.equal(harness.appState.status, 'ready');
});

await test('a rejected resume keeps the action usable and never claims success', async () => {
  const { document, harness, calls } = await loadUi({
    resumeResponse: { status: 409, payload: { detail: 'Somente jobs interrompidos podem ser retomados.' } },
  });
  harness.renderRuntime(makeRuntime({ resumable: [makeRecord(RECOVERABLE_ID)] }));
  const button = resumeButtons(document)[0];
  button.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Retomar');
  assert.equal(harness.appState.resumeBusyJobId, '');
  assert.ok(calls.some((call) => call.endpoint.startsWith('/api/ui/state')));
});

await test('a job the backend no longer lists loses its button on the next poll', async () => {
  const { document, harness } = await loadUi();
  harness.renderRuntime(makeRuntime({ resumable: [makeRecord(RECOVERABLE_ID)] }));
  assert.equal(resumeButtons(document).length, 1);
  harness.renderRuntime(makeRuntime({ resumable: [] }));
  assert.equal(document.getElementById('interruptedJobsPanel').hidden, true);
  assert.equal(resumeButtons(document).length, 0);
});

await test('resume and cancel are never both offered for the same job', async () => {
  const { document, harness } = await loadUi();
  harness.renderRuntime(makeRuntime({ resumable: [makeRecord(RECOVERABLE_ID)] }));
  assert.equal(document.getElementById('interruptedJobsPanel').hidden, false);
  assert.equal(document.getElementById('runCancelAction').hidden, true);

  harness.renderRuntime(makeRuntime({
    status: 'running',
    active: makeRecord(RECOVERABLE_ID, { status: 'running', can_resume: false }),
    resumable: [],
  }));
  assert.equal(document.getElementById('interruptedJobsPanel').hidden, true);
  assert.equal(document.getElementById('runCancelAction').hidden, false);
});

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exitCode = 1;
