// Wires the minimal auth UI (masthead status + modal) to the Supabase SDK module.
// Loaded as an ES module so it can import the SDK. Keeps the current access token in a
// window field that the classic tradutor_ui.js reads to attach the Bearer header.
// Never logs a token, session, or password.
const $ = (sel) => document.querySelector(sel);

function setError(msg) {
  const el = $('#authError');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
}

function setNote(msg) {
  const el = $('#authNote');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
}

function openModal() { $('#authModalOverlay')?.classList.add('show'); }
function closeModal() {
  $('#authModalOverlay')?.classList.remove('show');
  setError(''); setNote('');
}

let mode = 'login';
let authApi = null;
let authHeartbeatTimer = 0;
let authHeartbeatBusy = false;
let loginAttemptCounter = 0;
const AUTH_BOOTSTRAP_TIMEOUT_MS = 10000;
const AUTH_LOGIN_TIMEOUT_MS = 20000;
const AUTH_HANDLER_ID = 'auth_ui:canonical-submit-v3';
window.__tradutorAuthBuild = `auth_ui:${new URL(import.meta.url).searchParams.get('v') || 'unversioned'}`;
// The shell must not infer "visitor" while the backend session is still being
// resolved.  The backend response is authoritative for local-session and Supabase
// providers alike; the SDK session only supplies the bearer when applicable.
window.__tradutorAuthState = 'auth_loading';
window.__tradutorCommunityUserId = '';
window.__tradutorAuthTrace = Array.isArray(window.__tradutorAuthTrace)
  ? window.__tradutorAuthTrace : [];

function authTrace(event, fields = {}) {
  const safe = {event: String(event || ''), at: Date.now()};
  for (const key of ['status', 'code', 'authenticated', 'source']) {
    if (fields[key] !== undefined) safe[key] = fields[key];
  }
  window.__tradutorAuthTrace.push(safe);
  try {
    document.documentElement.dataset.tradutorAuthLastEvent = safe.event;
    if (safe.code) document.documentElement.dataset.tradutorAuthLastCode = String(safe.code).slice(0, 80);
    if (safe.status !== undefined) document.documentElement.dataset.tradutorAuthLastStatus = String(safe.status);
  } catch (_) { /* diagnostics never affect auth */ }
  if (window.__tradutorAuthTrace.length > 40) window.__tradutorAuthTrace.shift();
}

window.__tradutorAuthTraceEvent = authTrace;

function setAuthState(state, userId = '') {
  window.__tradutorAuthState = state;
  window.__tradutorCommunityUserId = state === 'authenticated' ? String(userId || '') : '';
  window.__tradutorAuthStore = {
    status: state,
    authenticated: state === 'authenticated',
    user_id: state === 'authenticated' ? String(userId || '') : '',
  };
  authTrace('auth_state_changed', {status: state, authenticated: state === 'authenticated'});
}

function startAuthHeartbeat() {
  if (authHeartbeatTimer) return;
  authHeartbeatTimer = window.setInterval(async () => {
    if (authHeartbeatBusy || window.__tradutorAuthState !== 'authenticated' || !authApi) return;
    authHeartbeatBusy = true;
    try {
      const token = await authApi.currentAccessToken();
      const canonical = await syncBackendSession(token);
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
  window.__tradutorAccessToken = '';
  openBtn.hidden = state === 'auth_loading' || state === 'authenticated';
  logoutBtn.hidden = state !== 'authenticated';
  if (state === 'auth_loading') status.textContent = 'Verificando sessão…';
  else if (state === 'authenticated') status.textContent = '';
  else if (state === 'auth_error') status.textContent = message || 'Não foi possível verificar sua sessão.';
  else status.textContent = 'visitante';
}

function withTimeout(promise, timeoutMs = AUTH_BOOTSTRAP_TIMEOUT_MS) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error('auth_bootstrap_timeout')), timeoutMs)),
  ]);
}

