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
const AUTH_BOOTSTRAP_TIMEOUT_MS = 10000;
// The shell must not infer "visitor" while the backend session is still being
// resolved.  The backend response is authoritative for local-session and Supabase
// providers alike; the SDK session only supplies the bearer when applicable.
window.__tradutorAuthState = 'auth_loading';
window.__tradutorCommunityUserId = '';

function setAuthState(state, userId = '') {
  window.__tradutorAuthState = state;
  window.__tradutorCommunityUserId = state === 'authenticated' ? String(userId || '') : '';
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

async function syncBackendSession(accessToken = '') {
  const headers = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  try {
    const response = await withTimeout(fetch('/api/community/auth/session', {
      headers, credentials: 'same-origin', cache: 'no-store',
    }));
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    if (response.ok && payload.authenticated) {
      setAuthState('authenticated', payload.user_id);
      window.__tradutorCommunityAuthenticated = true;
      window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
        detail: {authenticated: true, user_id: String(payload.user_id || ''), state: 'authenticated'},
      }));
      return payload;
    }
    if (response.status === 401) setAuthState('session_expired');
    else if (response.status === 403) setAuthState('auth_error');
    else setAuthState('unauthenticated');
    window.__tradutorCommunityAuthenticated = false;
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: window.__tradutorAuthState},
    }));
    return payload;
  } catch (_) {
    setAuthState('auth_error');
    window.__tradutorCommunityAuthenticated = false;
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'auth_error'},
    }));
    return null;
  }
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
    const timeoutId = setTimeout(() => controller.abort(), AUTH_BOOTSTRAP_TIMEOUT_MS * 2);
    submit.dataset.busy = '1';
    submit.disabled = true;
    submit.textContent = 'Aguarde…';
    setAuthState('auth_submitting');
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
        closeModal();
      }
    } catch (err) {
      const status = Number(err?.status || 0);
      const message = controller.signal.aborted || err?.name === 'AbortError'
        ? 'O login demorou para responder. Tente novamente.'
        : status === 401 ? 'E-mail ou senha inválidos.'
          : status === 403 ? 'Esta conta não tem permissão para entrar.'
            : status >= 500 ? 'Não foi possível concluir o login.'
              : 'Não foi possível conectar ao serviço de autenticação.';
      setAuthState(controller.signal.aborted ? 'auth_timeout' : status === 401 ? 'invalid_credentials' : 'auth_error');
      window.__tradutorCommunityAuthenticated = false;
      renderAuthShell('auth_error', message);
      setError(message);
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
