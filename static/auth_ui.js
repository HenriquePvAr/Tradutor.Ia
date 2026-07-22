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
const AUTH_BOOTSTRAP_TIMEOUT_MS = 10000;
const AUTH_LOGIN_TIMEOUT_MS = 20000;
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
  if (window.__tradutorAuthTrace.length > 40) window.__tradutorAuthTrace.shift();
}

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
    authTrace('canonical_session_request', {source: accessToken ? 'bearer' : 'cookie'});
    const response = await withTimeout(fetch('/api/community/auth/session', {
      headers, credentials: 'same-origin', cache: 'no-store', signal,
    }));
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    if (response.ok && payload.authenticated) {
      setAuthState('authenticated', payload.user_id);
      window.__tradutorCommunityAuthenticated = true;
      authTrace('canonical_session_confirmed', {authenticated: true});
      window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
        detail: {authenticated: true, user_id: String(payload.user_id || ''), state: 'authenticated'},
      }));
      return payload;
    }
    if (response.status === 401) setAuthState('session_expired');
    else if (response.status === 403) setAuthState('auth_error');
    else setAuthState('unauthenticated');
    window.__tradutorCommunityAuthenticated = false;
    authTrace('canonical_session_rejected', {status: response.status, authenticated: false});
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

function renderSession(session) {
  const area = $('#authArea');
  const status = $('#authStatus');
  const openBtn = $('#authOpenBtn');
  const logoutBtn = $('#authLogoutBtn');
  if (!area) return;
  area.hidden = false;
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
  document.querySelectorAll('.auth-tab').forEach((tab) => {
    tab.addEventListener('click', () => setMode(tab.dataset.authmode));
  });
  $('#authOpenBtn')?.addEventListener('click', openModal);
  $('#authModalClose')?.addEventListener('click', closeModal);
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
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), AUTH_LOGIN_TIMEOUT_MS);
    submit.dataset.busy = '1';
    submit.disabled = true;
    submit.textContent = 'Aguarde…';
    setAuthState('auth_submitting');
    authTrace('login_submit_started');
    try {
      if (mode === 'signup') {
        const { needsConfirmation } = await authApi.signUp(email, password);
        if (needsConfirmation) {
          setNote('Conta criada. Confirme pelo e-mail e depois entre.');
          setMode('login');
        } else {
          closeModal();
        }
      } else {
        await authApi.signIn(email, password, {signal: controller.signal});
        await establishCanonicalSession(controller.signal);
        closeModal();
        if (typeof window.__tradutorToast === 'function') window.__tradutorToast('Login realizado.', 'ok');
        authTrace('login_completed', {authenticated: true});
      }
    } catch (err) {
      if (controller.signal.aborted) {
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
      const message = sessionFailure
        ? 'O login foi aceito, mas a sessão não pôde ser confirmada.'
        : controller.signal.aborted || err?.name === 'AbortError'
        ? 'O login demorou para responder. Tente novamente.'
        : status === 401 ? 'E-mail ou senha inválidos.'
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
      submit.dataset.busy = '';
      submit.disabled = false;
      submit.textContent = mode === 'signup' ? 'Criar conta' : 'Entrar';
    }
  });
  // Keeps token fresh across login, logout and SDK auto-refresh.
  await withTimeout(authApi.onAuthChange(renderSession));
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