async function syncBackendSession(accessToken = '', {signal} = {}) {
  const headers = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  try {
    if (accessToken) authTrace('local_session_exchange_started', {source: 'bearer'});
    authTrace('canonical_session_request', {source: accessToken ? 'bearer' : 'cookie'});
    const response = await withTimeout(fetch('/api/community/auth/session', {
      headers, credentials: 'same-origin', cache: 'no-store', signal,
    }));
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    if (response.ok && payload.authenticated) {
      if (accessToken) authTrace('local_session_exchange_finished', {status: response.status, authenticated: true, source: 'bearer'});
      setAuthState('authenticated', payload.user_id);
      window.__tradutorCommunityAuthenticated = true;
      authTrace('canonical_session_confirmed', {authenticated: true});
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
    authTrace('canonical_session_rejected', {status: response.status, code: payload.reason_code || 'authentication_required', authenticated: false});
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: window.__tradutorAuthState},
    }));
    return payload;
  } catch (_) {
    setAuthState('auth_error');
    window.__tradutorCommunityAuthenticated = false;
    authTrace('canonical_session_error', {code: 'network_or_timeout'});
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'auth_error'},
    }));
    return null;
  }
}

async function establishCanonicalSession(signal) {
  let accessToken = '';
  try { accessToken = await authApi.currentAccessToken(); } catch (_) { /* backend remains authoritative */ }
  const payload = await syncBackendSession(accessToken, {signal});
  if (payload?.authenticated) return payload;
  const error = new Error('session_not_established');
  error.code = 'session_not_established';
  throw error;
}

async function completeLoginFlow(email, password, signal) {
  window.__tradutorLoginStage = 'supabase_sign_in';
  authTrace('login_request_started', {source: AUTH_HANDLER_ID});
  await authApi.signIn(email, password, {signal});
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
  renderAuthShell('authenticated');
  return canonical;
}

function setMode(next) {
  mode = next;
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.authmode === next);
  });
  const submit = $('#authSubmit');
  const pw = $('#authPassword');
  if (submit) submit.textContent = next === 'signup' ? 'Criar conta' : 'Entrar';
  if (pw) pw.autocomplete = next === 'signup' ? 'new-password' : 'current-password';
  setError(''); setNote('');
}

function renderSession(session, authEvent = '') {
  const area = $('#authArea');
  const status = $('#authStatus');
  const openBtn = $('#authOpenBtn');
  const logoutBtn = $('#authLogoutBtn');
  if (!area) return;
  area.hidden = false;
  // Supabase can briefly emit a null session while refreshing tokens.  Do not
  // turn that transient event into a logout; the canonical backend session is
  // authoritative.  An explicit SIGNED_OUT event still clears the UI.
  if (!session && authEvent && authEvent !== 'SIGNED_OUT') {
    setAuthState('auth_loading');
    void syncBackendSession().then((canonical) => {
      if (canonical?.authenticated) renderAuthShell('authenticated');
      else renderAuthShell('auth_error', 'Não foi possível verificar sua sessão.');
    });
    return;
  }
  window.__tradutorAccessToken = session?.access_token || '';
  authTrace('sdk_session_changed', {authenticated: Boolean(session)});
  startAuthHeartbeat();
  setAuthState('auth_loading');
  const email = session?.user?.email || '';
  if (session && email) {
    status.textContent = email;
    openBtn.hidden = true;
    logoutBtn.hidden = false;
  } else {
    status.textContent = 'visitante';
    openBtn.hidden = false;
    logoutBtn.hidden = true;
  }
  void syncBackendSession(window.__tradutorAccessToken);
}

