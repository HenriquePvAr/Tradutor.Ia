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
