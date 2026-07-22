// Supabase authentication for the local Tradutor.Ia UI.
//
// Only the browser-safe public config reaches this file: SUPABASE_URL and the
// publishable key, both fetched from the backend. The secret key never touches the
// frontend. The official SDK (pinned) owns token storage and refresh; we never persist
// tokens ourselves and never log a session or token.
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.58.0';

let clientPromise = null;
let publicConfigPromise = null;
let memorySession = null;
const SDK_SESSION_TIMEOUT_MS = 5000;

function transportTrace(event, fields = {}) {
  try { window.__tradutorAuthTraceEvent?.(event, fields); } catch (_) { /* diagnostics never affect auth */ }
}

async function fetchPublicConfig() {
  const response = await fetch('/api/community/auth/config', { credentials: 'same-origin' });
  if (!response.ok) throw new Error('auth config unavailable');
  return response.json();
}

function publicConfig() {
  if (!publicConfigPromise) publicConfigPromise = fetchPublicConfig();
  return publicConfigPromise;
}

// Resolves to a configured Supabase client, or null when the backend is not running
// the Supabase provider (e.g. local-session mode). Cached so the SDK loads once.
export function getSupabaseClient() {
  if (clientPromise) return clientPromise;
  clientPromise = (async () => {
    const cfg = await publicConfig();
    if (cfg.provider !== 'supabase' || !cfg.supabase_url || !cfg.publishable_key) {
      return null;
    }
    return createClient(cfg.supabase_url, cfg.publishable_key, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
        flowType: 'pkce',
      },
    });
  })();
  return clientPromise;
}

// Current access token for backend calls, or '' when signed out. Never logged.
export async function currentAccessToken() {
  if (memorySession?.access_token) return memorySession.access_token;
  const client = await getSupabaseClient();
  if (!client) return '';
  const { data } = await client.auth.getSession();
  return data?.session?.access_token || '';
}

export async function currentUserEmail() {
  const client = await getSupabaseClient();
  if (!client) return '';
  const { data } = await client.auth.getUser();
  return data?.user?.email || '';
}

export async function signUp(email, password) {
  const client = await getSupabaseClient();
  if (!client) throw new Error('supabase not configured');
  const redirectTo = `${window.location.origin}/auth/callback`;
  const { data, error } = await client.auth.signUp({
    email, password, options: { emailRedirectTo: redirectTo },
  });
  if (error) throw error;
  return { needsConfirmation: !data.session };
}

export async function signIn(email, password, { signal } = {}) {
  transportTrace('sign_in_started', {source: 'supabase_password'});
  const cfg = await publicConfig();
  if (cfg.provider !== 'supabase' || !cfg.supabase_url || !cfg.publishable_key) {
    throw new Error('supabase not configured');
  }
  const response = await fetch(`${cfg.supabase_url}/auth/v1/token?grant_type=password`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', apikey: cfg.publishable_key},
    body: JSON.stringify({email, password}),
    signal,
  });
  transportTrace('sign_in_response_received', {status: response.status, source: 'supabase_password'});
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* invalid response */ }
  if (!response.ok || !payload.access_token || !payload.refresh_token) {
    const error = new Error(String(payload.error_description || payload.msg || 'authentication_failed'));
    error.status = response.status;
    error.code = String(payload.error_code || payload.error || 'authentication_failed');
    transportTrace('sign_in_error_received', {status: response.status, code: error.code, source: 'supabase_password'});
    throw error;
  }
  memorySession = {
    access_token: String(payload.access_token),
    refresh_token: String(payload.refresh_token),
    token_type: payload.token_type || 'bearer',
    expires_in: payload.expires_in,
    user: payload.user || null,
  };
  const client = await getSupabaseClient();
  if (!client) throw new Error('supabase not configured');
  transportTrace('sdk_session_set_started', {source: 'supabase_password'});
  try {
    const result = await Promise.race([
      client.auth.setSession({
        access_token: payload.access_token,
        refresh_token: payload.refresh_token,
      }),
      new Promise((_, reject) => setTimeout(() => {
        const timeout = new Error('sdk_session_timeout');
        timeout.code = 'sdk_session_timeout';
        reject(timeout);
      }, SDK_SESSION_TIMEOUT_MS)),
    ]);
    if (result?.error) throw result.error;
    transportTrace('sdk_session_set_finished', {source: 'supabase_password'});
  } catch (error) {
    // The token came from the verified Auth endpoint. Keep it only in memory so
    // the canonical backend exchange can finish; the SDK may still complete its
    // own persistence/refresh asynchronously.
    transportTrace('sdk_session_set_deferred', {code: error?.code || 'sdk_session_error', source: 'supabase_password'});
  }
  try {
    const identity = await Promise.race([
      client.auth.getUser(),
      new Promise((_, reject) => setTimeout(() => {
        const timeout = new Error('sdk_get_user_timeout');
        timeout.code = 'sdk_get_user_timeout';
        reject(timeout);
      }, SDK_SESSION_TIMEOUT_MS)),
    ]);
    if (identity?.error || !identity?.data?.user?.id) {
      const error = identity?.error || new Error('user_not_available');
      error.code = error.code || 'user_not_available';
      transportTrace('get_user_error', {code: error.code, source: 'supabase_password'});
      throw error;
    }
    transportTrace('get_user_finished', {authenticated: true, source: 'supabase_password'});
  } catch (error) {
    transportTrace('get_user_error', {code: error?.code || 'get_user_failed', source: 'supabase_password'});
    // The SDK identity call can be blocked by its storage lock even though the
    // Auth endpoint returned a valid session. Verify the same user directly over
    // the authenticated REST endpoint before failing the login.
    try {
      const identityResponse = await fetch(`${cfg.supabase_url}/auth/v1/user`, {
        headers: {apikey: cfg.publishable_key, Authorization: `Bearer ${memorySession.access_token}`},
        signal,
      });
      const identityPayload = await identityResponse.json().catch(() => ({}));
      if (identityResponse.ok && identityPayload?.id) {
        memorySession.user = identityPayload;
        transportTrace('get_user_finished', {authenticated: true, source: 'supabase_rest'});
        return memorySession;
      }
      const identityError = new Error('user_not_available');
      identityError.status = identityResponse.status;
      identityError.code = identityPayload?.code || 'user_not_available';
      throw identityError;
    } catch (fallbackError) {
      transportTrace('get_user_error', {code: fallbackError?.code || 'user_not_available', status: fallbackError?.status || 0, source: 'supabase_rest'});
      throw fallbackError;
    }
  }
  return memorySession;
}

export async function signOut() {
  memorySession = null;
  window.__tradutorAccessToken = '';
  const client = await getSupabaseClient();
  if (client) await client.auth.signOut();
}

// Subscribe to session changes so the shell can re-render on login/logout/refresh.
export async function onAuthChange(handler) {
  const client = await getSupabaseClient();
  if (!client) { handler(null, 'INITIAL_SESSION'); return () => {}; }
  const { data } = client.auth.onAuthStateChange((event, session) => handler(session, event));
  const { data: initial } = await client.auth.getSession();
  handler(memorySession || initial?.session || null, 'INITIAL_SESSION');
  return () => data.subscription.unsubscribe();
}