async function init() {
  // Dynamic import keeps the canonical state available even when a CDN/module
  // dependency is unavailable. A failed import is an explicit auth_error, never
  // an implicit visitor with stale identity from a previous document.
  setAuthState('auth_loading');
  renderAuthShell('auth_loading');
  try {
    authApi = await withTimeout(import('/static/supabase_auth.js'));
  } catch (_) {
    setAuthState('auth_error');
    window.__tradutorCommunityAuthenticated = false;
    renderAuthShell('auth_error');
    setError('Não foi possível verificar sua sessão.');
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'auth_error'},
    }));
    return;
  }
  const client = await withTimeout(authApi.getSupabaseClient());
  if (!client) {
    // Local-session provider: resolve the HttpOnly cookie through the backend.
    window.__tradutorAccessToken = '';
    setAuthState('auth_loading');
    await syncBackendSession();
    return;
  }
  if (window.__tradutorAuthHandlersBound) return;
  window.__tradutorAuthHandlersBound = true;
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    tab.addEventListener('click', () => setMode(tab.dataset.authmode));
  });
  $('#authOpenBtn')?.addEventListener('click', openModal);
  $('#authModalClose')?.addEventListener('click', () => {
    window.__tradutorActiveLoginController?.abort();
    closeModal();
  });
  $('#authModalOverlay')?.addEventListener('click', (event) => {
    if (event.target === $('#authModalOverlay')) closeModal();
  });
  $('#authLogoutBtn')?.addEventListener('click', async () => {
    await authApi.signOut();
  });
  $('#authForm')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submit = $('#authSubmit');
    if (!submit) return;
    if (submit.dataset.busy === '1') return;
    setError(''); setNote('');
    const email = $('#authEmail').value.trim();
    const password = $('#authPassword').value;
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
      authTrace('login_watchdog_timeout', {code: 'auth_timeout'});
      submit.dataset.busy = '';
      submit.disabled = false;
      submit.textContent = mode === 'signup' ? 'Criar conta' : 'Entrar';
    }, AUTH_LOGIN_TIMEOUT_MS + 250);
    submit.dataset.busy = '1';
    submit.disabled = true;
    submit.textContent = 'Entrando…';
    setAuthState('auth_submitting');
    authTrace('login_submit_received', {source: AUTH_HANDLER_ID});
    try {
      if (mode === 'signup') {
        const { needsConfirmation } = await withTimeout(authApi.signUp(email, password), AUTH_LOGIN_TIMEOUT_MS);
        if (needsConfirmation) {
          setNote('Conta criada. Confirme pelo e-mail e depois entre.');
          setMode('login');
        } else {
          closeModal();
        }
      } else {
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
        closeModal();
        if (typeof window.__tradutorToast === 'function') window.__tradutorToast('Login realizado.', 'ok');
        authTrace('modal_closed', {authenticated: true, source: AUTH_HANDLER_ID});
        authTrace('login_completed', {authenticated: true});
      }
    } catch (err) {
      if (attemptId !== loginAttemptCounter) return;
      if (controller.signal.aborted || err?.code === 'auth_timeout') {
        try {
          await establishCanonicalSession();
          closeModal();
          if (typeof window.__tradutorToast === 'function') window.__tradutorToast('Login realizado.', 'ok');
          authTrace('login_reconciled_after_timeout', {authenticated: true});
          return;
        } catch (_) { /* retain the timeout message below */ }
      }
      const status = Number(err?.status || 0);
      const sessionFailure = err?.code === 'session_not_established';
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
      authTrace('login_failed', {code: err?.code || (status ? `http_${status}` : 'auth_error')});
    } finally {
      clearTimeout(timeoutId);
      clearTimeout(watchdogId);
      if (window.__tradutorActiveLoginController === controller) window.__tradutorActiveLoginController = null;
      if (attemptId === loginAttemptCounter) {
        submit.dataset.busy = '';
        submit.disabled = false;
        submit.textContent = mode === 'signup' ? 'Criar conta' : 'Entrar';
        authTrace('login_finally_executed', {source: AUTH_HANDLER_ID});
      }
    }
  });
  // Keeps token fresh across login, logout and SDK auto-refresh.
  await withTimeout(authApi.onAuthChange((session, event) => renderSession(session, event)));
}

init().catch(() => {
  setAuthState('auth_error');
  window.__tradutorCommunityAuthenticated = false;
  renderAuthShell('auth_error');
  setError('Não foi possível verificar sua sessão.');
  window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
    detail: {authenticated: false, state: 'auth_error'},
  }));
});
