// Wires the minimal auth UI (masthead status + full-page auth surface) to the Supabase SDK module.
// Loaded as an ES module so it can import the SDK. Keeps the current access token in a
// window field that the classic tradutor_ui.js reads to attach the Bearer header.
// Never logs a token, session, or password.
const $ = (sel) => document.querySelector(sel);

function setError(msg) {
  const el = $('#authError');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
  if (window.__tradutorAuthDiagnosticsEnabled === true && msg === 'Não foi possível verificar sua sessão.') {
    authTrace('SESSION_ERROR_BANNER_SET', {source: 'setError', reason: window.__tradutorAuthBootstrapFailureStage || 'UNKNOWN'});
  }
}

function setNote(msg) {
  const el = $('#authNote');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
}

function setShellState(state) {
  if (window.__tradutorVisualTestEnabled === true && document.documentElement.dataset.visualBootTest === '1') return;
  document.documentElement.dataset.shellState = state;
}

function showLoginSurface() {
  if (window.__tradutorVisualTestEnabled === true && document.documentElement.dataset.visualBootTest === '1') return;
  const surface = $('#authSurface');
  if (surface) surface.hidden = false;
}

function hideLoginSurface() {
  const surface = $('#authSurface');
  if (surface) surface.hidden = true;
  setError(''); setNote('');
}

function dismissBootSurface() {
  if (window.__tradutorVisualTestEnabled === true && document.documentElement.dataset.visualBootTest === '1') return;
  $('#boot')?.classList.add('hide');
}

function authShellStateFor(state) {
  if (state === 'authenticated') return 'authenticated';
  if (state === 'auth_loading') return 'booting';
  if (state === 'auth_submitting') return 'authenticating';
  return 'unauthenticated';
}

function setSubmitLabel(button, label) {
  if (!button) return;
  const text = button.querySelector('.btn-text');
  if (text) text.textContent = label;
  else button.textContent = label;
}

function setSubmitLoading(button, loading, label) {
  if (!button) return;
  const isLoading = Boolean(loading);
  const form = button.form || button.closest('form');
  button.classList.toggle('loading', isLoading);
  button.disabled = isLoading;
  button.setAttribute('aria-disabled', String(isLoading));
  form?.setAttribute('aria-busy', String(isLoading));
  setSubmitLabel(button, label);
}

// Credential controls are locked only while the explicit login/signup request
// is in flight.  A failed request must always return the form to an editable
// state so the user can correct credentials without reloading the app.
function setAuthCredentialFieldsBusy(busy) {
  const shouldLock = Boolean(busy) && (mode === 'login' || mode === 'signup');
  for (const id of ['authEmail', 'authPassword', 'authConfirm', 'authName']) {
    const input = document.getElementById(id);
    if (!input) continue;
    input.disabled = shouldLock;
    input.readOnly = false;
    input.setAttribute('aria-disabled', String(shouldLock));
  }
  const form = $('#authForm');
  if (form && !shouldLock) form.setAttribute('aria-busy', 'false');
  if (!shouldLock) {
    const email = $('#authEmail');
    const password = $('#authPassword');
    const emailValid = Boolean(email && email.value.trim() && email.checkValidity());
    if (password && emailValid) {
      password.focus({preventScroll: true});
    } else if (email) {
      email.focus({preventScroll: true});
    }
  }
}

function recoverLoginFormAfterFailure(reason = 'request_failed') {
  const email = $('#authEmail');
  const password = $('#authPassword');
  setAuthCredentialFieldsBusy(false);
  // Never retain a rejected credential in the DOM; keep the email so it can
  // be corrected only when necessary and immediately retry.
  if (password) password.value = '';
  authTrace('LOGIN_FORM_RECOVERED', {
    reason,
    email_editable: Boolean(email && !email.disabled && !email.readOnly),
    password_editable: Boolean(password && !password.disabled && !password.readOnly),
    password_cleared: true,
  });
}

let mode = 'login';
let recoveryView = 'form'; // form | sent | password | success | invalid
let recoveryErrorKind = '';
let recoveryFixtureMode = '';
let recoveryCooldownTimer = 0;
const RECOVERY_COOLDOWN_SECONDS = 60;
const RECOVERY_COOLDOWN_KEY = 'yomu_recovery_last_request_at';
const RECOVERY_PENDING_KEY = 'yomu_recovery_pending_at';
// localStorage is shared by tabs, unlike sessionStorage.  Keep only a short-lived
// timestamp so a recovery request can be handed off when the email opens a new tab.
const RECOVERY_DURABLE_KEY = 'yomu_recovery_pending_at_durable';
const DESKTOP_HANDOFF_KEY = 'yomu_desktop_recovery_handoff';
const RECOVERY_INTENT_TTL_MS = 10 * 60 * 1000;
let isPasswordRecoveryFlow = false;
let recoveryUpdateInProgress = false;
let recoveryUpdateSucceeded = false;
let recoveryInvalidIntent = false;
const AUTH_MODE_PARAM = 'auth_mode';
try {
  const intent = sessionStorage.getItem('yomu_recovery_intent') === '1'
    || localStorage.getItem('yomu_recovery_intent') === '1';
  const pendingAt = Number(sessionStorage.getItem(RECOVERY_PENDING_KEY)
    || localStorage.getItem(RECOVERY_DURABLE_KEY) || 0);
  isPasswordRecoveryFlow = intent && (!pendingAt || Date.now() - pendingAt < RECOVERY_INTENT_TTL_MS);
  if (intent && !isPasswordRecoveryFlow) {
    sessionStorage.removeItem('yomu_recovery_intent');
    localStorage.removeItem('yomu_recovery_intent');
    localStorage.removeItem(RECOVERY_DURABLE_KEY);
  }
} catch (_) { /* optional */ }
try { recoveryInvalidIntent = sessionStorage.getItem('yomu_recovery_invalid') === '1'; } catch (_) { /* optional */ }
// The callback uses a fixed auth_mode handoff. Treat it as a recovery hint,
// while still requiring the session marker/TTL before routing to the password
// form in the normal runtime.
if (new URLSearchParams(window.location.search || '').get(AUTH_MODE_PARAM) === 'recovery_password') {
  isPasswordRecoveryFlow = true;
}
let authApi = null;
let authHeartbeatTimer = 0;
let authHeartbeatBusy = false;
let loginAttemptCounter = 0;
// Non-zero only while THIS tab's own signIn() call is in flight. Distinguishes the
// narrow "late SDK event from our own setSession() persistence race" (see
// renderSession below) from a genuine SIGNED_OUT - same-tab logout button click or
// another tab signing out - which must clear the header immediately instead of
// being deferred behind a stale bearer re-check.
let ownLoginAttemptId = 0;
const AUTH_BOOTSTRAP_TIMEOUT_MS = 10000;
const AUTH_LOGIN_TIMEOUT_MS = 20000;
const AUTH_HANDLER_ID = 'auth_ui:canonical-submit-v3';
let canonicalAuthSubmitImpl = null;
let authInitInFlight = null;
let authProviderImportPromise = null;
// Explicit login submissions always take precedence over any session that may
// still be present from a recovery flow.  Recovery sessions are finalized
// after a successful password update, but these guards also prevent a stale
// SDK event from opening the app before credentials are verified.
let explicitLoginSubmissionInProgress = false;
let requireExplicitLogin = false;
let desktopHandoffPollTimer = 0;
let desktopHandoffBusy = false;
const desktopHandoffState = {
  flowType: 'PASSWORD_RECOVERY', pending: false, startedAt: 0,
  polling: false, handoffId: '', lastStatus: 'NONE', consumed: false,
  lastError: '', lastErrorStatus: 0,
};
function publishDesktopHandoffState() {
  // DEV-only, sanitized observability: never expose the capability itself.
  window.__tradutorDesktopHandoffState = {
    pending: desktopHandoffState.pending,
    flow: desktopHandoffState.pending ? desktopHandoffState.flowType : 'NONE',
    ageSeconds: desktopHandoffState.startedAt ? Math.max(0, Math.floor((Date.now() - desktopHandoffState.startedAt) / 1000)) : 0,
    polling: desktopHandoffState.polling,
    lastStatus: desktopHandoffState.lastStatus,
    consumed: desktopHandoffState.consumed,
    idPresent: Boolean(desktopHandoffState.handoffId),
    error: desktopHandoffState.lastError,
    errorStatus: desktopHandoffState.lastErrorStatus,
  };
  if (new URLSearchParams(window.location.search || '').get('desktop') === '1' || window.__yomuDesktopRuntime === true) {
    const snapshot = window.__tradutorDesktopHandoffState;
    void fetch('/api/auth/desktop/client-state', {
      method: 'POST', cache: 'no-store', headers: {'content-type': 'application/json'},
      body: JSON.stringify({...snapshot, authMode: mode, sessionState: window.__tradutorAuthState || 'LOADING'}),
    }).catch(() => {});
  }
}
window.__tradutorAuthScriptExecuted = true;
window.__tradutorAuthBuild = `auth_ui:${new URL(import.meta.url).searchParams.get('v') || 'unversioned'}`;
window.__tradutorAuthBuildId = window.__tradutorAuthBuild;
// The shell must not infer "visitor" while the backend session is still being
// resolved.  The backend response is authoritative for local-session and Supabase
// providers alike; the SDK session only supplies the bearer when applicable.
window.__tradutorAuthState = 'auth_loading';
window.__tradutorCommunityUserId = '';
window.__tradutorAuthTrace = Array.isArray(window.__tradutorAuthTrace)
  ? window.__tradutorAuthTrace : [];
let authDiagnosticSequence = 0;

function authTrace(event, fields = {}) {
  const safe = {event: String(event || ''), at: new Date().toISOString(), seq: ++authDiagnosticSequence};
  for (const key of ['status', 'code', 'name', 'message', 'authenticated', 'source', 'token_present', 'token_length', 'elapsed_ms', 'expires_at', 'reason', 'meta', 'destination', 'auth_event', 'recovery_intent_present', 'recovery_intent_valid', 'session_present_before_login', 'app_open_reason', 'build_id', 'caller', 'window_role', 'session_fingerprint', 'session_present', 'user_present', 'request_trace_id', 'generation']) {
    if (fields[key] !== undefined) safe[key] = fields[key];
  }
  window.__tradutorAuthTrace.push(safe);
  try {
    document.documentElement.dataset.tradutorAuthLastEvent = safe.event;
    if (safe.code) document.documentElement.dataset.tradutorAuthLastCode = String(safe.code).slice(0, 80);
    if (safe.status !== undefined) document.documentElement.dataset.tradutorAuthLastStatus = String(safe.status);
  } catch (_) { /* diagnostics never affect auth */ }
  if (window.__tradutorAuthTrace.length > 40) window.__tradutorAuthTrace.shift();
  if (window.__tradutorAuthDiagnosticsEnabled === true) {
    void fetch('/api/internal/auth-diagnostics', {
      method: 'POST', cache: 'no-store', headers: {'content-type': 'application/json'},
      body: JSON.stringify(safe),
    }).catch(() => {});
  }
}

