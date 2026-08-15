// Truthful connection indicator for the local Tradutor.Ia UI server.
//
// The "serviço conectado" badge used to be written once, at bootstrap, from
// `settings.nvidia_configured`: a configuration fact presented as a
// connectivity claim. Nothing revoked it, so a loaded page stayed green while
// the backend had no listener at all. CONNECTED here means one thing only:
// a probe of the local health endpoint succeeded recently.
//
// The state machine below is pure (injected fetch and clock) so it can be
// exercised without a browser; see test_service_health.mjs. The DOM wiring at
// the bottom only runs inside a real page.

export const CONNECTING = 'connecting';
export const CONNECTED = 'connected';
export const DISCONNECTED = 'disconnected';

export const PROBE_PATH = '/api/health';
export const PROBE_INTERVAL_MS = 5000;
export const PROBE_TIMEOUT_MS = 4000;
// One lost probe on loopback is usually a hiccup; two in a row is not.
export const FAILURE_THRESHOLD = 2;
// A hidden tab's timers are throttled or suspended, so probes stop happening
// rather than start failing. Past this age, silence is not consent.
export const STALE_AFTER_MS = PROBE_INTERVAL_MS * FAILURE_THRESHOLD + PROBE_TIMEOUT_MS;

export function createServiceHealth({fetchImpl, now = Date.now} = {}) {
  let doFetch = fetchImpl;
  let state = CONNECTING;
  let failures = 0;
  let lastOkAt = 0;
  let sequence = 0;

  function current() {
    if (state === CONNECTED && now() - lastOkAt > STALE_AFTER_MS) return DISCONNECTED;
    return state;
  }

  async function probe() {
    const mine = (sequence += 1);
    let healthy = false;
    try {
      const response = await doFetch(PROBE_PATH, {cache: 'no-store'});
      healthy = Boolean(response && response.ok);
    } catch (_) {
      healthy = false;
    }
    // A probe that started earlier must never overwrite a newer verdict.
    if (mine !== sequence) return current();
    if (healthy) {
      failures = 0;
      lastOkAt = now();
      state = CONNECTED;
    } else if ((failures += 1) >= FAILURE_THRESHOLD) {
      state = DISCONNECTED;
    }
    return current();
  }

  return {probe, state: current, setFetch(next) { doFetch = next; }};
}

// ---------------------------------------------------------------------------
// Presentation
// ---------------------------------------------------------------------------

const I18N_KEY = {
  [CONNECTING]: 'auth.service_checking',
  [CONNECTED]: 'auth.service_connected',
  [DISCONNECTED]: 'auth.service_disconnected',
};

const RAIL_LABEL = {
  [CONNECTING]: 'verificando ambiente…',
  [DISCONNECTED]: 'sem conexão com o serviço',
};

const BADGE_LABEL = {
  [CONNECTING]: 'verificando serviço…',
  [CONNECTED]: 'serviço conectado',
  [DISCONNECTED]: 'sem conexão com o serviço',
};

export function renderServiceHealth(doc, state) {
  const configured = doc.documentElement.dataset.tradutorApiConfigured === '1';
  doc.documentElement.dataset.tradutorServiceHealth = state;

  const rail = doc.querySelector('#railApiStatus');
  if (rail) {
    const label = state === CONNECTED
      ? (configured ? 'serviço conectado' : 'configuração necessária')
      : RAIL_LABEL[state];
    const dot = doc.createElement('span');
    dot.className = 'dot';
    rail.replaceChildren(dot, doc.createTextNode(label));
    rail.dataset.state = state;
    rail.parentElement?.classList.toggle(
      'is-error', state === DISCONNECTED || (state === CONNECTED && !configured));
  }

  const badge = doc.querySelector('.auth-login-status-dot');
  if (badge) {
    badge.dataset.state = state;
    const text = badge.querySelector('span');
    if (text) {
      const key = I18N_KEY[state];
      text.setAttribute('data-i18n', key);
      const translate = globalThis.TradutorI18n?.t;
      text.textContent = translate ? translate(key) : BADGE_LABEL[state];
    }
  }
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  const health = createServiceHealth({
    fetchImpl: (path, init) => fetch(path, {...init, signal: AbortSignal.timeout(PROBE_TIMEOUT_MS)}),
  });
  let last = null;
  const render = () => {
    const state = health.state();
    if (state === last) return;
    last = state;
    renderServiceHealth(document, state);
  };
  const check = () => { void health.probe().then(render); };

  render();
  check();
  // Not gated on visibility: browsers already throttle background timers, and a
  // throttled probe is still an observation. The stale rule covers the rest.
  window.setInterval(check, PROBE_INTERVAL_MS);
  // Returning to a throttled tab must re-verify instead of trusting the last
  // thing drawn before it was suspended.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { render(); check(); } });
  const redraw = () => { last = null; render(); };
  window.addEventListener('tradutor:language-changed', redraw);
  window.addEventListener('tradutor:api-configured-changed', redraw);
  window.__tradutorServiceHealth = health;
}
