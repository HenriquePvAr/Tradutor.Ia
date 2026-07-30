// Presentation-only behavior for the authentication surface.
// It never submits credentials, validates authentication, or owns session state.
// Everything here reflects state that already exists; nothing here decides it.

const INTERACTION_STATES = new Set(['idle', 'form-focused', 'submitting', 'error']);
const COMPARE_STATES = new Set(['both', 'original', 'translated']);
// The scene state is the single visual reading of the screen. It is derived,
// never set from outside, so it cannot drift from the states it summarises.
const SCENE_STATES = new Set([
  'idle', 'exploring-original', 'exploring-translated',
  'form-focused', 'filled', 'submitting', 'error',
]);
const COMPARE_CYCLE = ['both', 'original', 'translated'];
// Pointer parallax is decoration: a few pixels, and only where a fine pointer
// makes it meaningful. Anything larger reads as the page slipping.
const GLOW_TRAVEL_PX = 8;
const POINTER_MIN_WIDTH = 900;

function query(root, selector) {
  return root?.querySelector?.(selector) || null;
}

function queryAll(root, selector) {
  return Array.from(root?.querySelectorAll?.(selector) || []);
}

function clamp01(value) {
  return value < 0 ? 0 : value > 1 ? 1 : value;
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
  const textFields = fields.filter((field) => field.type !== 'checkbox');

  let functionalState = 'idle';
  let pointerFrame = 0;
  let pointerEnabled = false;
  let resizeObserver = null;
  const teardown = [];

  function listen(target, type, handler, options) {
    if (!target?.addEventListener) return;
    target.addEventListener(type, handler, options);
    teardown.push(() => target.removeEventListener(type, handler, options));
  }

  function mediaQuery(queryText) {
    return windowRef.matchMedia?.(queryText) || null;
  }

  function prefersReducedMotion() {
    return Boolean(mediaQuery('(prefers-reduced-motion: reduce)')?.matches);
  }

  function formHasFocus() {
    return Boolean(form?.contains?.(root.activeElement));
  }

  // --- scene ---------------------------------------------------------------

  function anyFieldFilled() {
    return textFields.some((field) => String(field.value || '').length > 0);
  }

  function allFieldsFilled() {
    return textFields.length > 0
      && textFields.every((field) => String(field.value || '').length > 0);
  }

  function resolveSceneState() {
    if (functionalState === 'submitting' || functionalState === 'error') return functionalState;
    if (formHasFocus()) return 'form-focused';
    if (allFieldsFilled()) return 'filled';
    const compare = shell?.dataset.compareState;
    if (compare === 'original') return 'exploring-original';
    if (compare === 'translated') return 'exploring-translated';
    return 'idle';
  }

  function syncScene() {
    if (!shell) return;
    const next = resolveSceneState();
    if (SCENE_STATES.has(next)) shell.dataset.sceneState = next;
    shell.dataset.hasInput = String(anyFieldFilled());
  }

  function setInteractionState(next) {
    if (!shell || !INTERACTION_STATES.has(next)) return;
    shell.dataset.interactionState = next;
    syncScene();
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
      status.setAttribute('aria-live', announce ? 'polite' : 'off');
    }
    syncScene();
  }

  function cycleCompareState() {
    const current = shell?.dataset.compareState || 'both';
    const index = COMPARE_CYCLE.indexOf(current);
    setCompareState(COMPARE_CYCLE[(index + 1) % COMPARE_CYCLE.length]);
  }

  function updateFilled(field) {
    const container = field?.closest?.('.auth-login-field');
    if (!container) return;
    // Filled is not valid: it says a value exists, nothing about the credential.
    container.dataset.filled = String(Boolean(String(field.value || '').length));
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
      // Clears the visual emphasis only. The functional error belongs to the
      // form module, which owns its own message and focus contract.
      functionalState = 'idle';
      restoreVisualFocusState();
    } else {
      syncScene();
    }
  }

  // --- pointer -------------------------------------------------------------

  function writePointer(x, y) {
    pointerFrame = 0;
    // Custom properties are decoration: where inline styles are unavailable the
    // scene keeps working from its CSS defaults.
    const style = shell?.style;
    if (!style?.setProperty) return;
    style.setProperty('--pointer-x', x.toFixed(4));
    style.setProperty('--pointer-y', y.toFixed(4));
    style.setProperty('--glow-x', `${((x - 0.5) * 2 * GLOW_TRAVEL_PX).toFixed(2)}px`);
    style.setProperty('--glow-y', `${((y - 0.5) * 2 * GLOW_TRAVEL_PX).toFixed(2)}px`);
  }

  function handlePointerMove(event) {
    if (!pointerEnabled || !shell) return;
    // Read here, write inside the frame: layout reads and style writes never
    // interleave on the same tick.
    const rect = shell.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = clamp01((event.clientX - rect.left) / rect.width);
    const y = clamp01((event.clientY - rect.top) / rect.height);
    if (pointerFrame) windowRef.cancelAnimationFrame?.(pointerFrame);
    pointerFrame = windowRef.requestAnimationFrame?.(() => writePointer(x, y)) || 0;
    if (!pointerFrame) writePointer(x, y);
  }

  function resetPointer() {
    if (pointerFrame) {
      windowRef.cancelAnimationFrame?.(pointerFrame);
      pointerFrame = 0;
    }
    writePointer(0.5, 0.5);
  }

  function refreshPointerAvailability() {
    const fine = mediaQuery('(pointer: fine)');
    const wide = mediaQuery(`(min-width: ${POINTER_MIN_WIDTH}px)`);
    // No media support at all means no evidence of a fine pointer: stay off.
    const next = Boolean(fine?.matches) && Boolean(wide?.matches) && !prefersReducedMotion();
    if (next === pointerEnabled) return;
    pointerEnabled = next;
    if (shell) shell.dataset.pointer = next ? 'active' : 'disabled';
    if (!next) resetPointer();
  }

  // --- responsive classification ------------------------------------------

  function classify(width, height) {
    if (!shell) return;
    shell.dataset.density = width < 900 ? 'compact' : width < 1500 ? 'regular' : 'spacious';
    shell.dataset.viewportHeight = height < 700 ? 'compact' : 'regular';
  }

  function measureFromWindow() {
    classify(windowRef.innerWidth || 0, windowRef.innerHeight || 0);
  }

  function watchSize() {
    const Observer = windowRef.ResizeObserver;
    if (typeof Observer !== 'function') {
      // Keeps the classification working where ResizeObserver is absent.
      listen(windowRef, 'resize', measureFromWindow);
      measureFromWindow();
      return;
    }
    resizeObserver = new Observer((entries) => {
      const box = entries[0]?.contentRect;
      classify(box?.width || windowRef.innerWidth || 0, windowRef.innerHeight || 0);
    });
    resizeObserver.observe(shell);
    teardown.push(() => resizeObserver?.disconnect());
    measureFromWindow();
  }

  // --- entrance ------------------------------------------------------------

  function playEntrance() {
    if (!shell) return;
    const raf = windowRef.requestAnimationFrame;
    if (prefersReducedMotion() || typeof raf !== 'function') {
      shell.dataset.entered = '1';
      return;
    }
    // Two frames, no timer: the attribute lands after the first paint so the
    // CSS transition has a starting point, and nothing is ever held back.
    raf(() => raf(() => { shell.dataset.entered = '1'; }));
  }

  function init() {
    if (!shell || !form || shell.dataset.presentationReady === '1') return false;
    shell.dataset.presentationReady = '1';
    shell.dataset.interactionState = shell.dataset.interactionState || 'idle';
    shell.dataset.compareState = shell.dataset.compareState || 'both';
    shell.dataset.reducedMotion = String(prefersReducedMotion());
    for (const field of fields) updateFilled(field);
    setCompareState(shell.dataset.compareState, {announce: false});

    listen(form, 'focusin', handleFocusIn);
    listen(form, 'focusout', handleFocusOut);
    listen(form, 'input', handleInput);
    for (const button of compareButtons) {
      listen(button, 'click', () => {
        // The central control cycles through the three readings; the side
        // controls select their own directly.
        if (button.dataset.authCompare === 'both'
            && shell.dataset.compareState === 'both') cycleCompareState();
        else setCompareState(button.dataset.authCompare);
      });
    }

    listen(shell, 'pointermove', handlePointerMove, {passive: true});
    listen(shell, 'pointerleave', resetPointer);
    for (const queryText of ['(pointer: fine)', `(min-width: ${POINTER_MIN_WIDTH}px)`,
      '(prefers-reduced-motion: reduce)']) {
      const media = mediaQuery(queryText);
      if (media?.addEventListener) {
        const onChange = () => {
          shell.dataset.reducedMotion = String(prefersReducedMotion());
          refreshPointerAvailability();
        };
        media.addEventListener('change', onChange);
        teardown.push(() => media.removeEventListener('change', onChange));
      }
    }
    refreshPointerAvailability();
    resetPointer();
    watchSize();
    syncScene();
    playEntrance();
    return true;
  }

  function destroy() {
    while (teardown.length) {
      const off = teardown.pop();
      try { off(); } catch { /* a detached node is already unsubscribed */ }
    }
    if (pointerFrame) {
      windowRef.cancelAnimationFrame?.(pointerFrame);
      pointerFrame = 0;
    }
    resizeObserver = null;
    if (shell) delete shell.dataset.presentationReady;
  }

  return {
    init,
    destroy,
    reflectAuthState,
    setCompareState,
    cycleCompareState,
    setInteractionState,
    updateFilled,
    syncScene,
  };
}

export function initAuthPresentation(options) {
  const controller = createAuthPresentationController(options);
  controller.init();
  return controller;
}