authTrace('AUTH_UI_BUILD_LOADED', {build_id: window.__tradutorAuthBuildId});

if (window.__tradutorAuthDiagnosticsEnabled === true) {
  document.addEventListener('click', (event) => {
    const button = event.target?.closest?.('#authSubmit, #authOpenBtn, #authRecoveryLink, [data-authmode-link="login"]');
    if (!button) return;
    const form = button.closest('form');
    authTrace('LOGIN_DOM_CLICK', {source: 'document_capture', authenticated: false});
  }, true);
  document.addEventListener('submit', (event) => {
    const form = event.target?.closest?.('#authForm');
    if (form) authTrace('LOGIN_FORM_SUBMIT_EVENT', {source: 'document_capture'});
  }, true);
  window.addEventListener('beforeunload', () => authTrace('PAGE_BEFORE_UNLOAD', {source: 'window'}));
  window.addEventListener('pagehide', () => authTrace('PAGE_HIDE', {source: 'window'}));
  window.addEventListener('pageshow', () => authTrace('PAGE_SHOW', {source: 'window'}));
  document.addEventListener('DOMContentLoaded', () => authTrace('DOM_CONTENT_LOADED', {source: 'document'}), {once: true});
}

// Intercept Auth form submission at capture time so a transient bootstrap
// failure or a rerendered form can never fall through to native navigation.
// The business handler remains responsible for validation and sign-in.
document.addEventListener('submit', (event) => {
  const form = event.target?.closest?.('#authForm');
  if (!form) return;
  event.preventDefault();
  authTrace('LOGIN_SUBMIT_DEFAULT_PREVENTED', {source: 'auth_capture'});
  const handler = window.__tradutorCanonicalAuthSubmit;
  if (typeof handler === 'function' && !event.__tradutorCanonicalDispatched) {
    event.__tradutorCanonicalDispatched = true;
    void handler(event);
  }
}, true);

// Keep the submit path alive even when the asynchronous provider bootstrap
// fails.  The concrete implementation is installed later, but this stable
// wrapper is available immediately after the module is evaluated and can
// retry the bootstrap on an explicit user action.
window.__tradutorCanonicalAuthSubmit = async (event) => {
  event?.preventDefault?.();
  if (canonicalAuthSubmitImpl) return canonicalAuthSubmitImpl(event);
  authTrace('LOGIN_HANDLER_ENTERED', {source: AUTH_HANDLER_ID, reason: 'bootstrap_retry'});
  authTrace('PROVIDER_RETRY_STARTED', {source: AUTH_HANDLER_ID});
  try {
    if (authInitInFlight) await authInitInFlight;
    if (!canonicalAuthSubmitImpl) {
      authInitInFlight = init();
      await authInitInFlight;
    }
    if (canonicalAuthSubmitImpl) return canonicalAuthSubmitImpl(event);
    authTrace('AUTH_PROVIDER_READY_FAILED', {code: 'bootstrap_unavailable'});
    setError('Não foi possível conectar ao serviço de autenticação. Tente novamente.');
  } catch (error) {
    authTrace('AUTH_PROVIDER_READY_FAILED', {
      name: String(error?.name || 'AuthError').slice(0, 80),
      code: String(error?.code || 'bootstrap_failed').slice(0, 80),
      status: Number(error?.status || 0),
    });
    setError('Não foi possível conectar ao serviço de autenticação. Tente novamente.');
  }
};

window.__tradutorAuthTraceEvent = authTrace;

function recoveryIntentSnapshot() {
  let marker = false;
  let pendingAt = 0;
  try {
    marker = sessionStorage.getItem('yomu_recovery_intent') === '1'
      || localStorage.getItem('yomu_recovery_intent') === '1';
    pendingAt = Number(sessionStorage.getItem(RECOVERY_PENDING_KEY)
      || localStorage.getItem(RECOVERY_DURABLE_KEY) || 0);
  } catch (_) { /* optional */ }
  const valid = marker && (!pendingAt || Date.now() - pendingAt < RECOVERY_INTENT_TTL_MS);
  const authMode = new URLSearchParams(window.location.search || '').get(AUTH_MODE_PARAM) || '';
  return {present: marker, valid, authMode};
}

// One deterministic destination resolver. Recovery intent always wins over a
// session event; SIGNED_IN/INITIAL_SESSION only mean the session is available
// for updateUser while the recovery flow is active.
function resolveAuthDestination({session = null, recoveryIntent = recoveryIntentSnapshot(), authMode = '', authEvent = '', canonicalConfirmed = false} = {}) {
  const validRecovery = Boolean(recoveryIntent?.valid)
    || (authMode === 'recovery_password' && Boolean(recoveryIntent?.present));
  const destination = validRecovery && (session || authEvent === 'PASSWORD_RECOVERY' || authEvent === 'INITIAL_SESSION' || authEvent === 'SIGNED_IN')
    ? 'RECOVERY_PASSWORD'
    : (session && canonicalConfirmed) ? 'AUTHENTICATED_APP' : 'LOGIN';
  authTrace('auth_destination_resolved', {
    destination,
    auth_event: authEvent || 'NONE',
    recovery_intent_present: Boolean(recoveryIntent?.present),
    recovery_intent_valid: validRecovery,
    authenticated: Boolean(session && canonicalConfirmed),
    reason: destination === 'RECOVERY_PASSWORD' ? 'recovery_priority' : (session && canonicalConfirmed) ? 'canonical_session' : session ? 'canonical_pending' : 'no_session',
  });
  return destination;
}

// Development-only durable recovery diagnostics.  Keep this deliberately
// narrow: no email, password, tokens, headers, or serialized Error objects.
const AUTH_RECOVERY_DIAGNOSTIC_KEY = 'tradutor_auth_password_recovery_last_error';
function sanitizeRecoveryMessage(value) {
  return String(value ?? '')
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[email]')
    .replace(/https?:\/\/[^\s]+/gi, '[url]')
    .slice(0, 240);
}
function persistRecoveryDiagnostic(error, outcome = 'error') {
  try {
    const record = {
      timestamp: new Date().toISOString(),
      operation: 'password_recovery_request',
      name: error?.name ?? (outcome === 'success' ? null : 'AuthError'),
      status: error?.status ?? (outcome === 'success' ? 200 : null),
      code: error?.code ?? null,
      message: error ? sanitizeRecoveryMessage(error.message) : (outcome === 'success' ? null : 'recovery_failed'),
    };
    localStorage.setItem(AUTH_RECOVERY_DIAGNOSTIC_KEY, JSON.stringify(record));
    window.__tradutorAuthRecoveryDiagnostic = record;
  } catch (_) { /* diagnostics must never affect auth */ }
}
function clearRecoveryDiagnostic() {
  try {
    localStorage.removeItem(AUTH_RECOVERY_DIAGNOSTIC_KEY);
    window.__tradutorAuthRecoveryDiagnostic = null;
  } catch (_) { /* diagnostics must never affect auth */ }
}
window.__tradutorAuthRecoveryDiagnosticKey = AUTH_RECOVERY_DIAGNOSTIC_KEY;
let authPresentation = null;
const presentationVisualTestDisabled = (
  window.__tradutorVisualTestEnabled === true
  && ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname)
  && new URLSearchParams(window.location.search).get('visual_auth_without_presentation') === '1'
);
if (presentationVisualTestDisabled) {
  authTrace('auth_presentation_unavailable', {code: 'visual_test_disabled'});
} else {
  void import('./auth_presentation.js')
    .then(({initAuthPresentation}) => {
      authPresentation = initAuthPresentation({root: document, windowRef: window});
      authPresentation.reflectAuthState(window.__tradutorAuthState);
    })
    .catch(() => {
      authTrace('auth_presentation_unavailable', {code: 'optional_module_unavailable'});
    });
}

function setAuthState(state, userId = '') {
  window.__tradutorAuthState = state;
  window.__tradutorCommunityUserId = state === 'authenticated' ? String(userId || '') : '';
  window.__tradutorAuthStore = {
    status: state,
    authenticated: state === 'authenticated',
    user_id: state === 'authenticated' ? String(userId || '') : '',
  };
  setShellState(authShellStateFor(state));
  authPresentation?.reflectAuthState(state);
  authTrace('auth_state_changed', {status: state, authenticated: state === 'authenticated'});
}

// The control-plane bootstrap can confirm the same current session after the
// explicit login confirmation raced/temporarily failed.  Reconcile that
// authoritative state back into the auth shell exactly once; otherwise the
// login error surface remains visible even though the app is authenticated.
let lateAuthShellGeneration = 0;
window.addEventListener('tradutor-control-plane-updated', event => {
  const snapshot = event?.detail?.state || {};
  if (snapshot.authenticated !== true) return;
  const generation = Number(window.__yomuAuthGeneration || 0);
  authTrace('AUTH_UI_RECONCILE_RECEIVED', {
    authenticated: true, generation, reason: 'control_plane_authenticated',
  });
  if (window.__tradutorAuthState === 'authenticated' && lateAuthShellGeneration === generation) {
    authTrace('AUTH_UI_RECONCILE_IGNORED', {authenticated: true, generation, reason: 'already_authenticated'});
    return;
  }
  if (generation && generation < lateAuthShellGeneration) {
    authTrace('AUTH_UI_RECONCILE_IGNORED', {authenticated: true, generation, reason: 'stale_generation'});
    return;
  }
  lateAuthShellGeneration = generation || lateAuthShellGeneration + 1;
  window.__tradutorCommunityAuthenticated = true;
  setAuthState('authenticated', snapshot.user_id || window.__tradutorCommunityUserId || '');
  authTrace('AUTH_UI_RECONCILE_APPLY', {authenticated: true, generation: lateAuthShellGeneration, reason: 'late_authoritative_auth'});
  renderAuthShell('authenticated');
  authTrace('AUTH_UI_RECONCILE_RESULT', {authenticated: true, generation: lateAuthShellGeneration, reason: 'shell_authenticated'});
});

