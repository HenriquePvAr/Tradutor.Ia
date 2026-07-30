import assert from 'node:assert/strict';
import test from 'node:test';

import { createAuthPresentationController } from './static/auth_presentation.js';

class FakeElement {
  constructor({dataset = {}, value = ''} = {}) {
    this.dataset = {...dataset};
    this.value = value;
    this.attributes = {};
    this.listeners = new Map();
    this.parent = null;
    this.fieldContainer = null;
    this.textContent = '';
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    const index = handlers.indexOf(handler);
    if (index >= 0) handlers.splice(index, 1);
  }

  // Counts handlers still attached, so teardown can be asserted, not assumed.
  listenerCount() {
    let total = 0;
    for (const handlers of this.listeners.values()) total += handlers.length;
    return total;
  }

  dispatch(type, target = this) {
    for (const handler of this.listeners.get(type) || []) handler({target});
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  contains(element) {
    for (let current = element; current; current = current.parent) {
      if (current === this) return true;
    }
    return false;
  }

  closest(selector) {
    return selector === '.auth-login-field' ? this.fieldContainer : null;
  }
}

function createHarness({reducedMotion = false} = {}) {
  const shell = new FakeElement({
    dataset: {interactionState: 'idle', compareState: 'both'},
  });
  const form = new FakeElement();
  const status = new FakeElement();
  const emailField = new FakeElement();
  const passwordField = new FakeElement();
  const email = new FakeElement();
  const password = new FakeElement();
  email.parent = form;
  password.parent = form;
  email.fieldContainer = emailField;
  password.fieldContainer = passwordField;
  const original = new FakeElement({dataset: {authCompare: 'original'}});
  const both = new FakeElement({dataset: {authCompare: 'both'}});
  const translated = new FakeElement({dataset: {authCompare: 'translated'}});
  const buttons = [original, both, translated];

  const selectors = new Map([
    ['#authSurface .auth-login-shell', shell],
    ['#authForm', form],
    ['#authCompareStatus', status],
  ]);
  const root = {
    activeElement: null,
    querySelector: (selector) => selectors.get(selector) || null,
    querySelectorAll: (selector) => selector === '#authForm input'
      ? [email, password]
      : selector === '[data-auth-compare]' ? buttons : [],
  };
  const windowRef = {
    matchMedia: () => ({matches: reducedMotion}),
    queueMicrotask: (callback) => callback(),
  };
  const controller = createAuthPresentationController({root, windowRef});
  return {
    shell, form, status, emailField, passwordField, email, password,
    original, both, translated, buttons, root, controller,
  };
}

test('starts idle with the balanced comparison', () => {
  const harness = createHarness();
  assert.equal(harness.controller.init(), true);
  assert.equal(harness.shell.dataset.interactionState, 'idle');
  assert.equal(harness.shell.dataset.compareState, 'both');
  assert.equal(harness.both.getAttribute('aria-pressed'), 'true');
  assert.equal(harness.original.getAttribute('aria-pressed'), 'false');
});

test('focus and blur only change presentation state', () => {
  const harness = createHarness();
  harness.controller.init();
  harness.root.activeElement = harness.email;
  harness.form.dispatch('focusin', harness.email);
  assert.equal(harness.shell.dataset.interactionState, 'form-focused');
  harness.root.activeElement = null;
  harness.form.dispatch('focusout', harness.email);
  assert.equal(harness.shell.dataset.interactionState, 'idle');
});

test('filled state reflects values without claiming validity', () => {
  const harness = createHarness();
  harness.controller.init();
  harness.email.value = 'local@example.test';
  harness.form.dispatch('input', harness.email);
  assert.equal(harness.emailField.dataset.filled, 'true');
  harness.email.value = '';
  harness.form.dispatch('input', harness.email);
  assert.equal(harness.emailField.dataset.filled, 'false');
  assert.equal(harness.emailField.dataset.valid, undefined);
});

test('comparison controls expose original, translated, and balanced states', () => {
  const harness = createHarness();
  harness.controller.init();
  harness.original.dispatch('click');
  assert.equal(harness.shell.dataset.compareState, 'original');
  assert.equal(harness.original.getAttribute('aria-pressed'), 'true');
  assert.match(harness.status.textContent, /original/i);
  harness.translated.dispatch('click');
  assert.equal(harness.shell.dataset.compareState, 'translated');
  assert.equal(harness.translated.getAttribute('aria-pressed'), 'true');
  harness.both.dispatch('click');
  assert.equal(harness.shell.dataset.compareState, 'both');
  assert.equal(harness.both.getAttribute('aria-pressed'), 'true');
});

test('submitting and error reflect functional auth states', () => {
  const harness = createHarness();
  harness.controller.init();
  harness.controller.reflectAuthState('auth_submitting');
  assert.equal(harness.shell.dataset.interactionState, 'submitting');
  harness.controller.reflectAuthState('invalid_credentials');
  assert.equal(harness.shell.dataset.interactionState, 'error');
});

test('new input clears only the visual error emphasis', () => {
  const harness = createHarness();
  harness.controller.init();
  harness.controller.reflectAuthState('auth_error');
  harness.root.activeElement = harness.password;
  harness.password.value = 'new attempt';
  harness.form.dispatch('input', harness.password);
  assert.equal(harness.shell.dataset.interactionState, 'form-focused');
});

test('presentation module never registers a submit listener', () => {
  const harness = createHarness();
  harness.controller.init();
  assert.equal(harness.form.listeners.has('submit'), false);
});

test('reduced motion preference is reflected without changing behavior', () => {
  const harness = createHarness({reducedMotion: true});
  harness.controller.init();
  assert.equal(harness.shell.dataset.reducedMotion, 'true');
  harness.translated.dispatch('click');
  assert.equal(harness.shell.dataset.compareState, 'translated');
});

test('initialization is idempotent', () => {
  const harness = createHarness();
  assert.equal(harness.controller.init(), true);
  assert.equal(harness.controller.init(), false);
  assert.equal(harness.form.listeners.get('focusin').length, 1);
});

// --- scene, pointer and responsive classification --------------------------
// A richer environment than the focus tests need: media matching per query
// string, frame scheduling, ResizeObserver and inline style support.

function createSceneHarness({
  media = {}, frames = true, resizeObserver = true, style = true,
} = {}) {
  const shell = new FakeElement({dataset: {interactionState: 'idle', compareState: 'both'}});
  if (style) {
    shell.style = {
      values: {},
      setProperty(name, value) { this.values[name] = value; },
    };
  }
  const form = new FakeElement();
  const status = new FakeElement();
  const emailField = new FakeElement();
  const passwordField = new FakeElement();
  const email = new FakeElement();
  const password = new FakeElement();
  email.parent = form;
  password.parent = form;
  email.fieldContainer = emailField;
  password.fieldContainer = passwordField;
  email.type = 'email';
  password.type = 'password';
  const original = new FakeElement({dataset: {authCompare: 'original'}});
  const both = new FakeElement({dataset: {authCompare: 'both'}});
  const translated = new FakeElement({dataset: {authCompare: 'translated'}});
  const buttons = [original, both, translated];

  const selectors = new Map([
    ['#authSurface .auth-login-shell', shell],
    ['#authForm', form],
    ['#authCompareStatus', status],
  ]);
  const root = {
    activeElement: null,
    querySelector: (selector) => selectors.get(selector) || null,
    querySelectorAll: (selector) => selector === '#authForm input'
      ? [email, password]
      : selector === '[data-auth-compare]' ? buttons : [],
  };

  const observed = [];
  let disconnected = 0;
  const scheduled = [];
  const windowRef = {
    innerWidth: media.innerWidth ?? 1440,
    innerHeight: media.innerHeight ?? 900,
    matchMedia: (queryText) => ({
      matches: Boolean(media[queryText]),
      addEventListener() {},
      removeEventListener() {},
    }),
    queueMicrotask: (callback) => callback(),
  };
  if (frames) {
    windowRef.requestAnimationFrame = (callback) => {
      scheduled.push(callback);
      return scheduled.length;
    };
    windowRef.cancelAnimationFrame = (handle) => { scheduled[handle - 1] = null; };
  }
  if (resizeObserver) {
    windowRef.ResizeObserver = class {
      constructor(callback) { this.callback = callback; }
      observe(target) { observed.push(target); }
      disconnect() { disconnected += 1; }
    };
  }

  function runFrames() {
    // Drains the queue, including frames scheduled from inside a frame.
    for (let guard = 0; guard < 10 && scheduled.some(Boolean); guard += 1) {
      const pending = scheduled.splice(0, scheduled.length);
      for (const callback of pending) if (callback) callback();
    }
  }

  const controller = createAuthPresentationController({root, windowRef});
  return {
    shell, form, status, email, password, emailField, passwordField,
    original, both, translated, buttons, root, windowRef, controller,
    runFrames, observed, disconnectedCount: () => disconnected,
  };
}

const DESKTOP_POINTER = {'(pointer: fine)': true, '(min-width: 900px)': true};

test('scene state derives exploring, filled and functional readings', () => {
  const harness = createSceneHarness();
  harness.controller.init();
  assert.equal(harness.shell.dataset.sceneState, 'idle');

  harness.controller.setCompareState('original');
  assert.equal(harness.shell.dataset.sceneState, 'exploring-original');
  harness.controller.setCompareState('translated');
  assert.equal(harness.shell.dataset.sceneState, 'exploring-translated');
  harness.controller.setCompareState('both');

  harness.email.value = 'local@example.test';
  harness.form.dispatch('input', harness.email);
  assert.equal(harness.shell.dataset.sceneState, 'idle', 'one field is not filled');
  harness.password.value = 'a-value';
  harness.form.dispatch('input', harness.password);
  assert.equal(harness.shell.dataset.sceneState, 'filled');

  // Functional states always win over exploration and filling.
  harness.controller.reflectAuthState('auth_submitting');
  assert.equal(harness.shell.dataset.sceneState, 'submitting');
  harness.controller.reflectAuthState('invalid_credentials');
  assert.equal(harness.shell.dataset.sceneState, 'error');
  harness.controller.reflectAuthState('idle');
  assert.equal(harness.shell.dataset.sceneState, 'filled');
});

test('filled never claims the credential is valid', () => {
  const harness = createSceneHarness();
  harness.controller.init();
  harness.email.value = 'x@y.test';
  harness.password.value = 'z';
  harness.form.dispatch('input', harness.email);
  harness.form.dispatch('input', harness.password);
  assert.equal(harness.shell.dataset.sceneState, 'filled');
  assert.equal(harness.emailField.dataset.valid, undefined);
  assert.equal(harness.shell.dataset.authenticated, undefined);
});

test('the central control cycles through the three readings', () => {
  const harness = createSceneHarness();
  harness.controller.init();
  harness.both.dispatch('click');
  assert.equal(harness.shell.dataset.compareState, 'original');
  harness.controller.cycleCompareState();
  assert.equal(harness.shell.dataset.compareState, 'translated');
  harness.controller.cycleCompareState();
  assert.equal(harness.shell.dataset.compareState, 'both');
  // A side control always selects its own reading directly.
  harness.translated.dispatch('click');
  assert.equal(harness.shell.dataset.compareState, 'translated');
  assert.equal(harness.translated.getAttribute('aria-pressed'), 'true');
  assert.equal(harness.both.getAttribute('aria-pressed'), 'false');
});

test('pointer parallax runs only on a wide fine-pointer surface', () => {
  const harness = createSceneHarness({media: DESKTOP_POINTER});
  harness.controller.init();
  harness.runFrames();
  assert.equal(harness.shell.dataset.pointer, 'active');
  // Centred by default, so the scene never starts displaced.
  assert.equal(harness.shell.style.values['--glow-x'], '0.00px');
  assert.equal(harness.shell.style.values['--pointer-x'], '0.5000');
});

test('pointer parallax stays off for touch, narrow and reduced motion', () => {
  const touch = createSceneHarness({media: {'(min-width: 900px)': true}});
  touch.controller.init();
  assert.equal(touch.shell.dataset.pointer, 'disabled');

  const narrow = createSceneHarness({media: {'(pointer: fine)': true}});
  narrow.controller.init();
  assert.equal(narrow.shell.dataset.pointer, 'disabled');

  const reduced = createSceneHarness({
    media: {...DESKTOP_POINTER, '(prefers-reduced-motion: reduce)': true},
  });
  reduced.controller.init();
  assert.equal(reduced.shell.dataset.pointer, 'disabled');
  assert.equal(reduced.shell.dataset.reducedMotion, 'true');
});

test('the entrance runs on frames and never on a timer', () => {
  const harness = createSceneHarness();
  harness.controller.init();
  assert.equal(harness.shell.dataset.entered, undefined, 'not before the first paint');
  harness.runFrames();
  assert.equal(harness.shell.dataset.entered, '1');

  // Reduced motion skips straight to the finished state.
  const reduced = createSceneHarness({media: {'(prefers-reduced-motion: reduce)': true}});
  reduced.controller.init();
  assert.equal(reduced.shell.dataset.entered, '1');
});

test('the shell is classified by width and height', () => {
  const wide = createSceneHarness({media: {innerWidth: 1920, innerHeight: 1080}});
  wide.controller.init();
  assert.equal(wide.shell.dataset.density, 'spacious');
  assert.equal(wide.shell.dataset.viewportHeight, 'regular');

  const low = createSceneHarness({media: {innerWidth: 1366, innerHeight: 650}});
  low.controller.init();
  assert.equal(low.shell.dataset.density, 'regular');
  assert.equal(low.shell.dataset.viewportHeight, 'compact');

  const phone = createSceneHarness({media: {innerWidth: 390, innerHeight: 844}});
  phone.controller.init();
  assert.equal(phone.shell.dataset.density, 'compact');
});

test('classification still works without ResizeObserver', () => {
  const harness = createSceneHarness({
    resizeObserver: false, media: {innerWidth: 1366, innerHeight: 650},
  });
  harness.controller.init();
  assert.equal(harness.shell.dataset.viewportHeight, 'compact');
  assert.equal(harness.observed.length, 0);
});

test('the scene survives a browser without frames or inline styles', () => {
  const harness = createSceneHarness({frames: false, style: false, media: DESKTOP_POINTER});
  assert.equal(harness.controller.init(), true);
  assert.equal(harness.shell.dataset.entered, '1', 'entrance completes immediately');
  harness.shell.dispatch('pointermove');
  assert.equal(harness.shell.dataset.sceneState, 'idle');
});

test('destroy removes every listener it registered', () => {
  const harness = createSceneHarness({media: DESKTOP_POINTER});
  harness.controller.init();
  const attached = harness.form.listenerCount()
    + harness.shell.listenerCount()
    + harness.buttons.reduce((total, button) => total + button.listenerCount(), 0);
  assert.ok(attached > 0);

  harness.controller.destroy();
  assert.equal(harness.form.listenerCount(), 0);
  assert.equal(harness.shell.listenerCount(), 0);
  assert.equal(harness.buttons.reduce((total, b) => total + b.listenerCount(), 0), 0);
  assert.equal(harness.disconnectedCount(), 1, 'the observer is disconnected');
  assert.equal(harness.shell.dataset.presentationReady, undefined);
});

test('the module owns no authentication behaviour', async () => {
  const fs = await import('node:fs/promises');
  const source = await fs.readFile(
    new URL('./static/auth_presentation.js', import.meta.url), 'utf8');
  const code = source.replace(/\/\/[^\n]*|\/\*[\s\S]*?\*\//g, '');
  const forbidden = ['fetch(', 'XMLHttpRequest', 'localStorage', 'sessionStorage',
    'setTimeout', 'setInterval', 'MutationObserver', '.submit('];
  for (const term of forbidden) {
    assert.equal(code.includes(term), false, `must not use ${term}`);
  }
  assert.equal(/^\s*import\s/m.test(code), false, 'no external dependency');
});
