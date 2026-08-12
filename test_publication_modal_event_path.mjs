// Regression harness for the history-card publication action.
//
// It loads the REAL static/tradutor_ui.js into a small DOM model, lets the real
// bootstrap/render path create history cards, then dispatches the same delegated
// document click path used by a user action. It intentionally does not call
// openPublicationModal() directly and never submits the publication form.

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
  catch (err) { failures.push({ name, message: String(err && err.stack || err) }); }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function makeClassList(node) {
  const sync = () => { node.attributes.class = node.className; };
  const tokens = () => String(node.className || '').split(/\s+/).filter(Boolean);
  return {
    add(...values) {
      const set = new Set(tokens());
      values.forEach((value) => set.add(value));
      node.className = Array.from(set).join(' ');
      sync();
    },
    remove(...values) {
      const remove = new Set(values);
      node.className = tokens().filter((value) => !remove.has(value)).join(' ');
      sync();
    },
    contains(value) { return tokens().includes(value); },
    toggle(value, force) {
      const has = this.contains(value);
      const enabled = force === undefined ? !has : Boolean(force);
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
  set textContent(value) {
    this._text = String(value ?? '');
    this.children = [];
    this._innerHTML = escapeHtml(this._text);
  }
  get innerText() { return this.textContent; }
  set innerText(value) { this.textContent = value; }
  get innerHTML() { return this._innerHTML || escapeHtml(this.textContent); }
  set innerHTML(value) {
    this._innerHTML = String(value ?? '');
    this._text = this._innerHTML.replace(/<[^>]+>/g, '');
    this.children = [];
    if (this.id === 'histList') parseHistoryList(this, this._innerHTML);
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    if (child.id) this.ownerDocument._ids.set(child.id, child);
    return child;
  }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) {
    this.children = [];
    children.filter(Boolean).forEach((child) => this.appendChild(child));
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
    this.parentElement = null;
  }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() {}
  get offsetTop() { return 20; }
  get offsetHeight() { return 32; }
  getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 32, width: 100, height: 32 }; }
  getContext() {
    return {
      clearRect() {},
      fillRect() {},
      beginPath() {},
      arc() {},
      fill() {},
      stroke() {},
      moveTo() {},
      lineTo() {},
      createLinearGradient: () => ({ addColorStop() {} }),
      createRadialGradient: () => ({ addColorStop() {} }),
    };
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === 'id') { this.id = text; this.ownerDocument._ids.set(text, this); }
    if (name === 'class') { this.className = text; }
    if (name === 'disabled') { this.disabled = true; }
    if (name === 'hidden') { this.hidden = true; }
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
  addEventListener(type, fn, options = {}) {
    (this._listeners[type] = this._listeners[type] || []).push({ fn, options });
  }
  dispatchEvent(event) {
    const path = [];
    let node = event.target && event.target.nodeType === 3 ? event.target.parentElement : event.target;
    while (node) { path.push(node); node = node.parentElement; }
    if (!path.includes(this.ownerDocument)) path.push(this.ownerDocument);
    const invoke = (target) => {
      for (const { fn } of target._listeners?.[event.type] || []) fn(event);
    };
    path.slice().reverse().forEach(invoke);
    path.forEach(invoke);
    return !event.defaultPrevented;
  }
  click() {
    const event = {
      type: 'click',
      target: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
    };
    this.ownerDocument.dispatchEvent(event);
  }

  matches(selector) {
    if (!selector) return false;
    if (selector === '*') return true;
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (/^(\.[a-z0-9_-]+)+$/i.test(selector)) {
      const own = String(this.className || '').split(/\s+/);
      return selector.split('.').filter(Boolean).every((cls) => own.includes(cls));
    }
    if (selector.startsWith('.')) return String(this.className || '').split(/\s+/).includes(selector.slice(1));
    const classDataMatch = selector.match(/^\.([a-z0-9_-]+)\[data-([a-z-]+)="([^"]*)"\]$/i);
    if (classDataMatch) {
      const [, cls, rawKey, expected] = classDataMatch;
      const key = rawKey.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return String(this.className || '').split(/\s+/).includes(cls) && this.dataset[key] === expected;
    }
    const dataMatch = selector.match(/^([a-z]+)?\[data-([a-z-]+)(?:="([^"]*)")?\](?::not\(:disabled\))?$/i);
    if (dataMatch) {
      const [, tag, rawKey, expected] = dataMatch;
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      if (selector.includes(':not(:disabled)') && this.disabled) return false;
      const key = rawKey.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (!Object.prototype.hasOwnProperty.call(this.dataset, key)) return false;
      return expected === undefined || this.dataset[key] === expected;
    }
    if (/^[a-z]+$/i.test(selector)) return this.tagName === selector.toUpperCase();
    return false;
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches?.(selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const selectors = String(selector).split(',').map((value) => value.trim()).filter(Boolean);
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

function parseAttrs(source, node) {
  for (const [, name, value = ''] of source.matchAll(/\s([a-zA-Z0-9_-]+)(?:="([^"]*)")?/g)) {
    node.setAttribute(name, value);
  }
}

function parseHistoryList(host, html) {
  const itemParts = html.split('<div class="hist-item"').slice(1);
  for (const part of itemParts) {
    const attrs = part.slice(0, part.indexOf('>'));
    const item = host.ownerDocument.createElement('div');
    item.setAttribute('class', 'hist-item');
    parseAttrs(attrs, item);
    item.textContent = part.replace(/<[^>]+>/g, ' ');
    const actions = host.ownerDocument.createElement('div');
    actions.setAttribute('class', 'hm-actions');
    item.appendChild(actions);
    for (const match of part.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)) {
      const button = host.ownerDocument.createElement('button');
      parseAttrs(match[1], button);
      const text = match[2].replace(/<[^>]+>/g, '').trim();
      button.textContent = text;
      const span = host.ownerDocument.createElement('span');
      span.textContent = text || 'nested';
      button.appendChild(span);
      actions.appendChild(button);
    }
    host.appendChild(item);
  }
}

function installModal(document) {
  const overlay = document.ensureId('publicationModalOverlay');
  overlay.setAttribute('class', 'modal-overlay');
  overlay.setAttribute('aria-hidden', 'true');
  const ids = [
    ['publicationSummary', 'div'],
    ['publicationPending', 'div'],
    ['publicationForm', 'form'],
    ['publicationTitle', 'input'],
    ['publicationDescription', 'textarea'],
    ['publicationTags', 'input'],
    ['publicationVisibility', 'select'],
    ['publicationAllowComments', 'input'],
    ['publicationPublishConsent', 'input'],
    ['publicationConfirm', 'input'],
    ['publicationError', 'div'],
    ['publicationSubmit', 'button'],
    ['publicationCancel', 'button'],
    ['publicationModalClose', 'button'],
  ];
  ids.forEach(([id, tag]) => {
    const node = document.ensureId(id, tag);
    overlay.appendChild(node);
  });
  document.getElementById('publicationPublishConsent').type = 'checkbox';
  document.getElementById('publicationConfirm').type = 'checkbox';
  document.getElementById('publicationAllowComments').type = 'checkbox';
  document.getElementById('publicationSubmit').disabled = true;
  return overlay;
}

function makeRecord(id, overrides = {}) {
  return {
    id,
    job_id: id,
    run_id: `run-${id}`,
    operation_kind: 'translation',
    chapter_name: 'Synthetic Chapter',
    series_name: 'Synthetic Series',
    slug: 'synthetic-chapter',
    status: 'finished',
    pdf_path: 'C:/synthetic/artifact.pdf',
    output_folder: 'C:/synthetic',
    quality_report_path: 'C:/synthetic/quality_report.json',
    output_verification: 'manifest_verified',
    publication_manifest_ready: false,
    quality_gate: true,
    review_status: 'none',
    community_ownership: 'owned',
    pages_processed: 1,
    groups_translated: 1,
    total_seconds: 1,
    ...overrides,
  };
}

async function loadTradutorUi(history, options = {}) {
  const document = new FakeDocument();
  [
    'boot', 'bootNodes', 'bootNodeLabels', 'appStatus', 'railIndicator', 'histList', 'histCount', 'histSearch',
    'historyPendingPreviews', 'seriesSearch', 'seriesSort', 'dashChapters', 'dashSeries',
    'dashPages', 'dashApproved', 'dashSeriesList', 'dashActivityList',
  ].forEach((id) => document.ensureId(id));
  ['inicio', 'nova', 'queue', 'hist', 'community', 'cfg', 'logs', 'profile'].forEach((tabName) => {
    document.ensureId(`view-${tabName}`).setAttribute('class', tabName === 'inicio' ? 'panel-view active' : 'panel-view');
    const tab = document.createElement('button');
    tab.setAttribute('class', tabName === 'inicio' ? 'rail-tab active' : 'rail-tab');
    tab.setAttribute('data-tab', tabName);
    tab.textContent = tabName;
    document.body.appendChild(tab);
  });
  installModal(document);
  const window = {
    document,
    location: { search: '', hostname: '127.0.0.1', origin: 'http://127.0.0.1:8080' },
    URLSearchParams,
    URL,
    crypto: { randomUUID: () => 'synthetic-correlation' },
    performance: { now: () => 0 },
    console,
    setTimeout: (fn) => { queueMicrotask(fn); return 1; },
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    requestAnimationFrame: () => 1,
    cancelAnimationFrame() {},
    addEventListener() {},
    sessionStorage: { getItem: () => '[]', setItem() {}, removeItem() {}, clear() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    fetch: async (url) => ({
      status: 200,
      ok: true,
      json: async () => String(url).startsWith('/api/ui/bootstrap')
        ? { status: 'ready', history, community: { authenticated: true, user_id: 'owner' }, profile: {}, settings: {}, queue: [], logs: [] }
        : {},
    }),
  };
  window.window = window;
  window.globalThis = window;
  const context = vm.createContext(window);
  const source = fs.readFileSync(path.join(ROOT, 'static', 'tradutor_ui.js'), 'utf8')
    .replace('  refreshBootstrap();', '  // refreshBootstrap disabled by publication modal event-path harness')
    .replace(/\}\)\(\);\s*$/, `
  if (window.__publicationModalEventHarness) {
    window.__publicationModalEventHarness.loadHistory = (history, options = {}) => {
      appState.bootstrap = {community: {authenticated: true, user_id: 'owner'}};
      appState.history = Array.isArray(history) ? history : [];
      setGlobal('__tradutorAuthState', 'authenticated');
      setGlobal('__tradutorCommunityAuthenticated', true);
      appState.expandedFolders = options.expandFolders === false
        ? new Set()
        : new Set(appState.history.map((record) => seriesFromRecord(record).toLowerCase()));
      renderHistory();
    };
  }
})();`);
  window.__publicationModalEventHarness = {};
  vm.runInContext(source, context, { filename: 'static/tradutor_ui.js' });
  window.__publicationModalEventHarness.loadHistory(history, options);
  return { window, document };
}

function clickNestedText(document, button) {
  const nested = button.children[0];
  const textNode = { nodeType: 3, parentElement: nested, textContent: nested.textContent };
  const event = {
    type: 'click',
    target: textNode,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
  };
  document.dispatchEvent(event);
}

function clickRetargetedThroughComposedPath(document, button) {
  const path = [];
  let node = button;
  while (node) { path.push(node); node = node.parentElement; }
  if (!path.includes(document)) path.push(document);
  const event = {
    type: 'click',
    target: document,
    defaultPrevented: false,
    composedPath: () => path,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
  };
  document.dispatchEvent(event);
}

function modalState(document) {
  const overlay = document.getElementById('publicationModalOverlay');
  return {
    open: overlay.classList.contains('show'),
    ariaHidden: overlay.getAttribute('aria-hidden'),
    title: document.getElementById('publicationTitle').value,
    submitDisabled: document.getElementById('publicationSubmit').disabled,
  };
}

await test('reconstructed history publication button opens modal through delegated nested click', async () => {
  const child = makeRecord('reconstruction-child-1', {
    operation_kind: 'artifact_reconstruction',
    operation_label: 'Reconstrução corrigida',
    output_verification: 'legacy_unverified',
    publication_manifest_ready: true,
  });
  const { document } = await loadTradutorUi([child]);
  const item = document.querySelector('.hist-item');
  assert.equal(item.dataset.id, 'reconstruction-child-1');
  const button = item.querySelector('button[data-action="publish"]');
  assert.equal(button.textContent, 'Publicar na comunidade');
  clickNestedText(document, button);
  assert.deepEqual(modalState(document), {
    open: true,
    ariaHidden: 'false',
    title: 'Synthetic Chapter',
    submitDisabled: true,
  });
});

await test('retargeted browser click opens modal using composed event path', async () => {
  const child = makeRecord('reconstruction-retargeted-1', {
    operation_kind: 'artifact_reconstruction',
    operation_label: 'ReconstruÃ§Ã£o corrigida',
    output_verification: 'legacy_unverified',
    publication_manifest_ready: true,
  });
  const { document } = await loadTradutorUi([child]);
  const button = document.querySelector('.hist-item').querySelector('button[data-action="publish"]');
  clickRetargetedThroughComposedPath(document, button);
  assert.deepEqual(modalState(document), {
    open: true,
    ariaHidden: 'false',
    title: 'Synthetic Chapter',
    submitDisabled: true,
  });
});

await test('history search opens matching folder so publish action is physically reachable', async () => {
  const child = makeRecord('reconstruction-search-open-1', {
    operation_kind: 'artifact_reconstruction',
    operation_label: 'ReconstruÃ§Ã£o corrigida',
    output_verification: 'legacy_unverified',
    publication_manifest_ready: true,
    chapter_name: 'reconstruction-search-open-1',
    series_name: 'Searchable Series',
  });
  const { document } = await loadTradutorUi([child], { expandFolders: false });
  assert.match(document.getElementById('histList').innerHTML, /community-folder\s+"/);
  const search = document.getElementById('histSearch');
  search.value = 'reconstruction-search-open-1';
  search.dispatchEvent({
    type: 'input',
    target: search,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
  });
  assert.match(document.getElementById('histList').innerHTML, /community-folder open/);
});

await test('normal translation publication button still opens modal', async () => {
  const { document } = await loadTradutorUi([makeRecord('translation-job-1')]);
  document.querySelector('.hist-item').querySelector('button[data-action="publish"]').click();
  assert.equal(modalState(document).open, true);
  assert.equal(modalState(document).title, 'Synthetic Chapter');
});

await test('historical ineligible source remains denied and cannot open modal', async () => {
  const source = makeRecord('historical-source-1', {
    status: 'review_required',
    quality_gate: false,
    review_status: 'required',
  });
  const { document } = await loadTradutorUi([source]);
  const button = document.querySelector('.hist-item').querySelector('button[data-action="publish"]');
  assert.equal(button.disabled, true);
  button.click();
  assert.equal(modalState(document).open, false);
});

await test('close and reopen preserves the selected reconstruction child target', async () => {
  const childA = makeRecord('reconstruction-child-a', {
    operation_kind: 'artifact_reconstruction',
    operation_label: 'Reconstrução corrigida',
    publication_manifest_ready: true,
    output_verification: 'legacy_unverified',
    chapter_name: 'Child A',
  });
  const childB = makeRecord('reconstruction-child-b', {
    operation_kind: 'artifact_reconstruction',
    operation_label: 'Reconstrução corrigida',
    publication_manifest_ready: true,
    output_verification: 'legacy_unverified',
    chapter_name: 'Child B',
  });
  const { document } = await loadTradutorUi([childA, childB]);
  const items = document.querySelectorAll('.hist-item');
  items[0].querySelector('button[data-action="publish"]').click();
  assert.equal(modalState(document).title, 'Child A');
  document.getElementById('publicationCancel').click();
  assert.equal(modalState(document).open, false);
  items[1].querySelector('button[data-action="publish"]').click();
  assert.equal(modalState(document).title, 'Child B');
});

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exitCode = 1;