function startAuthHeartbeat() {
  if (authHeartbeatTimer) return;
  authHeartbeatTimer = window.setInterval(async () => {
    if (authHeartbeatBusy || window.__tradutorAuthState !== 'authenticated' || !authApi) return;
    authHeartbeatBusy = true;
    try {
      const token = await authApi.currentAccessToken();
      const canonical = await syncBackendSession(token, {caller: 'auth-ui-heartbeat'});
      if (!canonical?.authenticated) authTrace('auth_heartbeat_lost', {authenticated: false});
    } catch (_) {
      authTrace('auth_heartbeat_error', {code: 'refresh_failed'});
    } finally { authHeartbeatBusy = false; }
  }, 60000);
}

function renderAuthShell(state, message = '') {
  const area = $('#authArea');
  const status = $('#authStatus');
  const openBtn = $('#authOpenBtn');
  const logoutBtn = $('#authLogoutBtn');
  if (!area || !status || !openBtn || !logoutBtn) return;
  area.hidden = false;
  // Keep the verified bearer during the authenticated shell transition.  Community
  // requests run immediately after login and must see the current provider token.
  if (state !== 'authenticated') window.__tradutorAccessToken = '';
  openBtn.hidden = state === 'auth_loading' || state === 'authenticated';
  logoutBtn.hidden = state !== 'authenticated';
  if (state === 'auth_loading') status.textContent = 'Verificando sessão…';
  else if (state === 'authenticated') status.textContent = '';
  else if (state === 'auth_error') status.textContent = message || 'Não foi possível verificar sua sessão.';
  else status.textContent = 'visitante';
  if (state === 'authenticated') {
    dismissBootSurface();
    hideLoginSurface();
    setShellState('authenticated');
  } else if (state === 'auth_loading') {
    setShellState('booting');
  } else if (state === 'auth_submitting') {
    dismissBootSurface();
    showLoginSurface();
    setShellState('authenticating');
  } else {
    dismissBootSurface();
    showLoginSurface();
    setShellState('unauthenticated');
  }
}

function withTimeout(promise, timeoutMs = AUTH_BOOTSTRAP_TIMEOUT_MS) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('auth_bootstrap_timeout')), timeoutMs)),
  ]);
}

async function syncBackendSession(accessToken = '', {signal, caller = 'unknown'} = {}) {
  const headers = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  try {
    if (accessToken) authTrace('local_session_exchange_started', {source: 'bearer'});
    authTrace('canonical_session_request', {source: accessToken ? 'bearer' : 'cookie'});
    headers['X-Yomu-Auth-Caller'] = String(caller || 'unknown').slice(0, 60);
    headers['X-Yomu-Window-Role'] = 'auth';
    const fingerprint = accessToken ? await fingerprintSession(accessToken) : '';
    const requestTraceId = (window.crypto?.randomUUID?.() || `auth-${Date.now()}-${authDiagnosticSequence}`)
      .replace(/[^A-Za-z0-9_.-]/g, '').slice(0, 80);
    headers['X-Yomu-Request-Trace'] = requestTraceId;
    if (fingerprint) headers['X-Yomu-Session-Fingerprint'] = fingerprint;
    authTrace('CANONICAL_CALL_DISPATCHED', {
      caller: String(caller || 'unknown').slice(0, 60),
      window_role: 'auth',
      token_present: Boolean(accessToken),
      session_present: Boolean(accessToken),
      session_fingerprint: fingerprint,
      request_trace_id: requestTraceId,
    });
    const response = await withTimeout(fetch('/api/community/auth/session', {
      headers, credentials: 'same-origin', cache: 'no-store', signal,
    }));
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    if (response.ok && payload.authenticated) {
      const recoverySnapshot = recoveryIntentSnapshot();
      const destination = resolveAuthDestination({
        session: {authenticated: true},
        recoveryIntent: recoverySnapshot,
        authMode: recoverySnapshot.authMode,
        authEvent: 'BACKEND_SESSION_SYNC',
        canonicalConfirmed: true,
      });
      if (destination === 'RECOVERY_PASSWORD') {
        isPasswordRecoveryFlow = true;
        showLoginSurface();
        showRecoveryView('password');
        setAuthState('unauthenticated');
        renderAuthShell('unauthenticated');
        authTrace('backend_session_sync_completed', {authenticated: true, destination: 'RECOVERY_PASSWORD', reason: 'recovery_priority'});
        return payload;
      }
      if (accessToken) authTrace('local_session_exchange_finished', {status: response.status, authenticated: true, source: 'bearer'});
      setAuthState('authenticated', payload.user_id);
      window.__tradutorCommunityAuthenticated = true;
      authTrace('canonical_session_confirmed', {authenticated: true});
      authTrace('backend_session_sync_completed', {authenticated: true, destination: 'AUTHENTICATED_APP'});
      renderAuthShell('authenticated');
      window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
        detail: {authenticated: true, user_id: String(payload.user_id || ''), state: 'authenticated'},
      }));
      return payload;
    }
    if (accessToken) authTrace('local_session_exchange_finished', {status: response.status, authenticated: false, source: 'bearer'});
    if (response.status === 401) setAuthState('session_expired');
    else if (response.status === 403) setAuthState('auth_error');
    else setAuthState('unauthenticated');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell(window.__tradutorAuthState, payload.message || '');
    authTrace('canonical_session_rejected', {status: response.status, code: payload.reason_code || 'authentication_required', authenticated: false});
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: window.__tradutorAuthState},
    }));
    return payload;
  } catch (_) {
    setAuthState('auth_error');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell('auth_error', 'Não foi possível verificar sua sessão.');
    authTrace('canonical_session_error', {code: 'network_or_timeout'});
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'auth_error'},
    }));
    return null;
  }
}

async function establishCanonicalSession(signal) {
  let accessToken = '';
  const tokenStarted = Date.now();
  try { accessToken = await authApi.currentAccessToken(); } catch (err) {
    authTrace('AUTH_ACCESS_TOKEN_MISSING', {code: err?.code || 'current_access_token_threw', elapsed_ms: Date.now() - tokenStarted});
  }
  const token = String(accessToken || '');
  let meta = null;
  if (token) {
    try {
      const parts = token.split('.');
      const d = (s) => JSON.parse(atob(String(s || '').replace(/-/g, '+').replace(/_/g, '/')));
      const h = d(parts[0]); const p = d(parts[1]);
      const nowS = Math.floor(Date.now() / 1000);
      meta = {alg: h.alg, kid: String(h.kid || '').slice(0, 12), iss: p.iss, aud: p.aud, exp_in: (p.exp || 0) - nowS, sub: Boolean(p.sub)};
    } catch (_) { meta = {decode_error: true}; }
  }
  authTrace(token ? 'AUTH_ACCESS_TOKEN_PRESENT' : 'AUTH_ACCESS_TOKEN_MISSING', {
    token_present: Boolean(token), token_length: token.length, elapsed_ms: Date.now() - tokenStarted, meta,
  });
  const payload = await syncBackendSession(accessToken, {signal, caller: 'auth-ui-explicit-login'});
  if (payload?.authenticated) return payload;
  const error = new Error('session_not_established');
  error.code = 'session_not_established';
  throw error;
}

async function completeLoginFlow(email, password, signal) {
  window.__tradutorLoginStage = 'supabase_sign_in';
  authTrace('LOGIN_HANDLER_STARTED', {source: AUTH_HANDLER_ID});
    authTrace('SIGN_IN_WITH_PASSWORD_STARTED', {source: 'supabase_auth'});
    authTrace('SIGN_IN_STARTED', {source: 'supabase_auth'});
  try {
    await authApi.signIn(email, password, {signal});
  } catch (err) {
    authTrace('SIGN_IN_WITH_PASSWORD_FAILURE', {
      status: Number(err?.status || 0),
      code: String(err?.code || err?.name || 'sign_in_failed').slice(0, 80),
      name: String(err?.name || 'AuthError').slice(0, 80),
      message: sanitizeRecoveryMessage(err?.message || 'sign_in_failed'),
      source: 'supabase_auth',
    });
    authTrace('SIGN_IN_FAILED', {status: Number(err?.status || 0), code: String(err?.code || err?.name || 'sign_in_failed').slice(0, 80), source: 'supabase_auth'});
    throw err;
  }
    authTrace('SIGN_IN_WITH_PASSWORD_SUCCESS', {source: 'supabase_auth'});
    authTrace('SIGN_IN_SUCCESS', {source: 'supabase_auth'});
  authTrace('login_request_finished', {source: AUTH_HANDLER_ID});
  authTrace('canonical_session_refresh_started', {source: AUTH_HANDLER_ID});
  window.__tradutorLoginStage = 'canonical_session';
  const canonical = await establishCanonicalSession(signal);
  authTrace('canonical_session_refresh_finished', {authenticated: true, source: AUTH_HANDLER_ID});
  if (!canonical?.authenticated) {
    const error = new Error('session_not_established');
    error.code = 'session_not_established';
    throw error;
  }
  authTrace('auth_state_updated', {authenticated: true, source: AUTH_HANDLER_ID});
  window.__tradutorAccessToken = await authApi.currentAccessToken();
  explicitLoginSubmissionInProgress = false;
  requireExplicitLogin = false;
  renderAuthShell('authenticated');
  authTrace('APP_OPENED', {app_open_reason: 'explicit_login'});
  return canonical;
}

async function fingerprintSession(value) {
  try {
    if (!value || !window.crypto?.subtle) return '';
    const bytes = new TextEncoder().encode(String(value));
    const digest = await window.crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, '0')).join('').slice(0, 12);
  } catch (_) { return ''; }
}

