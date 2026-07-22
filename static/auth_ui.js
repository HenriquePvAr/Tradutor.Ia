// Wires the minimal auth UI (masthead status + modal) to the Supabase SDK module.
// Loaded as an ES module so it can import the SDK. Keeps the current access token in a
// window field that the classic tradutor_ui.js reads to attach the Bearer header.
// Never logs a token, session, or password.
import {
  getSupabaseClient, signUp, signIn, signOut, onAuthChange,
} from '/static/supabase_auth.js';

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
// The shell must not infer "visitor" while the backend session is still being
// resolved.  The backend response is authoritative for local-session and Supabase
// providers alike; the SDK session only supplies the bearer when applicable.
window.__tradutorAuthState = 'auth_loading';
window.__tradutorCommunityUserId = '';

function setAuthState(state, userId = '') {
  window.__tradutorAuthState = state;
  window.__tradutorCommunityUserId = state === 'authenticated' ? String(userId || '') : '';
}

async function syncBackendSession(accessToken = '') {
  const headers = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  try {
    const response = await fetch('/api/community/auth/session', {
      headers, credentials: 'same-origin', cache: 'no-store',
    });
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
  const client = await getSupabaseClient();
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
    await signOut();
  });
  $('#authForm')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    setError(''); setNote('');
    const email = $('#authEmail').value.trim();
    const password = $('#authPassword').value;
    const submit = $('#authSubmit');
    submit.disabled = true;
    submit.textContent = 'Aguarde…';
    try {
      if (mode === 'signup') {
        const { needsConfirmation } = await signUp(email, password);
        if (needsConfirmation) {
          setNote('Conta criada. Confirme pelo e-mail e depois entre.');
          setMode('login');
        } else {
          closeModal();
        }
      } else {
        await signIn(email, password);
        closeModal();
      }
    } catch (err) {
      setError(err?.message ? String(err.message) : 'Falha na autenticação.');
    } finally {
      submit.disabled = false;
      submit.textContent = mode === 'signup' ? 'Criar conta' : 'Entrar';
    }
  });
  // Keeps token fresh across login, logout and SDK auto-refresh.
  await onAuthChange(renderSession);
}

init().catch(() => {
  setAuthState('auth_error');
  window.__tradutorCommunityAuthenticated = false;
  window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
    detail: {authenticated: false, state: 'auth_error'},
  }));
});
