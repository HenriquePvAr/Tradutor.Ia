// Presentation-only behavior for the authentication surface.
// It never submits credentials, validates authentication, or owns session state.

const INTERACTION_STATES = new Set(['idle', 'form-focused', 'submitting', 'error']);
const COMPARE_STATES = new Set(['both', 'original', 'translated']);

function query(root, selector) {
  return root?.querySelector?.(selector) || null;
}

function queryAll(root, selector) {
  return Array.from(root?.querySelectorAll?.(selector) || []);
}

export function createAuthPresentationController({
  root = document,
  windowRef = window,
} = {}) {
  const shell = query(root, '#authSurface .auth-login-shell');
  const form = query(root, '#authForm');
  const status = query(root, '#authCompareStatus');
  const fields = queryAll(root, '#authForm input');
  const compareButtons = queryAll(root, '[data-auth-compare]');
  let functionalState = 'idle';

  function setInteractionState(next) {
    if (!shell || !INTERACTION_STATES.has(next)) return;
    shell.dataset.interactionState = next;
  }

  function setCompareState(next, {announce = true} = {}) {
    if (!shell || !COMPARE_STATES.has(next)) return;
    shell.dataset.compareState = next;
    for (const button of compareButtons) {
      button.setAttribute('aria-pressed', String(button.dataset.authCompare === next));
    }
    if (status) {
      status.textContent = next === 'original'
        ? 'Página original em destaque.'
        : next === 'translated'
          ? 'Página traduzida em destaque.'
          : 'Original e traduzido em equilíbrio.';
      if (!announce) status.setAttribute('aria-live', 'off');
      else status.setAttribute('aria-live', 'polite');
    }
  }

  function updateFilled(field) {
    const container = field?.closest?.('.auth-login-field');
    if (!container) return;
    container.dataset.filled = String(Boolean(String(field.value || '').length));
  }

  function formHasFocus() {
    return Boolean(form?.contains?.(root.activeElement));
  }

  function restoreVisualFocusState() {
    if (functionalState === 'submitting' || functionalState === 'error') return;
    setInteractionState(formHasFocus() ? 'form-focused' : 'idle');
  }

  function reflectAuthState(state) {
    functionalState = state === 'auth_submitting'
      ? 'submitting'
      : ['auth_error', 'auth_timeout', 'invalid_credentials'].includes(state)
        ? 'error'
        : 'idle';
    if (functionalState === 'submitting' || functionalState === 'error') {
      setInteractionState(functionalState);
    } else {
      restoreVisualFocusState();
    }
  }

  function handleFocusIn(event) {
    if (!form?.contains?.(event.target)) return;
    if (functionalState !== 'submitting') setInteractionState('form-focused');
  }

  function handleFocusOut() {
    windowRef.queueMicrotask(restoreVisualFocusState);
  }

  function handleInput(event) {
    if (!fields.includes(event.target)) return;
    updateFilled(event.target);
    if (functionalState === 'error') {
      functionalState = 'idle';
      restoreVisualFocusState();
    }
  }

  function init() {
    if (!shell || !form || shell.dataset.presentationReady === '1') return false;
    shell.dataset.presentationReady = '1';
    shell.dataset.interactionState = shell.dataset.interactionState || 'idle';
    shell.dataset.compareState = shell.dataset.compareState || 'both';
    const reducedMotion = Boolean(windowRef.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
    shell.dataset.reducedMotion = String(reducedMotion);
    for (const field of fields) updateFilled(field);
    setCompareState(shell.dataset.compareState, {announce: false});
    form.addEventListener('focusin', handleFocusIn);
    form.addEventListener('focusout', handleFocusOut);
    form.addEventListener('input', handleInput);
    for (const button of compareButtons) {
      button.addEventListener('click', () => setCompareState(button.dataset.authCompare));
    }
    return true;
  }

  return {
    init,
    reflectAuthState,
    setCompareState,
    setInteractionState,
    updateFilled,
  };
}

export function initAuthPresentation(options) {
  const controller = createAuthPresentationController(options);
  controller.init();
  return controller;
}