function setMode(next) {
  mode = ['login', 'signup', 'forgot'].includes(next) ? next : 'login';
  if (mode !== 'forgot') recoveryUpdateSucceeded = false;
  if (mode !== 'forgot') recoveryView = 'form';
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.authmode === next);
  });
  const submit = $('#authSubmit');
  const pw = $('#authPassword');
  const title = $('#authTitle');
  const lead = document.querySelector('.auth-login-lead');
  const confirm = $('#authConfirmField');
  const nameField = $('#authNameField');
  const recovery = $('#authRecoveryLink');
  const remember = document.querySelector('.auth-login-remember');
  const footer = document.querySelector('.auth-login-footer');
  const forgotNote = $('#authForgotNote');
  const recoveryPassword = $('#authRecoveryPasswordField');
  const recoveryPasswordConfirm = $('#authRecoveryPasswordConfirmField');
  const recoveryPasswordInput = $('#authRecoveryPassword');
  const recoveryPasswordConfirmInput = $('#authRecoveryPasswordConfirm');
  const emailField = $('#authEmailField');
  const heroTitle = $('#authHeroTitle');
  const heroSubtitle = $('#authHeroSubtitle');
  const footerPrompt = $('#authFooterPrompt');
  const footerLink = $('#authFooterLink');
  const terminal = $('#authRecoveryTerminal');
  const terminalText = $('#authRecoveryTerminalText');
  const terminalAction = $('#authRecoveryTerminalAction');
  const isForgot = mode === 'forgot';
  const recoveryPasswordView = isForgot && recoveryView === 'password';
  const recoverySentView = isForgot && recoveryView === 'sent';
  const recoverySuccessView = isForgot && recoveryView === 'success';
  const recoveryInvalidView = isForgot && recoveryView === 'invalid';
  const recoveryErrorTitle = recoveryErrorKind === 'expired' ? 'Link de recuperação expirado'
    : recoveryErrorKind === 'invalid' ? 'Link de recuperação inválido'
    : recoveryErrorKind === 'same_password' ? 'Escolha uma senha diferente'
    : recoveryErrorKind === 'weak_password' ? 'Senha muito fraca' : '';
  if (title) title.textContent = mode === 'signup' ? 'Criar conta' : recoveryErrorTitle || (recoveryPasswordView ? 'Crie uma nova senha' : recoverySuccessView ? 'Senha alterada' : recoveryInvalidView ? 'Link de recuperação inválido' : isForgot ? 'Esqueci minha senha' : 'Entrar');
  if (lead) lead.textContent = mode === 'signup'
    ? 'Crie sua conta para começar a ler no seu idioma.'
    : recoveryErrorKind === 'expired' ? 'Esse link de recuperação expirou. Solicite um novo link para continuar.'
    : recoveryErrorKind === 'invalid' ? 'Esse link não é mais válido. Solicite um novo link de recuperação para continuar.'
    : recoveryErrorKind === 'same_password' ? 'A nova senha não pode ser igual à senha atual. Digite uma senha diferente para continuar.'
    : recoveryErrorKind === 'weak_password' ? 'Use pelo menos 8 caracteres e escolha uma senha mais segura.'
    : recoveryPasswordView ? 'Escolha uma nova senha para continuar.'
    : recoverySuccessView ? 'Sua senha foi atualizada com segurança.'
    : recoverySentView ? 'Confira sua caixa de entrada para continuar.'
    : recoveryInvalidView ? 'Este link de recuperação expirou ou não é mais válido.'
    : isForgot ? 'Informe seu e-mail para recuperar o acesso.'
    : 'Acesse sua conta para continuar.';
  if (submit) setSubmitLabel(submit, mode === 'signup' ? 'Criar conta' : recoveryPasswordView ? 'Salvar nova senha' : recoveryInvalidView ? 'Solicitar novo link' : isForgot ? (recoverySentView ? 'Enviar novo link' : 'Enviar link de recuperação') : 'Entrar');
  // The email-sent view disables this shared button for its cooldown. Re-enable
  // it when recovery_password is rendered so a valid handoff can be submitted.
  if (submit && recoveryPasswordView && submit.dataset.busy !== '1') {
    submit.disabled = false;
    submit.setAttribute('aria-disabled', 'false');
  }
  if (heroTitle) heroTitle.innerHTML = mode === 'login'
    ? 'Seu tradutor pessoal de<br><em>manhwas e mangás.</em>'
    : mode === 'forgot' ? 'Seus projetos seguros,<br>seu <em>acesso garantido.</em>'
    : 'Seu tradutor pessoal de<br><em>manhwas e mangás.</em>';
  if (heroSubtitle) heroSubtitle.innerHTML = mode === 'login'
    ? 'Traduza seus capítulos com uma experiência pensada para preservar o sentido,<br>o contexto e o estilo de cada obra.'
    : mode === 'forgot' ? 'Recupere o acesso à sua conta e continue suas traduções de onde parou.'
    : 'Traduza capítulos com mais praticidade, acompanhe seu progresso<br>e organize suas leituras em um só lugar.';
  const benefits = mode === 'forgot'
    ? [['Tradução fiel à obra', 'Preserve o sentido e o contexto dos diálogos.'], ['Capítulos no seu idioma', 'Leia suas histórias com uma tradução natural.'], ['Feito para suas histórias', 'Uma experiência pensada para manhwas e mangás.']]
    : [['Tradução fiel à obra', 'Preserve o sentido e o contexto dos diálogos.'], ['Capítulos no seu idioma', 'Leia suas histórias com uma tradução natural.'], ['Feito para suas histórias', 'Uma experiência pensada para manhwas e mangás.']];
  benefits.forEach(([heading, copy], index) => {
    const titleEl = document.querySelector(`[data-benefit-title="${index + 1}"]`);
    const copyEl = document.querySelector(`[data-benefit-copy="${index + 1}"]`);
    if (titleEl) titleEl.textContent = heading;
    if (copyEl) copyEl.textContent = copy;
  });
  if (pw) { pw.autocomplete = mode === 'signup' ? 'new-password' : 'current-password'; pw.required = !isForgot; pw.closest('.auth-login-field')?.toggleAttribute('hidden', isForgot || recoveryPasswordView || recoverySuccessView || recoverySentView); }
  confirm?.toggleAttribute('hidden', mode !== 'signup');
  if (confirm && pw?.closest('.auth-login-field')) pw.closest('.auth-login-field').after(confirm);
  nameField?.toggleAttribute('hidden', mode !== 'signup');
  if (confirm) confirm.querySelector('input')?.toggleAttribute('required', mode === 'signup');
  recoveryPassword?.toggleAttribute('hidden', !recoveryPasswordView);
  recoveryPasswordConfirm?.toggleAttribute('hidden', !recoveryPasswordView);
  if (recoveryPasswordInput) recoveryPasswordInput.required = recoveryPasswordView;
  if (recoveryPasswordConfirmInput) recoveryPasswordConfirmInput.required = recoveryPasswordView;
  recovery?.toggleAttribute('hidden', mode !== 'login');
  if (recovery) recovery.textContent = 'Esqueci minha senha';
  // Remember-me applies only to sign-in; hiding it on signup keeps the
  // four-field registration card compact and aligned with the reference.
  remember?.toggleAttribute('hidden', isForgot || mode === 'signup');
  // Keep the invalid-link state actionable: it exposes both "Solicitar novo
  // link" (terminal action) and a secondary "Voltar para entrar" action.
  footer?.toggleAttribute('hidden', recoverySuccessView);
  if (footerPrompt && footerLink) {
    footerPrompt.hidden = mode !== 'login' || recoveryPasswordView || recoveryInvalidView;
    footerPrompt.textContent = mode === 'signup' ? 'Já tem uma conta?' : mode === 'forgot' ? 'Lembrou sua senha?' : 'Ainda não tem conta?';
    footerLink.textContent = mode === 'signup' ? 'Já tenho conta' : mode === 'forgot' ? 'Voltar para entrar' : 'Criar conta';
    footerLink.dataset.authmodeLink = mode === 'login' ? 'signup' : 'login';
    if (recoveryPasswordView || recoveryInvalidView) {
      footerPrompt.removeAttribute('data-i18n');
      footerLink.removeAttribute('data-i18n');
      footerLink.textContent = 'Voltar para entrar';
      footerLink.dataset.authmodeLink = 'login';
    }
  }
  forgotNote?.toggleAttribute('hidden', !isForgot || recoveryView === 'form');
  if (forgotNote && recoverySentView) forgotNote.textContent = 'Se o endereço estiver autorizado, enviamos instruções para continuar a recuperação.';
  if (forgotNote && recoveryPasswordView) forgotNote.textContent = 'Use pelo menos 8 caracteres e não reutilize uma senha comprometida.';
  if (forgotNote && recoverySuccessView) forgotNote.textContent = 'Você já pode entrar com sua nova senha.';
  if (forgotNote && recoveryView === 'form') forgotNote.textContent = 'Se o endereço estiver autorizado, enviaremos instruções para continuar a recuperação.';
  const form = $('#authForm');
  form?.toggleAttribute('hidden', recoverySuccessView || recoveryInvalidView);
  document.querySelector('.auth-login-separator')?.toggleAttribute('hidden', recoverySuccessView || recoveryInvalidView);
  if (terminal) terminal.hidden = !(recoverySuccessView || recoveryInvalidView);
  if (terminalText) terminalText.textContent = recoverySuccessView
    ? 'Sua nova senha foi salva com sucesso.'
    : recoveryInvalidView ? (recoveryErrorKind === 'expired' ? 'Esse link de recuperação expirou. Solicite um novo link para continuar.' : 'Esse link não é mais válido. Solicite um novo link de recuperação para continuar.') : '';
  if (terminalAction) terminalAction.textContent = recoveryInvalidView ? 'Solicitar novo link' : 'Entrar';
  if (footerPrompt && footerLink && (recoverySentView || recoverySuccessView)) {
    footerPrompt.textContent = recoverySuccessView ? 'Pronto para entrar?' : 'Não recebeu o e-mail?';
    footerLink.textContent = 'Voltar para entrar';
    footerLink.dataset.authmodeLink = 'login';
  }
  emailField?.toggleAttribute('hidden', recoveryPasswordView || recoverySuccessView || recoveryInvalidView);
  document.querySelector('.auth-login-badge')?.replaceChildren(document.createTextNode(
    recoveryPasswordView ? 'RECUPERAÇÃO DE ACESSO' : recoverySuccessView ? 'CONCLUÍDO' : recoveryInvalidView ? 'LINK INVÁLIDO' : isForgot ? 'PRONTO' : mode === 'signup' ? 'EM DESENVOLVIMENTO' : 'PRONTO'
  ));
  setError(''); setNote('');
  renderRecoveryCooldown();
}

function showRecoveryView(next) {
  recoveryView = ['form', 'sent', 'password', 'success', 'invalid'].includes(next) ? next : 'form';
  if (recoveryView !== 'invalid' && recoveryView !== 'password') recoveryErrorKind = '';
  if (recoveryView === 'password' && !['same_password', 'weak_password'].includes(recoveryErrorKind)) recoveryErrorKind = '';
  setMode('forgot');
}

function showRecoveryError(kind, message = '') {
  recoveryErrorKind = kind;
  if (kind === 'expired' || kind === 'invalid') recoveryView = 'invalid';
  else recoveryView = 'password';
  setMode('forgot');
  if (message) setError(message);
}

