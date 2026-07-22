// Supabase authentication for the local Tradutor.Ia UI.
//
// Only the browser-safe public config reaches this file: SUPABASE_URL and the
// publishable key, both fetched from the backend. The secret key never touches the
// frontend. The official SDK (pinned) owns token storage and refresh; we never persist
// tokens ourselves and never log a session or token.
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.58.0';

let clientPromise = null;
let publicConfigPromise = null;

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
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* invalid response */ }
  if (!response.ok || !payload.access_token || !payload.refresh_token) {
    const error = new Error(String(payload.error_description || payload.msg || 'authentication_failed'));
    error.status = response.status;
    throw error;
  }
  const client = await getSupabaseClient();
  if (!client) throw new Error('supabase not configured');
  const { error } = await client.auth.setSession({
    access_token: payload.access_token,
    refresh_token: payload.refresh_token,
  });
  if (error) throw error;
}

export async function signOut() {
  const client = await getSupabaseClient();
  if (client) await client.auth.signOut();
}

// Subscribe to session changes so the shell can re-render on login/logout/refresh.
export async function onAuthChange(handler) {
  const client = await getSupabaseClient();
  if (!client) { handler(null); return () => {}; }
  const { data } = client.auth.onAuthStateChange((_event, session) => handler(session));
  const { data: initial } = await client.auth.getSession();
  handler(initial?.session || null);
  return () => data.subscription.unsubscribe();
}
