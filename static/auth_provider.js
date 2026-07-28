// Browser auth adapter for the active provider.  Supabase remains available as a
// rollback module, but Better Auth mode never imports the Supabase SDK.

let configPromise = null;
let providerPromise = null;

async function publicConfig() {
  if (!configPromise) {
    configPromise = fetch('/api/community/auth/config', {
      credentials: 'same-origin',
      cache: 'no-store',
    }).then((response) => {
      if (!response.ok) throw new Error('auth_config_unavailable');
      return response.json();
    });
  }
  return configPromise;
}

function authTrace(event, fields = {}) {
  try { window.__tradutorAuthTraceEvent?.(event, fields); } catch (_) { /* diagnostics only */ }
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', ...(options.headers || {})},
    cache: 'no-store',
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(String(payload?.message || payload?.error || 'authentication_failed'));
    error.status = response.status;
    error.code = String(payload?.code || payload?.error || `http_${response.status}`);
    throw error;
  }
  return payload;
}

function betterAuthAdapter(config) {
  const base = String(config.auth_base_path || '/api/auth').replace(/\/$/, '');
  return {
    async getSupabaseClient() {
      return {provider: 'better_auth'};
    },
    async currentAccessToken() {
      return '';
    },
    async getCanonicalAccessToken() {
      return '';
    },
    async currentUserEmail() {
      return '';
    },
    async signUp(email, password, {signal} = {}) {
      authTrace('better_auth_signup_started', {source: 'better_auth'});
      await requestJson(`${base}/sign-up/email`, {
        method: 'POST',
        body: JSON.stringify({email, password, name: 'Tradutor'}),
        signal,
      });
      authTrace('better_auth_signup_finished', {source: 'better_auth'});
      return {needsConfirmation: false};
    },
    async signIn(email, password, {signal} = {}) {
      authTrace('sign_in_started', {source: 'better_auth_password'});
      const payload = await requestJson(`${base}/sign-in/email`, {
        method: 'POST',
        body: JSON.stringify({email, password, rememberMe: true}),
        signal,
      });
      authTrace('sign_in_response_received', {status: 200, source: 'better_auth_password'});
      return payload;
    },
    async signOut() {
      try {
        await requestJson(`${base}/sign-out`, {method: 'POST', body: '{}'});
      } catch (_) {
        // Canonical local cleanup below remains authoritative for the shell.
      }
    },
    async onAuthChange(handler) {
      try {
        const session = await fetch('/api/community/auth/session', {
          credentials: 'same-origin',
          cache: 'no-store',
        }).then((response) => response.ok ? response.json() : null);
        handler(session?.authenticated ? {provider: 'better_auth'} : null, 'INITIAL_SESSION');
      } catch (_) {
        handler(null, 'INITIAL_SESSION');
      }
      return () => {};
    },
  };
}

function csrfToken() {
  const prefix = 'tradutor_community_csrf=';
  const part = document.cookie.split(';').map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return part ? decodeURIComponent(part.slice(prefix.length)) : '';
}

function localTestAdapter() {
  return {
    async getSupabaseClient() {
      return {provider: 'local_test'};
    },
    async currentAccessToken() {
      return '';
    },
    async getCanonicalAccessToken() {
      return '';
    },
    async currentUserEmail() {
      return '';
    },
    async signUp() {
      const error = new Error('local_test_signup_disabled');
      error.status = 403;
      error.code = 'local_test_signup_disabled';
      throw error;
    },
    async signIn(email, password, {signal} = {}) {
      authTrace('sign_in_started', {source: 'local_test_password'});
      const payload = await requestJson('/api/community/auth/local-test/login', {
        method: 'POST',
        body: JSON.stringify({email, password}),
        signal,
      });
      authTrace('sign_in_response_received', {status: 200, source: 'local_test_password'});
      return payload;
    },
    async signOut() {
      const csrf = csrfToken();
      await requestJson('/api/community/auth/logout', {
        method: 'POST',
        body: '{}',
        headers: csrf ? {'X-Tradutor-CSRF': csrf} : {},
      });
    },
    async onAuthChange(handler) {
      try {
        const session = await fetch('/api/community/auth/session', {
          credentials: 'same-origin',
          cache: 'no-store',
        }).then((response) => response.ok ? response.json() : null);
        handler(session?.authenticated ? {provider: 'local_test'} : null, 'INITIAL_SESSION');
      } catch (_) {
        handler(null, 'INITIAL_SESSION');
      }
      return () => {};
    },
  };
}

async function provider() {
  if (providerPromise) return providerPromise;
  providerPromise = (async () => {
    const cfg = await publicConfig();
    if (cfg.provider === 'better_auth') return betterAuthAdapter(cfg);
    if (cfg.provider === 'local_test') return localTestAdapter(cfg);
    return import(`/static/supabase_auth.js?v=${Date.now()}`);
  })();
  return providerPromise;
}

export async function getSupabaseClient() {
  return (await provider()).getSupabaseClient();
}

export async function currentAccessToken() {
  return (await provider()).currentAccessToken();
}

export async function getCanonicalAccessToken() {
  return (await provider()).getCanonicalAccessToken();
}

export async function currentUserEmail() {
  return (await provider()).currentUserEmail();
}

export async function signUp(email, password, options = {}) {
  return (await provider()).signUp(email, password, options);
}

export async function signIn(email, password, options = {}) {
  return (await provider()).signIn(email, password, options);
}

export async function signOut() {
  return (await provider()).signOut();
}

export async function onAuthChange(handler) {
  return (await provider()).onAuthChange(handler);
}

export async function authEnvironment() {
  return publicConfig();
}