function bindRecoveryFixtureHandlers() {
  const form = $('#authForm');
  const submit = $('#authSubmit');
  const terminalAction = $('#authRecoveryTerminalAction');
  const toggle = (buttonId, inputId) => document.getElementById(buttonId)?.addEventListener('click', () => {
    const input = document.getElementById(inputId);
    const button = document.getElementById(buttonId);
    if (!input || !button) return;
    const shown = input.type === 'text';
    input.type = shown ? 'password' : 'text';
    button.setAttribute('aria-label', shown ? 'Mostrar senha' : 'Ocultar senha');
    button.setAttribute('aria-pressed', String(!shown));
  });
  toggle('authToggleRecoveryPassword', 'authRecoveryPassword');
  toggle('authToggleRecoveryPasswordConfirm', 'authRecoveryPasswordConfirm');
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    if (recoveryFixtureMode !== 'recovery_password') return;
    const nextPassword = $('#authRecoveryPassword')?.value || '';
    const confirmation = $('#authRecoveryPasswordConfirm')?.value || '';
    if (nextPassword.length < 8) { showRecoveryError('weak_password'); setError('Use pelo menos 8 caracteres e escolha uma senha mais segura.'); return; }
    if (nextPassword !== confirmation) { showRecoveryError('mismatch'); setError('As senhas não coincidem. Repita a mesma senha nos dois campos para continuar.'); return; }
    setError('');
    setSubmitLoading(submit, true, 'Salvando…');
    window.setTimeout(() => {
      setSubmitLoading(submit, false, 'Salvar nova senha');
      showRecoveryView('success');
    }, 180);
  });
  terminalAction?.addEventListener('click', () => {
    if (recoveryView === 'invalid') showRecoveryView('form');
    else handleRecoverySuccessEnter();
  });
}

function applyRecoveryFixture(state) {
  recoveryFixtureMode = state;
  showLoginSurface();
  if (state === 'recovery_password') showRecoveryView('password');
  else if (state === 'recovery_success') showRecoveryView('success');
  else if (state === 'recovery_invalid') showRecoveryView('invalid');
  bindRecoveryFixtureHandlers();
  setAuthState('unauthenticated');
  renderAuthShell('unauthenticated');
}

function recoveryCooldownRemaining() {
  try {
    const last = Number(sessionStorage.getItem(RECOVERY_COOLDOWN_KEY) || 0);
    return Math.max(0, RECOVERY_COOLDOWN_SECONDS - Math.floor((Date.now() - last) / 1000));
  } catch (_) { return 0; }
}
function renderRecoveryCooldown() {
  if (mode !== 'forgot' || recoveryView !== 'sent') return;
  const submit = $('#authSubmit');
  const left = recoveryCooldownRemaining();
  if (submit) {
    submit.disabled = left > 0;
    submit.setAttribute('aria-disabled', String(left > 0));
    setSubmitLabel(submit, left > 0 ? `Enviar novo link em 00:${String(left).padStart(2, '0')}` : 'Enviar novo link');
  }
  if (left > 0) {
    if (!recoveryCooldownTimer) recoveryCooldownTimer = window.setTimeout(() => { recoveryCooldownTimer = 0; renderRecoveryCooldown(); }, 1000);
  }
}
function startRecoveryCooldown() {
  try { sessionStorage.setItem(RECOVERY_COOLDOWN_KEY, String(Date.now())); } catch (_) { /* optional */ }
  renderRecoveryCooldown();
}

// Apply the canonical login presentation immediately.  Local-session mode can
// return from initialization before provider handlers are bound; without this
// early reflection the static shell (signup copy/badge) briefly leaks into the
// login screen and produces an internally inconsistent first render.
setMode('login');

function clearAuthCredentialFields() {
  const email = $('#authEmail');
  const password = $('#authPassword');
  const toggle = $('#authTogglePassword');
  if (email) email.value = '';
  if (password) {
    password.value = '';
    password.type = 'password';
  }
  toggle?.classList.remove('shown');
  toggle?.setAttribute('aria-label', 'Mostrar senha');
  toggle?.setAttribute('aria-pressed', 'false');
}

function renderSession(session, authEvent = '') {
  if (authEvent === 'INITIAL_SESSION') {
    authTrace('SDK_INITIAL_SESSION_RECEIVED', {
      session_present: Boolean(session),
      access_token_present: Boolean(session?.access_token),
      user_present: Boolean(session?.user),
      auth_event: authEvent,
    });
  }
  const area = $('#authArea');
  const status = $('#authStatus');
  const openBtn = $('#authOpenBtn');
  const logoutBtn = $('#authLogoutBtn');
  if (!area) return;
  if (recoveryUpdateInProgress || recoveryUpdateSucceeded) {
    authTrace('auth_event_held_during_recovery_finalization', {auth_event: authEvent || 'NONE', reason: recoveryUpdateSucceeded ? 'recovery_update_succeeded' : 'recovery_update_in_progress'});
    return;
  }
  // Once recovery has completed, a residual SDK session must never open the
  // application from the login screen.  Only a successful explicit
  // signInWithPassword may clear this gate.
  if (requireExplicitLogin && !explicitLoginSubmissionInProgress && authEvent !== 'PASSWORD_RECOVERY') {
    authTrace('login_session_gate_blocked', {
      auth_event: authEvent || 'NONE',
      session_present_before_login: Boolean(session),
      reason: 'recovery_session_requires_explicit_login',
    });
    window.__tradutorAccessToken = '';
    showLoginSurface();
    setAuthState('unauthenticated');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell('unauthenticated');
    return;
  }
  area.hidden = false;
  const recoverySnapshot = recoveryIntentSnapshot();
  const authMode = new URLSearchParams(window.location.search || '').get(AUTH_MODE_PARAM) || recoverySnapshot.authMode;
  // Supabase can briefly emit a null session while refreshing tokens.  Do not
  // turn that transient event into a logout; the canonical backend session is
  // authoritative.  An explicit SIGNED_OUT event still clears the UI.
  if (authEvent === 'PASSWORD_RECOVERY') {
    isPasswordRecoveryFlow = true;
    try { sessionStorage.setItem('yomu_recovery_intent', '1'); } catch (_) { /* optional */ }
    showLoginSurface();
    showRecoveryView('password');
    setAuthState('unauthenticated');
    renderAuthShell('unauthenticated');
    return;
  }
  if (recoveryInvalidIntent) {
    recoveryInvalidIntent = false;
    try { sessionStorage.removeItem('yomu_recovery_invalid'); } catch (_) { /* optional */ }
    showLoginSurface();
    showRecoveryView('invalid');
    setAuthState('unauthenticated');
    renderAuthShell('unauthenticated');
    return;
  }
  const destination = resolveAuthDestination({session, recoveryIntent: recoverySnapshot, authMode, authEvent});
  if (destination === 'RECOVERY_PASSWORD') {
    isPasswordRecoveryFlow = true;
    showLoginSurface();
    showRecoveryView('password');
    setAuthState('unauthenticated');
    renderAuthShell('unauthenticated');
    return;
  }
  if (isPasswordRecoveryFlow && (authEvent === 'INITIAL_SESSION' || authEvent === 'INITIAL_SESSION_RESTORED' || session)) {
    showLoginSurface();
    showRecoveryView('password');
    setAuthState('unauthenticated');
    renderAuthShell('unauthenticated');
    return;
  }
  // A clean install normally emits INITIAL_SESSION with no persisted session.
  // That is the expected logged-out state, not a verification failure.  Avoid
  // probing the backend just to rediscover the absence of a session; reserve
  // the red error state for an actual provider/network failure.
  if (!session && (authEvent === 'INITIAL_SESSION' || authEvent === 'INITIAL_SESSION_RESTORED')) {
    setAuthState('unauthenticated');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell('unauthenticated');
    return;
  }
  if (!session && authEvent && authEvent !== 'SIGNED_OUT') {
    // Provider events without a session are provisional/no-session signals.
    // Never probe the canonical endpoint anonymously: only a real bearer or
    // cookie-backed session may enter backend verification.
    authTrace('canonical_session_skipped_no_session', {
      auth_event: authEvent,
      reason: 'provider_session_absent',
    });
    setAuthState('unauthenticated');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell('unauthenticated');
    return;
  }
  if (!session && authEvent === 'SIGNED_OUT' && window.__tradutorAccessToken && ownLoginAttemptId) {
    // A late SDK event can report sign-out when setSession persistence timed out
    // during OUR OWN in-flight signIn() call; retain the verified in-memory bearer
    // until the canonical session rejects it. Scoped to an active login attempt so
    // a genuine SIGNED_OUT - same-tab logout or another tab signing out while this
    // tab sits idle authenticated - falls through to the immediate clear below
    // instead of being deferred behind a stale bearer re-check.
    authTrace('sdk_signout_deferred', {authenticated: true, source: 'memory_session'});
    void syncBackendSession(window.__tradutorAccessToken, {caller: 'auth-ui-sdk-signout-recovery'});
    return;
  }
  window.__tradutorAccessToken = session?.access_token || '';
  authTrace('sdk_session_changed', {authenticated: Boolean(session)});
  startAuthHeartbeat();
  setAuthState('auth_loading');
  const displayName = String(window.__tradutorDisplayName || '').trim();
  if (session) {
    // The public shell never falls back to the account email.  The local profile
    // bootstrap replaces the neutral label with the persisted display name.
    status.textContent = displayName || 'Usuário';
    openBtn.hidden = true;
    logoutBtn.hidden = false;
    if (explicitLoginSubmissionInProgress) {
      authTrace('canonical_session_deferred_to_explicit_login', {
        auth_event: authEvent || 'NONE',
        reason: 'explicit_login_in_flight',
      });
      return;
    }
  } else {
    window.__tradutorDisplayName = '';
    status.textContent = 'visitante';
    openBtn.hidden = false;
    logoutBtn.hidden = true;
  }
  void syncBackendSession(window.__tradutorAccessToken, {caller: 'auth-ui-sdk-session-change'});
}

function initRealProductPreview() {
  const carousel = document.getElementById('authProductCarousel');
  const track = carousel?.querySelector('.auth-product-real-slides');
  const dots = carousel?.querySelector('.auth-product-dots');
  if (!carousel || !track || !dots || track.dataset.ready === '1') return;
  const ids = String(carousel.dataset.previewSources || '').split(',').map((id) => id.trim()).filter(Boolean);
  const shellSource = document.querySelector('.shell');
  const slides = [];
  ids.forEach((id, index) => {
    if (!shellSource || !document.getElementById(id)) return;
    const slide = shellSource.cloneNode(true);
    slide.removeAttribute('id');
    slide.className = `shell auth-product-real-slide${index === 0 ? ' is-active' : ''}`;
    slide.querySelectorAll('[hidden]').forEach((el) => { el.hidden = false; });
    slide.querySelectorAll('.panel-view').forEach((view) => {
      view.classList.toggle('active', view.id === id);
      view.hidden = view.id !== id;
    });
    slide.querySelectorAll('.rail-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === id.replace('view-', '')));
    slide.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
    track.appendChild(slide);
    slides.push(slide);
    const dot = document.createElement('i');
    dot.setAttribute('role', 'button');
    dot.setAttribute('tabindex', '0');
    dot.setAttribute('aria-label', `Mostrar ${id.replace('view-', '')}`);
    dot.addEventListener('click', () => showSlide(index));
    dots.appendChild(dot);
  });
  if (!slides.length) return;
  const dotEls = [...dots.children];
  let current = 0;
  let timer = 0;
  const showSlide = (next) => {
    current = (next + slides.length) % slides.length;
    slides.forEach((slide, i) => slide.classList.toggle('is-active', i === current));
    dotEls.forEach((dot, i) => dot.classList.toggle('is-active', i === current));
  };
  const start = () => { if (!timer && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) timer = window.setInterval(() => showSlide(current + 1), 4000); };
  const stop = () => { if (timer) { window.clearInterval(timer); timer = 0; } };
  carousel.addEventListener('mouseenter', stop);
  carousel.addEventListener('mouseleave', start);
  track.dataset.ready = '1';
  showSlide(0);
  start();
}

function clearRecoveryContext({cleanUrl = true} = {}) {
  isPasswordRecoveryFlow = false;
  recoveryUpdateInProgress = false;
  recoveryUpdateSucceeded = false;
  recoveryInvalidIntent = false;
  recoveryView = 'form';
  requireExplicitLogin = false;
  try {
    sessionStorage.removeItem('yomu_recovery_intent');
    sessionStorage.removeItem(RECOVERY_PENDING_KEY);
    sessionStorage.removeItem(DESKTOP_HANDOFF_KEY);
    sessionStorage.removeItem('yomu_recovery_invalid');
    localStorage.removeItem('yomu_recovery_intent');
    localStorage.removeItem(RECOVERY_DURABLE_KEY);
  } catch (_) { /* optional */ }
  desktopHandoffState.pending = false;
  desktopHandoffState.polling = false;
  desktopHandoffState.handoffId = '';
  desktopHandoffState.lastStatus = 'NONE';
  desktopHandoffState.consumed = false;
  publishDesktopHandoffState();
  if (cleanUrl) {
    try {
      const url = new URL(window.location.href);
      url.searchParams.delete(AUTH_MODE_PARAM);
      url.searchParams.delete('recovery');
      url.searchParams.delete('code');
      url.searchParams.delete('type');
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    } catch (_) { /* URL cleanup must never block logout */ }
  }
}

function isDesktopRuntime() {
  return new URLSearchParams(window.location.search || '').get('desktop') === '1'
    || window.__yomuDesktopRuntime === true;
}

async function startDesktopRecoveryHandoff() {
  const response = await fetch('/api/auth/desktop/handoff/start', {
    method: 'POST', cache: 'no-store', headers: {'content-type': 'application/json'}, body: '{}',
  });
  if (!response.ok) throw new Error('desktop_handoff_start_failed');
  const payload = await response.json();
  const handoffId = String(payload?.handoff_id || '');
  if (!handoffId) throw new Error('desktop_handoff_id_missing');
  desktopHandoffState.pending = true;
  desktopHandoffState.startedAt = Date.now();
  desktopHandoffState.handoffId = handoffId;
    desktopHandoffState.lastStatus = 'WAITING';
    desktopHandoffState.consumed = false;
    desktopHandoffState.lastError = '';
    desktopHandoffState.lastErrorStatus = 0;
  publishDesktopHandoffState();
  try { sessionStorage.setItem(DESKTOP_HANDOFF_KEY, handoffId); } catch (_) { /* optional */ }
  authTrace('DESKTOP_RECOVERY_HANDOFF_CREATED', {source: 'desktop_handoff'});
  startDesktopRecoveryPolling();
  return handoffId;
}

async function pollDesktopRecoveryHandoff() {
  if (!isDesktopRuntime() || !authApi || desktopHandoffBusy) return;
  let handoffId = desktopHandoffState.handoffId;
  if (!handoffId) {
    try { handoffId = sessionStorage.getItem(DESKTOP_HANDOFF_KEY) || ''; } catch (_) { return; }
    if (handoffId) {
      desktopHandoffState.handoffId = handoffId;
      desktopHandoffState.pending = true;
      desktopHandoffState.startedAt ||= Date.now();
      desktopHandoffState.lastStatus = 'WAITING';
      publishDesktopHandoffState();
    }
  }
  if (!handoffId) return;
  desktopHandoffState.polling = true;
  publishDesktopHandoffState();
  desktopHandoffBusy = true;
  try {
    authTrace('DESKTOP_RECOVERY_HANDOFF_POLL', {source: 'desktop_handoff'});
    const statusResponse = await fetch(`/api/auth/desktop/handoff/${encodeURIComponent(handoffId)}/status`, {cache: 'no-store'});
    if (!statusResponse.ok) throw new Error('desktop_handoff_status_failed');
    const status = await statusResponse.json();
    if (!status?.ready) { desktopHandoffState.lastStatus = 'WAITING'; publishDesktopHandoffState(); return; }
    desktopHandoffState.lastStatus = 'READY'; publishDesktopHandoffState();
    authTrace('DESKTOP_RECOVERY_HANDOFF_READY', {source: 'desktop_handoff'});
    const consumeResponse = await fetch(`/api/auth/desktop/handoff/${encodeURIComponent(handoffId)}/consume`, {
      method: 'POST', cache: 'no-store', headers: {'content-type': 'application/json'}, body: '{}',
    });
    if (!consumeResponse.ok) throw new Error('desktop_handoff_consume_failed');
    const consumed = await consumeResponse.json();
    const code = String(consumed?.code || '');
    if (!code) throw new Error('desktop_handoff_code_missing');
    authTrace('DESKTOP_RECOVERY_HANDOFF_CONSUMED', {source: 'desktop_handoff'});
    try { sessionStorage.removeItem(DESKTOP_HANDOFF_KEY); } catch (_) { /* optional */ }
    desktopHandoffState.consumed = true;
    desktopHandoffState.pending = false;
    desktopHandoffState.lastStatus = 'CONSUMED';
    desktopHandoffState.polling = false;
    publishDesktopHandoffState();
    if (desktopHandoffPollTimer) { window.clearInterval(desktopHandoffPollTimer); desktopHandoffPollTimer = 0; }
    sessionStorage.setItem('yomu_recovery_intent', '1');
    if (typeof authApi.exchangeCodeForSession !== 'function') throw new Error('auth_exchange_unavailable');
    await withTimeout(authApi.exchangeCodeForSession(code), AUTH_LOGIN_TIMEOUT_MS);
    isPasswordRecoveryFlow = true;
    showLoginSurface();
    showRecoveryView('password');
    setAuthState('unauthenticated');
    renderAuthShell('unauthenticated');
    authTrace('DESKTOP_RECOVERY_HANDOFF_EXCHANGED', {source: 'desktop_handoff'});
  } catch (error) {
    desktopHandoffState.lastStatus = Number(error?.status || 0) === 404 ? 'EXPIRED' : 'ERROR';
    desktopHandoffState.lastError = String(error?.code || error?.name || 'handoff_failed').slice(0, 80);
    desktopHandoffState.lastErrorStatus = Number(error?.status || 0);
    publishDesktopHandoffState();
    authTrace('DESKTOP_RECOVERY_HANDOFF_FAILED', {
      status: Number(error?.status || 0),
      code: String(error?.code || error?.name || 'handoff_failed').slice(0, 80),
    });
  } finally {
    desktopHandoffBusy = false;
    if (desktopHandoffState.lastStatus === 'CONSUMED' || desktopHandoffState.lastStatus === 'EXPIRED') {
      desktopHandoffState.polling = false;
      if (desktopHandoffPollTimer) { window.clearInterval(desktopHandoffPollTimer); desktopHandoffPollTimer = 0; }
      publishDesktopHandoffState();
    }
  }
}

function startDesktopRecoveryPolling() {
  if (!isDesktopRuntime() || desktopHandoffPollTimer) return;
  void pollDesktopRecoveryHandoff();
  desktopHandoffPollTimer = window.setInterval(() => { void pollDesktopRecoveryHandoff(); }, 1000);
}

function handleRecoverySuccessEnter() {
  clearRecoveryContext({cleanUrl: true});
  // Even if an SDK event arrives late with a stale session, the user must
  // explicitly authenticate from the login form after leaving recovery.
  requireExplicitLogin = true;
  showLoginSurface();
  setMode('login');
  setAuthState('unauthenticated');
  window.__tradutorCommunityAuthenticated = false;
  renderAuthShell('unauthenticated');
  authTrace('recovery_success_exit_to_login', {destination: 'LOGIN'});
}

async function init() {
  window.__tradutorAuthInitStarted = true;
  initRealProductPreview();
  const visualParams = new URLSearchParams(window.location.search || '');
  const debugState = visualParams.get('auth_debug_state');
  if (['recovery_password', 'recovery_success', 'recovery_invalid'].includes(debugState)
      && ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname)) {
    applyRecoveryFixture(debugState);
    return;
  }
  const visualReducedMotion = window.__tradutorVisualTestEnabled === true
    && ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname)
    && visualParams.get('visual_auth_reduced_motion') === '1';
  if (visualReducedMotion) document.documentElement.dataset.visualReducedMotion = '1';
  // Dynamic import keeps the canonical state available even when a CDN/module
  // dependency is unavailable. A failed import is an explicit auth_error, never
  // an implicit visitor with stale identity from a previous document.
  setAuthState('auth_loading');
  renderAuthShell('auth_loading');
  let bootstrapStage = 'PROVIDER_IMPORT';
  try {
    // Bare specifier on purpose. social_api.js and social_community.js import the
    // same module statically as '/static/auth_provider.js'; a per-call cache key
    // here resolved to a *different* URL, so the browser kept two module instances
    // alive, each with its own provider promise and its own Supabase client. That
    // second client is what logged "Multiple GoTrueClient instances detected in the
    // same browser context". app_ui.py versions auth_ui.js itself by mtime, so a
    // restarted UI still picks up new code without splitting the module graph.
    if (!authProviderImportPromise) {
      authProviderImportPromise = withTimeout(import('/static/auth_provider.js'))
        .catch((error) => { authProviderImportPromise = null; throw error; });
    }
    authApi = await authProviderImportPromise;
    authTrace('AUTH_PROVIDER_IMPORT_SUCCESS', {source: AUTH_HANDLER_ID});
    bootstrapStage = 'AUTH_ENVIRONMENT';
    authTrace('AUTH_ENVIRONMENT_STARTED', {source: AUTH_HANDLER_ID});
    const authEnvironment = typeof authApi.authEnvironment === 'function'
      ? await withTimeout(authApi.authEnvironment())
      : {};
    authTrace('AUTH_ENVIRONMENT_SUCCESS', {source: AUTH_HANDLER_ID});
    if (authEnvironment?.local_test_environment === true) {
      document.body.dataset.authEnvironment = 'local_test';
      let notice = document.getElementById('authLocalTestNotice');
      if (!notice) {
        notice = document.createElement('div');
        notice.id = 'authLocalTestNotice';
        notice.className = 'auth-local-test-notice';
        notice.textContent = 'Ambiente local de testes';
        document.querySelector('.auth-login-card')?.prepend(notice);
      }
      document.querySelectorAll(
        '.auth-tab[data-authmode="signup"], [data-authmode-link="signup"]'
      ).forEach((control) => {
        control.hidden = true;
        control.dataset.authLocalSignupControl = 'disabled';
      });
    }
    // Hiding only the signup control left its question behind: "Ainda não tem
    // conta?" stayed on screen with nothing to answer it. The provider reports
    // whether signup exists, so the whole invitation follows that answer rather
    // than being hidden control by control.
    if (authEnvironment?.signup_enabled === false) {
      document.querySelectorAll('.auth-login-footer').forEach((footer) => {
        footer.hidden = true;
        footer.dataset.authSignupInvitation = 'unavailable';
      });
    }
  } catch (error) {
    const safeCode = String(error?.code || error?.name || 'bootstrap_failed').slice(0, 80);
    authTrace(`AUTH_${bootstrapStage}_FAILED`, {
      name: String(error?.name || 'AuthError').slice(0, 80),
      code: safeCode,
      status: Number(error?.status || 0),
      source: AUTH_HANDLER_ID,
    });
    window.__tradutorAuthBootstrapFailureStage = bootstrapStage;
    setAuthState('auth_error');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell('auth_error');
    setError('Não foi possível verificar sua sessão.');
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'auth_error'},
    }));
    return;
  }
  let client;
  try {
    authTrace('SUPABASE_CLIENT_GET_STARTED', {source: AUTH_HANDLER_ID});
    client = await withTimeout(authApi.getSupabaseClient());
    authTrace('SUPABASE_CLIENT_GET_SUCCESS', {source: AUTH_HANDLER_ID});
  } catch (error) {
    authTrace('SUPABASE_CLIENT_GET_FAILED', {
      name: String(error?.name || 'AuthError').slice(0, 80),
      code: String(error?.code || error?.name || 'client_failed').slice(0, 80),
      status: Number(error?.status || 0),
      source: AUTH_HANDLER_ID,
    });
    setAuthState('auth_error');
    renderAuthShell('auth_error');
    setError('Não foi possível verificar sua sessão.');
    return;
  }
  if (!client) {
    // Local-session provider: resolve the HttpOnly cookie through the backend.
    window.__tradutorAccessToken = '';
    setAuthState('auth_loading');
    await syncBackendSession('', {caller: 'auth-ui-explicit-refresh'});
    return;
  }
  startDesktopRecoveryPolling();
  window.__tradutorGetCanonicalAccessToken = authApi.getCanonicalAccessToken;
  if (window.__tradutorAuthHandlersBound) return;
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    tab.addEventListener('click', () => setMode(tab.dataset.authmode));
  });
  $('#authOpenBtn')?.addEventListener('click', () => {
    showLoginSurface();
    setShellState('unauthenticated');
  });
  document.querySelectorAll('[data-authmode-link]').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (recoveryView === 'success' && (link.dataset.authmodeLink || 'login') === 'login') {
        event.preventDefault();
        handleRecoverySuccessEnter();
        return;
      }
      setMode(link.dataset.authmodeLink || 'login');
    });
  });
  $('#authRecoveryLink')?.addEventListener('click', (event) => {
    event.preventDefault();
    setMode('forgot');
  });
  $('#authLogoutBtn')?.addEventListener('click', async () => {
    authTrace('LOGOUT_SUBMIT_CLICKED', {source: AUTH_HANDLER_ID});
    // Clear recovery context before the SDK's SIGNED_OUT event can resolve a
    // stale recovery query/marker back into recovery_password.
    clearRecoveryContext();
    try { await withTimeout(authApi.signOut(), AUTH_BOOTSTRAP_TIMEOUT_MS); } catch (_) { /* local cleanup below is authoritative */ }
    clearRecoveryContext();
    clearAuthCredentialFields();
    window.__tradutorAccessToken = '';
    window.__tradutorCommunityAuthenticated = false;
    setAuthState('unauthenticated');
    renderAuthShell('unauthenticated');
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'unauthenticated'},
    }));
  });
  async function handleAuthSubmit(event) {
    // Replaces the former direct `$('#authForm')?.addEventListener('submit'`
    // binding; the stable capture-phase delegate invokes this canonical handler.
    event.preventDefault();
    const submit = $('#authSubmit');
    if (!submit) return;
    if (submit.dataset.busy === '1') return;
    setError(''); setNote('');
    const email = $('#authEmail').value.trim();
    const password = $('#authPassword').value;
    if (mode === 'forgot') {
      if (recoveryView === 'password') {
        const nextPassword = $('#authRecoveryPassword')?.value || '';
        const confirmation = $('#authRecoveryPasswordConfirm')?.value || '';
        authTrace('PASSWORD_SAVE_CLICKED', {source: AUTH_HANDLER_ID});
        authTrace('PASSWORD_VALIDATION_STARTED', {source: AUTH_HANDLER_ID});
        if (nextPassword.length < 8) { authTrace('PASSWORD_VALIDATION_FAILED', {code: 'password_policy'}); showRecoveryError('weak_password'); setError('Use pelo menos 8 caracteres e escolha uma senha mais segura.'); return; }
        if (nextPassword !== confirmation) { authTrace('PASSWORD_VALIDATION_FAILED', {code: 'password_mismatch'}); showRecoveryError('mismatch'); setError('As senhas não coincidem. Repita a mesma senha nos dois campos para continuar.'); return; }
        authTrace('PASSWORD_VALIDATION_PASSED', {source: AUTH_HANDLER_ID});
        recoveryUpdateInProgress = true;
        authTrace('UPDATE_PASSWORD_HANDLER_STARTED', {source: AUTH_HANDLER_ID});
        submit.dataset.busy = '1';
        setSubmitLoading(submit, true, 'Salvando…');
        try {
          const recoveryToken = await withTimeout(authApi.currentAccessToken(), AUTH_BOOTSTRAP_TIMEOUT_MS);
          if (!recoveryToken) {
            const sessionError = new Error('recovery_session_missing');
            sessionError.code = 'recovery_session_missing';
            sessionError.status = 401;
            throw sessionError;
          }
          authTrace('UPDATE_USER_REQUEST_STARTED', {source: 'supabase_auth'});
          await withTimeout(authApi.updatePassword(nextPassword), AUTH_LOGIN_TIMEOUT_MS);
          authTrace('UPDATE_USER_REQUEST_SUCCESS', {status: 200, source: 'supabase_auth'});
          recoveryUpdateSucceeded = true;
          requireExplicitLogin = true;
          authTrace('RECOVERY_SUCCESS_MODE_SELECTED', {destination: 'RECOVERY_SUCCESS'});
          if ($('#authRecoveryPassword')) $('#authRecoveryPassword').value = '';
          if ($('#authRecoveryPasswordConfirm')) $('#authRecoveryPasswordConfirm').value = '';
          isPasswordRecoveryFlow = false;
          authTrace('RECOVERY_INTENT_CLEANUP_STARTED', {source: AUTH_HANDLER_ID});
          try {
            sessionStorage.removeItem('yomu_recovery_intent'); sessionStorage.removeItem(RECOVERY_PENDING_KEY);
            localStorage.removeItem('yomu_recovery_intent'); localStorage.removeItem(RECOVERY_DURABLE_KEY);
          } catch (_) { /* optional */ }
          authTrace('RECOVERY_INTENT_CLEANUP_COMPLETED', {source: AUTH_HANDLER_ID});
          showRecoveryView('success');
          setNote('Sua senha foi atualizada com sucesso.');
          authTrace('RECOVERY_SUCCESS_RENDERED', {destination: 'RECOVERY_SUCCESS'});
          // A recovery session is not a normal login.  Finalize it only after
          // updateUser() has succeeded and the success state is rendered.
          authTrace('RECOVERY_SIGNOUT_STARTED', {source: AUTH_HANDLER_ID});
          try {
            await withTimeout(authApi.signOut(), AUTH_BOOTSTRAP_TIMEOUT_MS);
            authTrace('RECOVERY_SIGNOUT_SUCCESS', {source: AUTH_HANDLER_ID});
          } catch (signOutError) {
            authTrace('RECOVERY_SIGNOUT_FAILURE', {
              status: Number(signOutError?.status || 0),
              code: String(signOutError?.code || signOutError?.name || 'sign_out_failed').slice(0, 80),
              source: AUTH_HANDLER_ID,
            });
          }
        } catch (err) {
          recoveryUpdateSucceeded = false;
          const status = Number(err?.status || 0);
          const errorCode = String(err?.code || err?.name || 'update_failed').slice(0, 80);
          const errorMessage = sanitizeRecoveryMessage(err?.message || 'update_failed');
          authTrace('UPDATE_USER_REQUEST_FAILED', {status, code: errorCode, name: String(err?.name || 'AuthError').slice(0, 80), message: errorMessage});
          const expired = status === 401 || /session_missing|expired|invalid.*(token|session)|jwt/i.test(`${errorCode} ${errorMessage}`);
          const samePassword = /same[_ -]?password|password.*same|new password.*current|identical/i.test(`${errorCode} ${errorMessage}`);
          if (samePassword) {
            showRecoveryError('same_password', 'A nova senha não pode ser igual à senha atual. Digite uma senha diferente para continuar.');
            authTrace('UPDATE_USER_REJECTED_SAME_PASSWORD', {status, code: errorCode});
          } else if (expired) {
            clearRecoveryContext({cleanUrl: true});
            showRecoveryError(errorCode.includes('expired') ? 'expired' : 'invalid');
            setNote(errorCode.includes('expired') ? 'Esse link de recuperação expirou. Solicite um novo link para continuar.' : 'Esse link não é mais válido. Solicite um novo link de recuperação para continuar.');
            authTrace('RECOVERY_EXPIRED_OR_INVALID', {status, code: errorCode});
          } else {
            setError(status === 429 ? 'Muitas tentativas. Aguarde um pouco e tente novamente.' : 'Não foi possível atualizar sua senha agora. Tente novamente em instantes. Se o problema continuar, solicite um novo link de recuperação.');
          }
        } finally {
          recoveryUpdateInProgress = false;
          submit.dataset.busy = '';
          setSubmitLoading(submit, false, 'Salvar nova senha');
        }
        return;
      }
      if (!email || !$('#authEmail')?.checkValidity()) { setError('Informe um e-mail válido.'); return; }
      submit.dataset.busy = '1';
      setSubmitLoading(submit, true, 'Enviando…');
      try {
        // Carry an explicit, non-sensitive recovery hint in the redirect URL.
        // This survives a new-tab email handoff even when browser storage is
        // partitioned; the callback still performs the normal PKCE exchange.
        let redirectTo = `${window.location.origin}/auth/callback?recovery=1`;
        if (isDesktopRuntime()) {
          const handoffId = await startDesktopRecoveryHandoff();
          redirectTo += `&desktop=1&handoff=${encodeURIComponent(handoffId)}`;
        }
        // Record the cross-tab handoff before the remote request.  This marker
        // contains no account data or secret and lets a newly opened callback
        // recognize the recovery flow even if the originating tab is paused.
        const requestedAt = String(Date.now());
        sessionStorage.setItem(RECOVERY_PENDING_KEY, requestedAt);
        localStorage.setItem(RECOVERY_DURABLE_KEY, requestedAt);
        await withTimeout(authApi.resetPasswordForEmail(email, redirectTo), AUTH_LOGIN_TIMEOUT_MS);
        clearRecoveryDiagnostic();
        persistRecoveryDiagnostic(null, 'success');
        authTrace('password_recovery_request_finished', {status: 200, source: 'supabase_auth'});
        // Deliberately identical for existing and unknown addresses.
        showRecoveryView('sent');
        try {
          const now = String(Date.now());
          sessionStorage.setItem(RECOVERY_PENDING_KEY, now);
          localStorage.setItem(RECOVERY_DURABLE_KEY, now);
        } catch (_) { /* optional */ }
        startRecoveryCooldown();
        setNote('Se o endereço estiver autorizado, enviamos instruções para continuar a recuperação.');
      } catch (err) {
        const status = Number(err?.status || 0);
        persistRecoveryDiagnostic(err);
        authTrace('password_recovery_request_failed', {
          status,
          code: String(err?.code || err?.name || 'recovery_failed').slice(0, 80),
          source: 'supabase_auth',
        });
        setError(status === 429 ? 'Muitas solicitações. Aguarde um pouco e tente novamente.' : 'Não foi possível iniciar a recuperação agora. Tente novamente mais tarde.');
      } finally {
        submit.dataset.busy = '';
        setSubmitLoading(submit, false, 'Enviar link de recuperação');
      }
      return;
    }
    const attemptId = ++loginAttemptCounter;
    const controller = new AbortController();
    window.__tradutorActiveLoginController = controller;
    const timeoutId = setTimeout(() => controller.abort(), AUTH_LOGIN_TIMEOUT_MS);
    // Keep a UI-level escape hatch independent of the promise returned by a
    // provider SDK.  A misbehaving thenable must never leave the button locked.
    const watchdogId = setTimeout(() => {
      if (attemptId !== loginAttemptCounter || submit.dataset.busy !== '1') return;
      controller.abort();
      const message = 'O login demorou para responder. Tente novamente.';
      setAuthState('auth_timeout');
      window.__tradutorCommunityAuthenticated = false;
      renderAuthShell('auth_error', message);
      setError(message);
      recoverLoginFormAfterFailure('timeout');
      authTrace('login_watchdog_timeout', {code: 'auth_timeout'});
      submit.dataset.busy = '';
      setSubmitLoading(submit, false, mode === 'signup' ? 'Criar conta' : 'Entrar no painel');
    }, AUTH_LOGIN_TIMEOUT_MS + 250);
    submit.dataset.busy = '1';
    setSubmitLoading(submit, true, 'Entrando…');
    setAuthCredentialFieldsBusy(true);
    setAuthState('auth_submitting');
    explicitLoginSubmissionInProgress = mode !== 'signup';
    authTrace('LOGIN_HANDLER_ENTERED', {source: AUTH_HANDLER_ID});
    authTrace('LOGIN_SUBMIT_CLICKED', {source: AUTH_HANDLER_ID, session_present_before_login: Boolean(window.__tradutorAccessToken)});
    authTrace('login_submit_received', {source: AUTH_HANDLER_ID});
    authTrace('login_request_started', {source: AUTH_HANDLER_ID});
    try {
      if (mode === 'signup') {
        const { needsConfirmation } = await withTimeout(authApi.signUp(email, password), AUTH_LOGIN_TIMEOUT_MS);
        if (needsConfirmation) {
          setNote('Conta criada. Confirme pelo e-mail e depois entre.');
          setMode('login');
        } else {
          hideLoginSurface();
        }
      } else {
        ownLoginAttemptId = attemptId;
        try {
          await withTimeout(completeLoginFlow(email, password, controller.signal), AUTH_LOGIN_TIMEOUT_MS);
        } catch (signInError) {
          controller.abort();
          if (signInError?.message === 'auth_bootstrap_timeout') {
            signInError.code = `${window.__tradutorLoginStage || 'supabase_sign_in'}_timeout`;
            authTrace('sign_in_timeout', {code: signInError.code});
          }
          throw signInError;
        }
        hideLoginSurface();
        if (typeof window.__tradutorToast === 'function') window.__tradutorToast('Login realizado.', 'ok');
        authTrace('auth_surface_hidden', {authenticated: true, source: AUTH_HANDLER_ID});
        authTrace('login_completed', {authenticated: true});
      }
    } catch (err) {
      if (attemptId !== loginAttemptCounter) return;
      // A stale confirmation failure must not put the UI back into auth_error
      // after a newer authoritative control-plane event opened the shell.
      if (window.__tradutorAuthState === 'authenticated' || window.__tradutorCommunityAuthenticated === true) {
        authTrace('LOGIN_FAILURE_IGNORED_AFTER_AUTH_RECONCILE', {authenticated: true, reason: 'late_session_failure'});
        return;
      }
      // Never reconcile an explicit login timeout from an existing session:
      // recovery/session state is not proof of the credentials just entered.
      const status = Number(err?.status || 0);
      const sessionFailure = err?.code === 'session_not_established';
      if (sessionFailure || String(err?.code || '').endsWith('_timeout')) {
        authTrace('login_reconciled_after_timeout', {authenticated: false});
      }
      const errorCode = String(err?.code || '');
      const message = sessionFailure
        ? 'O login foi aceito, mas a sessão não pôde ser confirmada.'
        : errorCode === 'email_not_confirmed'
        ? 'Confirme seu e-mail antes de entrar.'
        : status === 429
        ? 'Muitas tentativas. Aguarde um pouco e tente novamente.'
        : status === 400 || status === 401
        ? 'E-mail ou senha inválidos.'
        : errorCode === 'supabase_not_configured' || errorCode === 'auth_config_invalid'
        ? 'O serviço de autenticação não está configurado corretamente.'
        : String(err?.code || '').endsWith('_timeout')
        ? 'O serviço de autenticação demorou para responder.'
        : controller.signal.aborted || err?.name === 'AbortError'
        ? 'O login demorou para responder. Tente novamente.'
        : status === 403 ? 'Esta conta não tem permissão para entrar.'
            : status >= 500 ? 'Não foi possível concluir o login.'
              : 'Não foi possível conectar ao serviço de autenticação.';
      setAuthState(controller.signal.aborted ? 'auth_timeout' : status === 401 ? 'invalid_credentials' : 'auth_error');
      window.__tradutorCommunityAuthenticated = false;
      renderAuthShell('auth_error', message);
      setError(message);
      recoverLoginFormAfterFailure(status === 400 || status === 401 ? 'invalid_credentials' : 'request_failed');
      authTrace('login_failed', {code: err?.code || (status ? `http_${status}` : 'auth_error')});
    } finally {
      clearTimeout(timeoutId);
      clearTimeout(watchdogId);
      explicitLoginSubmissionInProgress = false;
      if (ownLoginAttemptId === attemptId) ownLoginAttemptId = 0;
      if (window.__tradutorActiveLoginController === controller) window.__tradutorActiveLoginController = null;
      if (attemptId === loginAttemptCounter) {
        submit.dataset.busy = '';
        setSubmitLoading(submit, false, mode === 'signup' ? 'Criar conta' : 'Entrar no painel');
        setAuthCredentialFieldsBusy(false);
        authTrace('login_finally_executed', {source: AUTH_HANDLER_ID});
      }
    }
  }
  canonicalAuthSubmitImpl = handleAuthSubmit;
  window.__tradutorAuthHandlersBound = true;
  authTrace('AUTH_HANDLER_BIND_READY', {source: AUTH_HANDLER_ID});
  $('#authTogglePassword')?.addEventListener('click', () => {
    const input = $('#authPassword');
    const toggle = $('#authTogglePassword');
    if (!input || !toggle) return;
    const shown = input.type === 'text';
    input.type = shown ? 'password' : 'text';
    toggle.classList.toggle('shown', !shown);
    toggle.setAttribute('aria-label', shown ? 'Mostrar senha' : 'Ocultar senha');
    toggle.setAttribute('aria-pressed', String(!shown));
  });
  // Recovery fields are rerendered as modes change. Delegate their eye
  // controls from the stable auth card so toggling remains available after
  // every render and never submits the form.
  document.querySelector('.auth-login-card')?.addEventListener('click', (event) => {
    const recoveryAction = event.target.closest('#authRecoveryTerminalAction');
    if (recoveryAction) {
      event.preventDefault();
      if (recoveryAction.dataset.busy === '1') return;
      recoveryAction.dataset.busy = '1';
      handleRecoverySuccessEnter();
      recoveryAction.dataset.busy = '';
      return;
    }
    const toggle = event.target.closest('.auth-login-toggle-pw');
    if (!toggle || toggle.id === 'authTogglePassword') return;
    event.preventDefault();
    const inputId = toggle.id === 'authToggleRecoveryPasswordConfirm'
      ? 'authRecoveryPasswordConfirm' : 'authRecoveryPassword';
    const input = document.getElementById(inputId);
    if (!input) return;
    const shown = input.type === 'text';
    input.type = shown ? 'password' : 'text';
    toggle.setAttribute('aria-label', shown ? 'Mostrar senha' : 'Ocultar senha');
    toggle.setAttribute('aria-pressed', String(!shown));
  });
  setMode('login');
  // Keeps token fresh across login, logout and SDK auto-refresh.
  await withTimeout(authApi.onAuthChange((session, event) => renderSession(session, event)));
  window.__tradutorAuthInitCompleted = true;
}

authInitInFlight = init();
authInitInFlight.catch(() => {
  setAuthState('auth_error');
  window.__tradutorCommunityAuthenticated = false;
  renderAuthShell('auth_error');
  setError('Não foi possível verificar sua sessão.');
  window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
    detail: {authenticated: false, state: 'auth_error'},
  }));
});
