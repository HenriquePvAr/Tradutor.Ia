// Central client for the authenticated beta control plane.  The browser only
// ever sends the user's publishable-session bearer; server secrets stay inside
// Edge Functions.  This module is intentionally inert until Auth announces an
// authenticated session.
const state = {
  bootstrap: null, wallet: null, progression: null, license: null,
  device: null, identity: null, heartbeatTimer: 0, inFlight: null, authenticated: false,
  ready: false, walletLoaded: false, walletStatus: 'loading', translationInFlight: null,
  passiveAdsEnabled: false,
};
const WINDOW_REALM_ID = (() => {
  try { return window.__YOMU_WINDOW_REALM_ID__ || (window.__YOMU_WINDOW_REALM_ID__ = crypto.randomUUID()); }
  catch (_) { return window.__YOMU_WINDOW_REALM_ID__ || `realm-${Date.now()}`; }
})();

const WALLET_FIELDS = ['daily', 'subscription', 'permanent', 'reserved'];
const YK_DIAGNOSTIC_ENABLED = window.__yomuYkDiagnostic === true;
let ykClaimAttemptCount = 0;
let ykWalletRefreshSeq = 0;
function ykTrace(event, fields = {}) {
  if (!YK_DIAGNOSTIC_ENABLED) return;
  const raw = fields && typeof fields === 'object' ? fields : {};
  const safe = {};
  for (const key of ['reason', 'trigger', 'amount_granted', 'granted', 'amount', 'already_claimed', 'next_available_at', 'next_claim_at',
    'daily_target', 'daily_yk_target', 'daily_stored', 'daily_usable', 'daily_expires_at', 'expires_at', 'usable_now',
    'daily', 'permanent', 'subscription', 'has_active_plan', 'resolved_plan', 'canonical_usable', 'displayed_usable', 'header_match',
    'backend_next_available', 'ui_next_available', 'cooldown_match', 'passive_ads_enabled',
    'claim_attempt_count', 'attempt', 'wallet_refresh_seq', 'route', 'session_generation', 'surface_visible', 'navigation_success',
    'auth_context_present', 'current_cycle_claim_exists_for_auth_user', 'current_cycle_claim_amount_for_auth_user',
    'valid_daily_credit_total_for_auth_user', 'daily_debit_total_for_auth_user',
    'active_daily_reserved_total_for_auth_user', 'daily_net_stored_recomputed_for_auth_user',
    'wallet_daily_stored_returned', 'daily_reconciliation_match',
    'session_valid', 'error_kind', 'ok', 'error_code']) {
    if (raw[key] !== undefined && raw[key] !== null) safe[key] = typeof raw[key] === 'string' ? raw[key].slice(0, 120) : raw[key];
  }
  trace(event, safe);
}
function walletDiagnosticFields(payload = {}, normalized = null) {
  const raw = payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {};
  return {
    daily_stored: Number.isFinite(Number(raw.daily_stored)) ? Number(raw.daily_stored) : Number.isFinite(Number(raw.daily)) ? Number(raw.daily) : null,
    daily_usable: Number.isFinite(Number(raw.daily_usable)) ? Number(raw.daily_usable) : Number.isFinite(Number(raw.daily)) ? Number(raw.daily) : null,
    permanent: Number.isFinite(Number(raw.permanent)) ? Number(raw.permanent) : null,
    subscription: Number.isFinite(Number(raw.subscription)) ? Number(raw.subscription) : null,
    reserved: Number.isFinite(Number(raw.reserved)) ? Number(raw.reserved) : null,
    usable_now: Number.isFinite(Number(raw.usable_now)) ? Number(raw.usable_now) : normalized?.active_yk ?? null,
    has_active_plan: typeof raw.has_active_plan === 'boolean' ? raw.has_active_plan : null,
    resolved_plan: typeof raw.resolved_plan === 'string' ? raw.resolved_plan.slice(0, 80) : null,
    daily_target: Number.isFinite(Number(raw.daily_yk_target)) ? Number(raw.daily_yk_target) : null,
    expires_at: raw.daily_expires_at || raw.expires_at || null,
    passive_ads_enabled: typeof raw.passive_ads_enabled === 'boolean' ? raw.passive_ads_enabled : null,
    auth_context_present: typeof raw.auth_context_present === 'boolean' ? raw.auth_context_present : null,
    current_cycle_claim_exists_for_auth_user: typeof raw.current_cycle_claim_exists_for_auth_user === 'boolean' ? raw.current_cycle_claim_exists_for_auth_user : null,
    current_cycle_claim_amount_for_auth_user: Number.isFinite(Number(raw.current_cycle_claim_amount_for_auth_user)) ? Number(raw.current_cycle_claim_amount_for_auth_user) : null,
    valid_daily_credit_total_for_auth_user: Number.isFinite(Number(raw.valid_daily_credit_total_for_auth_user)) ? Number(raw.valid_daily_credit_total_for_auth_user) : null,
    daily_debit_total_for_auth_user: Number.isFinite(Number(raw.daily_debit_total_for_auth_user)) ? Number(raw.daily_debit_total_for_auth_user) : null,
    active_daily_reserved_total_for_auth_user: Number.isFinite(Number(raw.active_daily_reserved_total_for_auth_user)) ? Number(raw.active_daily_reserved_total_for_auth_user) : null,
    daily_net_stored_recomputed_for_auth_user: Number.isFinite(Number(raw.daily_net_stored_recomputed_for_auth_user)) ? Number(raw.daily_net_stored_recomputed_for_auth_user) : null,
    wallet_daily_stored_returned: Number.isFinite(Number(raw.wallet_daily_stored_returned)) ? Number(raw.wallet_daily_stored_returned) : null,
    daily_reconciliation_match: typeof raw.daily_reconciliation_match === 'boolean' ? raw.daily_reconciliation_match : null,
  };
}
function normalizeBootstrapSnapshot(payload) {
  const snapshot = payload && typeof payload === 'object' && !Array.isArray(payload)
    ? payload : null;
  const flags = snapshot?.feature_flags;
  if (!snapshot || !flags || typeof flags !== 'object' || Array.isArray(flags)
      || typeof flags.translation_enabled !== 'boolean') {
    throw Object.assign(new Error('bootstrap_policy_missing'), {
      code: 'bootstrap_policy_missing', status: 502,
    });
  }
  return {...snapshot, translation_policy_source: 'control_plane_snapshot',
    translation_policy_resolved_at: new Date().toISOString()};
}
function normalizeWalletState(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return {status: 'unknown_schema', active_yk: null, daily_yk: null, reserved_yk: null, subscription_yk: null, permanent_yk: null};
  const recognized = WALLET_FIELDS.filter(key => Object.prototype.hasOwnProperty.call(payload, key));
  if (!recognized.length) return {status: 'unknown_schema', active_yk: null, daily_yk: null, reserved_yk: null, subscription_yk: null, permanent_yk: null};
  const values = Object.fromEntries(WALLET_FIELDS.map(key => [key, Number(payload[key])]));
  if (recognized.some(key => !Number.isFinite(values[key]) || values[key] < 0)) return {status: 'unknown_schema', active_yk: null, daily_yk: null, reserved_yk: null, subscription_yk: null, permanent_yk: null};
  const daily = values.daily || 0, subscription = values.subscription || 0, permanent = values.permanent || 0, reserved = values.reserved || 0;
  const serverUsable = Number.isFinite(Number(payload.usable_now)) ? Number(payload.usable_now) : null;
  return {status: 'ready', active_yk: Math.max(0, serverUsable === null ? daily + subscription + permanent - reserved : serverUsable), daily_yk: daily, subscription_yk: subscription, permanent_yk: permanent, reserved_yk: reserved, usable_now: serverUsable};
}
function applyWallet(payload, source) {
  const raw = payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {};
  const wallet_refresh_seq = ++ykWalletRefreshSeq;
  const rawNumber = (key) => Number.isFinite(Number(raw[key])) ? Number(raw[key]) : null;
  trace('WALLET_PAYLOAD_SANITIZED', {source, http_status: 200, daily: rawNumber('daily'), subscription: rawNumber('subscription'), permanent: rawNumber('permanent'), reserved: rawNumber('reserved')});
  ykTrace('YK_WALLET_RPC_RAW', {wallet_refresh_seq, ...walletDiagnosticFields(raw)});
  const normalized = normalizeWalletState(payload);
  trace('WALLET_NORMALIZED', {source, status: normalized.status, daily: normalized.daily_yk, subscription: normalized.subscription_yk, permanent: normalized.permanent_yk, reserved: normalized.reserved_yk, active_yk: normalized.active_yk});
  state.wallet = normalized;
  state.walletLoaded = normalized.status === 'ready';
  state.walletStatus = normalized.status;
  ykTrace('YK_WALLET_NORMALIZED', {reason: source, wallet_refresh_seq, ...walletDiagnosticFields(payload, normalized)});
  ykTrace('YK_WALLET_SNAPSHOT', {reason: source, wallet_refresh_seq, ...walletDiagnosticFields(payload, normalized)});
  if (normalized.status === 'unknown_schema') trace('WALLET_SCHEMA_UNRECOGNIZED', {source, top_level_keys: Object.keys(payload || {}).slice(0, 20).join(','), recognized_fields: ''});
  return normalized;
}
function dispatchControlPlaneUpdated(snapshot) {
  const flags = snapshot?.bootstrap?.feature_flags;
  const fields = {
    translation_enabled: typeof flags?.translation_enabled === 'boolean' ? flags.translation_enabled : null,
    source: snapshot?.bootstrap?.translation_policy_source || 'control_plane_snapshot',
    resolved_at: snapshot?.bootstrap?.translation_policy_resolved_at || '',
    event_name: 'tradutor-control-plane-updated', event_target: 'window', window_realm_id: WINDOW_REALM_ID,
    is_top_window: window.top === window, pathname: window.location?.pathname || '/',
  };
  trace('CONTROL_PLANE_UI_EVENT_DISPATCH_ATTEMPT', fields);
  window.dispatchEvent(new CustomEvent('tradutor-control-plane-updated', {detail: {state: snapshot}}));
  trace('CONTROL_PLANE_UI_EVENT_DISPATCHED', fields);
}

function trace(event, fields = {}) {
  const safe = {event: String(event || '').slice(0, 80), ...fields};
  try { window.__tradutorUiTrace?.push({at: Date.now(), ...safe}); } catch (_) {}
  // Persist only the explicitly allow-listed, sanitized control-plane fields.
  // Diagnostics are best-effort and must never affect the functional flow.
  try {
    const allowed = ['step', 'status', 'http_status', 'reason_code', 'error_code',
      'error_name', 'user_id', 'license_id', 'license_status', 'device_uuid',
      'device_id_prefix', 'identity_exists', 'public_key_present', 'bridge_available', 'authenticated',
      'attempt', 'duration_ms', 'ok', 'valid', 'reason', 'license_present', 'verified', 'challenge_id',
      'ttl_available', 'source', 'top_level_keys', 'recognized_fields', 'active_yk', 'daily_yk', 'reserved_yk', 'subscription_yk', 'permanent_yk',
      'daily', 'subscription', 'permanent', 'reserved', 'amount_granted', 'granted', 'amount',
      'already_claimed', 'next_available_at', 'next_claim_at', 'daily_target', 'daily_stored',
      'daily_usable', 'daily_expires_at', 'expires_at', 'usable_now', 'has_active_plan',
      'resolved_plan', 'daily_yk_target', 'canonical_usable',
      'wallet_refresh_seq', 'auth_context_present', 'current_cycle_claim_exists_for_auth_user',
      'current_cycle_claim_amount_for_auth_user', 'valid_daily_credit_total_for_auth_user',
      'daily_debit_total_for_auth_user', 'active_daily_reserved_total_for_auth_user',
      'daily_net_stored_recomputed_for_auth_user', 'wallet_daily_stored_returned',
      'daily_reconciliation_match',
      'displayed_usable', 'header_match', 'backend_next_available', 'ui_next_available',
      'cooldown_match', 'passive_ads_enabled', 'claim_attempt_count', 'trigger', 'route',
      'session_generation', 'surface_visible', 'navigation_success', 'session_valid', 'error_kind',
      'top_text', 'rewards_text',
      'translation_enabled', 'maintenance_mode', 'keys_present', 'project_ref', 'function_slug',
      'event_name', 'event_target', 'policy_source', 'policy_resolved_at', 'resolved_at',
      'window_realm_id', 'is_top_window', 'pathname', 'has_detail', 'detail_translation_enabled',
      'detail_source', 'detail_resolved_at', 'probe'];
    const payload = {event: safe.event};
    for (const key of allowed) {
      const value = safe[key];
      if (value === undefined || value === null) continue;
      payload[key] = typeof value === 'string' ? value.slice(0, 120) : value;
    }
    void fetch('/api/ui/control-plane-trace', {method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin', body: JSON.stringify(payload), keepalive: true}).catch(() => {});
  } catch (_) {}
}
function isDesktopRuntime() {
  return new URLSearchParams(window.location.search || '').get('desktop') === '1'
    || window.__yomuDesktopRuntime === true;
}
function friendly(code) {
  return ({
    license_not_found: 'Não foi possível validar sua licença.',
    license_revoked: 'Sua licença foi revogada.', license_expired: 'Sua licença expirou.',
    device_limit_reached: 'O limite de dispositivos foi atingido.',
    device_not_found: 'Este dispositivo ainda não foi autorizado.', insufficient_yk: 'Você não possui Yomu Keys suficientes.',
    passive_ads_disabled: 'Ative os anúncios passivos para resgatar os créditos diários.',
    already_claimed: 'Os créditos de hoje já foram resgatados.',
    unauthorized: 'Faça login novamente para resgatar os créditos diários.',
    translation_disabled: 'As traduções estão temporariamente indisponíveis.',
    maintenance_mode: 'O serviço está em manutenção.', provider_not_configured: 'O serviço de tradução ainda não está configurado.',
    provider_outcome_unknown: 'Estamos verificando o estado desta tradução. Não inicie uma nova tentativa ainda.',
    rate_limited: 'Muitas tentativas. Aguarde um momento.', server_unavailable: 'Não foi possível validar sua licença. Verifique sua conexão e tente novamente.',
  })[code] || 'Não foi possível validar sua licença. Verifique sua conexão e tente novamente.';
}
async function config() {
  const r = await fetch('/api/community/auth/config', {credentials: 'same-origin', cache: 'no-store'});
  if (!r.ok) throw Object.assign(new Error('server_unavailable'), {code: 'server_unavailable', status: r.status});
  return r.json();
}
async function token() {
  const getter = window.__tradutorGetCanonicalAccessToken;
  return typeof getter === 'function' ? (await getter()) || '' : window.__tradutorAccessToken || '';
}
async function call(name, body = {}) {
  const cfg = await config();
  const bearer = await token();
  if (!bearer || cfg.provider !== 'supabase' || !cfg.supabase_url) throw Object.assign(new Error('server_unavailable'), {code: 'server_unavailable', status: 503});
  const r = await fetch(`${String(cfg.supabase_url).replace(/\/$/, '')}/functions/v1/${name}`, {
    method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json', apikey: cfg.publishable_key || '', Authorization: `Bearer ${bearer}`},
    body: JSON.stringify(body),
  });
  if (name === 'beta-bootstrap') {
    let project_ref = '';
    try { project_ref = new URL(cfg.supabase_url).hostname.split('.')[0] || ''; } catch (_) {}
    trace('CONTROL_PLANE_ENDPOINT_IDENTITY', {project_ref, function_slug: name});
  }
  const payload = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(String(payload.code || payload.message || `http_${r.status}`)), {code: String(payload.code || ''), status: r.status});
  return payload;
}
async function callRpc(name, body = {}) {
  const cfg = await config();
  const bearer = await token();
  if (!bearer || cfg.provider !== 'supabase' || !cfg.supabase_url) throw Object.assign(new Error('server_unavailable'), {code: 'server_unavailable', status: 503});
  const r = await fetch(`${String(cfg.supabase_url).replace(/\/$/, '')}/rest/v1/rpc/${name}`, {
    method: 'POST', cache: 'no-store', headers: {'Content-Type': 'application/json', apikey: cfg.publishable_key || '', Authorization: `Bearer ${bearer}`},
    body: JSON.stringify(body),
  });
  const payload = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(String(payload.message || payload.code || `http_${r.status}`)), {code: String(payload.code || ''), status: r.status});
  return payload;
}
async function loadPassiveAdsPreference() {
  const payload = await callRpc('get_passive_ads_preference');
  state.passiveAdsEnabled = payload?.passive_ads_enabled === true;
  window.__yomuPassiveAdsPreference = state.passiveAdsEnabled;
  window.dispatchEvent(new CustomEvent('yomu-passive-ads-preference-changed', {detail: {enabled: state.passiveAdsEnabled}}));
  return state.passiveAdsEnabled;
}
async function setPassiveAdsPreference(enabled) {
  const payload = await callRpc('set_passive_ads_enabled', {p_enabled: Boolean(enabled)});
  state.passiveAdsEnabled = payload?.passive_ads_enabled === true;
  window.__yomuPassiveAdsPreference = state.passiveAdsEnabled;
  window.dispatchEvent(new CustomEvent('yomu-passive-ads-preference-changed', {detail: {enabled: state.passiveAdsEnabled}}));
  return state.passiveAdsEnabled;
}
async function desktopIdentity() {
  if (!window.pywebview?.api) {
    await new Promise(resolve => {
      const timer = window.setTimeout(resolve, 5000);
      window.addEventListener('pywebviewready', () => { window.clearTimeout(timer); resolve(); }, {once: true});
    });
  }
  if (!window.pywebview?.api) throw Object.assign(new Error('device_identity_unavailable'), {code: 'device_identity_unavailable', status: 503});
  if (!window.pywebview.api.get_install_identity) throw Object.assign(new Error('device_identity_unavailable'), {code: 'device_identity_unavailable', status: 503});
  return window.pywebview.api.get_install_identity();
}
// Seal the short-lived Supabase access token in the desktop parent.  The token is
// passed only as an in-memory bridge argument and the bridge returns metadata, never
// the credential or an environment/command-line value.
async function sealJobAuthContext(jobId, userId = '') {
  if (!isDesktopRuntime() || !window.pywebview?.api?.set_auth_context) {
    throw Object.assign(new Error('auth_handoff_unavailable'), {code: 'auth_handoff_unavailable'});
  }
  const bearer = await token();
  if (!bearer) throw Object.assign(new Error('auth_required'), {code: 'auth_required'});
  const result = await window.pywebview.api.set_auth_context(String(jobId || ''), bearer, null, String(userId || ''));
  trace('AUTH_CONTEXT_SEALED', {job_id: String(jobId || ''), envelope_ready: result?.envelope_ready === true});
  return result;
}
window.__yomuSealJobAuthContext = sealJobAuthContext;
async function ensureDevice() {
  trace('CONTROL_PLANE_ENSURE_DEVICE_STARTED', {step: 'ensure_device', authenticated: state.authenticated});
  if (!isDesktopRuntime()) { trace('CONTROL_PLANE_ERROR', {step: 'ensure_device', reason_code: 'not_desktop_runtime'}); return null; }
  if (state.device?.verified) return state.device;
  try {
    trace('INSTALL_IDENTITY_REQUESTED', {step: 'get_install_identity'});
    state.identity = state.identity || await desktopIdentity();
    trace('INSTALL_IDENTITY_RESULT', {step: 'get_install_identity', ok: true, identity_exists: Boolean(state.identity?.device_id), device_id_prefix: String(state.identity?.device_id || '').slice(0, 12), public_key_present: Boolean(state.identity?.public_key)});
    trace('DEVICE_REGISTER_STARTED', {step: 'device_register'});
    const registered = await call('device-register', {device_id: state.identity.device_id, public_key: state.identity.public_key});
    trace('DEVICE_REGISTER_RESULT', {step: 'device_register', ok: true, license_id: registered.license_id, device_uuid: registered.device_id || registered.device_uuid, status: registered.status});
  const deviceId = String(registered.device_id || registered.device_uuid || state.identity.device_id);
    trace('DEVICE_CHALLENGE_STARTED', {step: 'device_challenge', device_uuid: deviceId});
    const challenge = await call('device-challenge', {device_uuid: deviceId});
    trace('DEVICE_CHALLENGE_RESULT', {step: 'device_challenge', ok: true, challenge_id: challenge.challenge_id, ttl_available: Boolean(challenge.expires_at || challenge.ttl_seconds || challenge.ttl)});
    trace('DEVICE_SIGN_STARTED', {step: 'device_sign', bridge_available: Boolean(window.pywebview?.api?.sign_device_challenge)});
    const signed = await window.pywebview.api.sign_device_challenge(String(challenge.nonce || ''));
    trace('DEVICE_SIGN_RESULT', {step: 'device_sign', ok: Boolean(signed?.signature), bridge_available: true});
    trace('DEVICE_VERIFY_STARTED', {step: 'device_verify', challenge_id: challenge.challenge_id});
    const verified = await call('device-verify', {challenge_id: challenge.challenge_id, nonce: challenge.nonce, signature: signed.signature});
    trace('DEVICE_VERIFY_RESULT', {step: 'device_verify', ok: true, verified: verified?.verified === true, status: verified?.status});
    state.device = {...registered, ...verified, device_id: deviceId, device_uuid: deviceId, verified: true};
    trace('CONTROL_PLANE_DEVICE_VERIFIED', {step: 'ensure_device', ok: true, device_uuid: deviceId});
    return state.device;
  } catch (error) {
    trace('CONTROL_PLANE_ERROR', {step: 'ensure_device', error_name: String(error?.name || 'Error'), error_code: String(error?.code || '').slice(0, 80), reason_code: String(error?.message || '').slice(0, 120)});
    throw error;
  }
}
function render() {
  const w = state.wallet || {};
  const walletLoading = state.authenticated && state.walletStatus === 'loading';
  const walletError = state.authenticated && state.walletStatus === 'unknown_schema';
  const p = state.progression || state.bootstrap?.rank || {};
  const license = state.license || state.bootstrap?.license || {};
  const wallet = document.querySelector('#controlPlaneWallet');
  const topText = walletLoading ? 'Sincronizando…' : walletError ? 'Saldo indisponível' : `${w.active_yk} YK`;
  if (wallet) wallet.textContent = topText;
  if (YK_DIAGNOSTIC_ENABLED && !walletLoading && !walletError) {
    const canonical = Number.isFinite(Number(w.active_yk)) ? Number(w.active_yk) : null;
    ykTrace('YK_HEADER_STATE', {canonical_usable: canonical, displayed_usable: canonical, header_match: true});
  }
  const rank = document.querySelector('#controlPlaneRank');
  if (rank) rank.textContent = `${String(p.rank || 'Leitor Iniciante')} · ${Number(p.xp || 0)} XP`;
  const status = document.querySelector('#controlPlaneStatus');
  if (status) { const statusValue = String(license.status || '').toLowerCase(); const warning = statusValue && statusValue !== 'active'; status.hidden = !warning; status.textContent = warning ? `Licença ${statusValue === 'revoked' ? 'revogada' : statusValue === 'expired' ? 'expirada' : 'indisponível'}` : ''; status.dataset.state = statusValue || 'active'; }
  const set = (id, value) => { const el = document.querySelector(id); if (!el) return; const child = el.querySelector('.yk-value'); if (child) child.textContent = String(value); else el.textContent = String(value); };
  const daily = w.daily_yk, subscription = w.subscription_yk, permanent = w.permanent_yk;
  const rewardsText = walletLoading ? 'Sincronizando…' : walletError ? 'Saldo indisponível' : `${w.active_yk} YK`;
  set('#rewardsTotal', rewardsText); set('#rewardsDaily', walletLoading ? '…' : walletError ? '—' : daily); set('#rewardsSubscription', walletLoading ? '…' : walletError ? '—' : subscription); set('#rewardsPermanent', walletLoading ? '…' : walletError ? '—' : permanent);
  trace('WALLET_UI_RENDERED', {status: walletLoading ? 'loading' : walletError ? 'error' : 'ready', active_yk: walletLoading || walletError ? null : w.active_yk, top_text: topText, rewards_text: rewardsText});
  set('#rewardsRank', p.rank || 'Leitor Iniciante'); set('#rewardsXp', `${Number(p.xp || 0)} XP`);
  const thresholds = [0,50,200,500,1000,2500,5000]; const rankNames = ['Leitor Iniciante','Explorador de Balões','Caçador de Kanji','Tradutor do Sekai','Mestre de Capítulos','Guardião das Histórias','Lenda do Sekai']; const xp = Number(p.xp || 0); const nextIndex = thresholds.findIndex(t => t > xp); const next = nextIndex >= 0 ? thresholds[nextIndex] : thresholds.at(-1); const prior = thresholds.filter(t => t <= xp).at(-1) || 0;
  const pct = next > prior ? Math.max(0, Math.min(100, ((xp - prior) / (next - prior)) * 100)) : 100;
  const bar = document.querySelector('#rewardsProgressBar'); if (bar) bar.style.width = `${pct}%`;
  const pbar = document.querySelector('#profileRankProgress'); if (pbar) pbar.style.width = `${pct}%`; set('#rewardsNext', next > xp ? `Próximo rank: ${rankNames[nextIndex] || 'próximo rank'} · ${next} XP` : 'Progresso máximo alcançado'); set('#profileRankValue', `${p.rank || 'Leitor Iniciante'} · ${xp} XP`);
}
async function bootstrap() {
  if (!state.authenticated || state.inFlight) return state.inFlight;
  state.inFlight = (async () => {
    const started = Date.now();
    trace('CONTROL_PLANE_BOOTSTRAP_STARTED', {step: 'bootstrap', authenticated: state.authenticated});
    try {
      state.walletStatus = 'loading'; render(); trace('WALLET_FETCH_STARTED', {source: 'beta-bootstrap'});
      const bootstrapPayload = await call('beta-bootstrap');
      const bootstrapFlags = bootstrapPayload && typeof bootstrapPayload === 'object'
        ? bootstrapPayload.feature_flags : undefined;
      trace('BOOTSTRAP_RESPONSE_SHAPE', {
        top_level_keys: Object.keys(bootstrapPayload || {}).slice(0, 30).join(','),
        data_keys: Object.keys(bootstrapPayload?.data || {}).slice(0, 30).join(','),
        payload_keys: Object.keys(bootstrapPayload?.payload || {}).slice(0, 30).join(','),
        body_keys: Object.keys(bootstrapPayload?.body || {}).slice(0, 30).join(','),
        feature_flags_present: Boolean(bootstrapFlags),
        feature_flags_type: Array.isArray(bootstrapFlags) ? 'array' : typeof bootstrapFlags,
      });
      const previousTranslationEnabled = state.bootstrap?.feature_flags?.translation_enabled;
      const normalizedBootstrap = normalizeBootstrapSnapshot(bootstrapPayload);
      trace('BOOTSTRAP_NORMALIZED_POLICY', {
        translation_enabled: normalizedBootstrap.feature_flags.translation_enabled,
        maintenance_mode: normalizedBootstrap.maintenance,
        policy_valid: true,
      });
      state.bootstrap = normalizedBootstrap;
      trace('CONTROL_PLANE_SNAPSHOT_APPLIED', {
        translation_enabled: state.bootstrap.feature_flags.translation_enabled,
        previous_translation_enabled: previousTranslationEnabled,
        snapshot_updated: true,
        resolved_at: state.bootstrap.translation_policy_resolved_at,
      });
      dispatchControlPlaneUpdated({bootstrap: state.bootstrap, wallet: state.wallet, progression: state.progression, license: state.license, device: state.device, authenticated: state.authenticated});
      trace('BOOTSTRAP_FEATURE_FLAGS', {
        translation_enabled: state.bootstrap.feature_flags.translation_enabled,
        maintenance_mode: state.bootstrap.maintenance,
        keys_present: Object.keys(state.bootstrap.feature_flags).slice(0, 20).join(','),
      });
      trace('CONTROL_PLANE_BOOTSTRAP_RESULT', {step: 'bootstrap', ok: true, status: 200, duration_ms: Date.now() - started});
      const bootstrapWallet = state.bootstrap?.wallet;
      trace('WALLET_FETCH_FINISHED', {source: 'beta-bootstrap', http_status: 200, ok: true, payload_present: Boolean(bootstrapWallet), top_level_keys: Object.keys(bootstrapWallet || {}).join(','), recognized_fields: WALLET_FIELDS.filter(key => Object.prototype.hasOwnProperty.call(bootstrapWallet || {}, key)).join(',')});
      let wallet = applyWallet(bootstrapWallet, 'beta-bootstrap');
      if (wallet.status !== 'ready') {
        trace('WALLET_FETCH_STARTED', {source: 'wallet-summary'});
        try {
          const summary = await call('wallet-summary');
          trace('WALLET_FETCH_FINISHED', {source: 'wallet-summary', http_status: 200, ok: true, payload_present: true, top_level_keys: Object.keys(summary || {}).join(','), recognized_fields: WALLET_FIELDS.filter(key => Object.prototype.hasOwnProperty.call(summary || {}, key)).join(',')});
          wallet = applyWallet(summary, 'wallet-summary');
        } catch (error) {
          trace('WALLET_FETCH_FINISHED', {source: 'wallet-summary', http_status: Number(error.status || 0), ok: false, payload_present: false});
        }
      }
      state.progression = state.bootstrap.rank || await call('progression-summary');
      state.license = state.bootstrap.license || state.bootstrap.licence || null;
      trace('CONTROL_PLANE_LICENSE_STATE', {step: 'license', license_present: Boolean(state.license), license_status: state.license?.status, license_id: state.license?.id || state.license?.license_id});
      if (!state.license || String(state.license.status || '').toLowerCase() !== 'active') throw Object.assign(new Error('license_not_found'), {code: 'license_not_found', status: 403});
      try { await loadPassiveAdsPreference(); } catch (_) { state.passiveAdsEnabled = false; window.__yomuPassiveAdsPreference = false; }
      await ensureDevice();
      state.ready = true;
      trace('CONTROL_PLANE_BOOTSTRAP_SUCCESS', {step: 'bootstrap', ok: true, duration_ms: Date.now() - started}); render(); startHeartbeat();
      return state.bootstrap;
    } catch (error) {
      trace('CONTROL_PLANE_BOOTSTRAP_RESULT', {step: 'bootstrap', ok: false, http_status: Number(error.status || 0), reason_code: String(error.code || 'server_unavailable').slice(0, 80), duration_ms: Date.now() - started});
      trace('CONTROL_PLANE_ERROR', {step: 'bootstrap', error_code: String(error.code || '').slice(0, 80), reason_code: String(error.message || '').slice(0, 120)});
      trace('CONTROL_PLANE_BOOTSTRAP_FAILED', {code: String(error.code || 'server_unavailable').slice(0, 80), status: Number(error.status || 0)});
      const status = document.querySelector('#controlPlaneStatus');
      if (status) { status.textContent = friendly(error.code); status.dataset.state = 'error'; }
      return null;
    } finally { state.inFlight = null; }
  })();
  return state.inFlight;
}
async function heartbeat() {
  if (!state.authenticated) return;
  trace('DEVICE_HEARTBEAT_STARTED', {step: 'heartbeat'});
  try { state.device = await call('device-heartbeat', state.device ? {device_uuid: state.device.device_uuid || state.device.id} : {}); trace('DEVICE_HEARTBEAT_RESULT', {step: 'heartbeat', ok: true, status: 200}); trace('CONTROL_PLANE_HEARTBEAT_SUCCESS'); }
  catch (error) { trace('DEVICE_HEARTBEAT_RESULT', {step: 'heartbeat', ok: false, http_status: Number(error.status || 0), reason_code: String(error.code || '').slice(0, 80)}); trace('CONTROL_PLANE_HEARTBEAT_FAILED', {code: String(error.code || '').slice(0, 80), status: Number(error.status || 0)}); if (['license_revoked','license_expired','device_not_found'].includes(error.code)) { state.license = {status: 'revoked'}; render(); } }
}
function nextDailyClaimText(claimDay = '') {
  const now = new Date();
  let next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  if (claimDay) {
    const parsed = new Date(`${String(claimDay).slice(0, 10)}T00:00:00Z`);
    if (!Number.isNaN(parsed.getTime())) next = new Date(parsed.getTime() + 86400000);
  }
  const remaining = Math.max(0, next.getTime() - now.getTime());
  const hours = Math.floor(remaining / 3600000);
  const minutes = Math.floor((remaining % 3600000) / 60000);
  return `Disponível novamente em ${hours}h ${minutes}min (UTC)`;
}
function renderDailyClaimStatus(result = {}) {
  const button = document.querySelector('#dailyClaimBtn');
  if (!button) return;
  if (result?.already_claimed === true) {
    button.textContent = nextDailyClaimText(result.claim_day);
    button.dataset.state = 'already-claimed';
    return;
  }
  if (result?.amount !== undefined) {
    button.textContent = Number(result.amount) > 0 ? 'Créditos de hoje recebidos' : 'Nenhum crédito diário disponível';
    button.dataset.state = 'claimed';
  }
}
async function dailyClaim(trigger = 'manual') {
  ykClaimAttemptCount += 1;
  ykTrace('YK_CLAIM_START', {trigger, claim_attempt_count: ykClaimAttemptCount});
  try {
    const result = await call('wallet-daily-claim');
    const r = result && typeof result === 'object' ? result : {};
    ykTrace('YK_CLAIM_RESULT', {ok: true, amount_granted: Number.isFinite(Number(r.amount)) ? Number(r.amount) : null,
      already_claimed: typeof r.already_claimed === 'boolean' ? r.already_claimed : null,
      next_available_at: r.next_available_at || r.next_claim_at || null,
      daily_target: Number.isFinite(Number(r.daily_yk_target)) ? Number(r.daily_yk_target) : null,
      claim_attempt_count: ykClaimAttemptCount});
    renderDailyClaimStatus(result);
    return result;
  } catch (error) {
    ykTrace('YK_CLAIM_RESULT', {ok: false, error_code: String(error?.code || 'claim_failed').slice(0, 80), claim_attempt_count: ykClaimAttemptCount});
    throw error;
  }
}
async function reserve(jobId, requestId = '') {
  return call('wallet-reserve', {
    job_id: String(jobId || ''), request_id: String(requestId || ''),
    device_id: String(state.device?.device_id || state.device?.device_uuid || ''),
  });
}
async function finalize(reservationId, release = false) { return call('wallet-finalize', {reservation_id: String(reservationId || ''), release: Boolean(release)}); }
async function executeTranslation(payload = {}) {
  // The desktop never contacts DeepL: this is the only production translation
  // transport and the provider key remains inside translation-execute.
  return call('translation-execute', payload);
}
async function translationPreflight(payload = {}) {
  if (state.translationInFlight) return state.translationInFlight;
  const jobId = String(payload.job_id || `preflight-${Date.now()}`), requestId = String(payload.request_id || `preflight-${Date.now()}`);
  state.translationInFlight = (async () => {
    let reservation = null;
    try {
      reservation = await reserve(jobId, requestId);
      const result = await executeTranslation({...payload, job_id: jobId, request_id: requestId, reservation_id: reservation.reservation_id, device_id: state.device?.device_id || state.device?.device_uuid});
      if (String(result?.code || '') === 'translation_disabled' || result?.status === 'translation_disabled') {
        await finalize(reservation.reservation_id, true);
        return {status: 'translation_disabled', released: true};
      }
      return result;
    } catch (error) {
      if (reservation?.reservation_id) { try { await finalize(reservation.reservation_id, true); } catch (_) {} }
      throw error;
    } finally { state.translationInFlight = null; }
  })();
  return state.translationInFlight;
}
function startHeartbeat() { if (state.heartbeatTimer) return; state.heartbeatTimer = window.setInterval(heartbeat, 120000); }
function stopHeartbeat() { if (state.heartbeatTimer) window.clearInterval(state.heartbeatTimer); state.heartbeatTimer = 0; }
window.__yomuControlPlane = {state, call, callRpc, bootstrap, heartbeat, dailyClaim, renderDailyClaimStatus, nextDailyClaimText, reserve, finalize, executeTranslation, translationPreflight, ensureDevice, loadPassiveAdsPreference, setPassiveAdsPreference, friendly, normalizeWalletState, applyWallet, render, trace};
window.addEventListener('tradutor-ui-policy-listener-ready', () => {
  trace('CONTROL_PLANE_UI_LISTENER_READY_RECEIVED', {event_name: 'tradutor-ui-policy-listener-ready', window_realm_id: WINDOW_REALM_ID, is_top_window: window.top === window, pathname: window.location?.pathname || '/'});
  if (state.bootstrap?.feature_flags && typeof state.bootstrap.feature_flags.translation_enabled === 'boolean') {
    dispatchControlPlaneUpdated({bootstrap: state.bootstrap, wallet: state.wallet, progression: state.progression, license: state.license, device: state.device, authenticated: state.authenticated});
  }
});
window.dispatchEvent(new CustomEvent('tradutor-control-plane-ready'));
document.querySelector('#controlPlaneWallet')?.addEventListener('click', () => window.dispatchEvent(new CustomEvent('tradutor-navigate-tab', {detail: {tab: 'rewards'}})));
document.querySelector('#controlPlaneRank')?.addEventListener('click', () => window.dispatchEvent(new CustomEvent('tradutor-navigate-tab', {detail: {tab: 'rewards'}})));
window.addEventListener('tradutor-navigate-tab', event => { if (event.detail?.tab === 'rewards' && state.authenticated) void bootstrap(); });
document.querySelector('#dailyClaimBtn')?.addEventListener('click', async (event) => {
  const button = event.currentTarget; if (button.dataset.busy === '1') return; button.dataset.busy = '1'; button.disabled = true;
  if (state.passiveAdsEnabled !== true) { button.textContent = 'Ative os anúncios passivos para resgatar'; button.dataset.state = 'ads-off'; button.disabled = false; button.dataset.busy = '0'; return; }
  try { const result = await dailyClaim(); applyWallet(await call('wallet-summary'), 'wallet-summary'); render(); renderDailyClaimStatus(result); }
  catch (error) { button.textContent = friendly(error.code); button.dataset.state = String(error.code || 'error'); }
  finally { button.dataset.busy = '0'; button.disabled = false; }
});
window.addEventListener('tradutor-auth-changed', event => {
  trace('AUTH_CHANGED_EVENT_RECEIVED', {step: 'auth_event', authenticated: String(event.detail?.state || '') === 'authenticated', user_id: event.detail?.user_id});
  const nextAuthenticated = String(event.detail?.state || '') === 'authenticated';
  const authTransition = nextAuthenticated && !state.authenticated;
  state.authenticated = nextAuthenticated;
  if (authTransition) {
    const generation = Number(window.__yomuAuthGeneration || 0) + 1;
    window.__yomuAuthGeneration = generation;
    window.__yomuPendingAuthBootstrapGeneration = generation;
    trace('CONTROL_PLANE_AUTH_TRANSITION', {authenticated: true, auth_generation: generation});
  }
  if (state.authenticated) {
    trace('CONTROL_PLANE_UI_AUTH_NOTIFY_START', {step: 'auth_bridge', authenticated: true});
    void bootstrap().finally(() => {
      try {
        const refreshProfile = window.__tradutorRefreshBootstrap;
        if (typeof refreshProfile === 'function') {
          const generation = Number(window.__yomuPendingAuthBootstrapGeneration || 0);
          window.__yomuPendingAuthBootstrapGeneration = 0;
          refreshProfile();
          trace('CONTROL_PLANE_UI_AUTH_NOTIFY_RESULT', {step: 'auth_bridge', ok: true, auth_generation: generation});
        } else {
          trace('CONTROL_PLANE_UI_AUTH_NOTIFY_DEFERRED', {step: 'auth_bridge', pending: true, auth_generation: Number(window.__yomuPendingAuthBootstrapGeneration || 0), reason: 'ui_bridge_not_ready'});
        }
      } catch (_) { /* profile refresh is best effort */ }
    });
  }
   else { stopHeartbeat(); state.bootstrap = state.wallet = state.progression = state.license = state.device = null; state.passiveAdsEnabled = false; window.__yomuPassiveAdsPreference = false; state.walletLoaded = false; state.walletStatus = 'loading'; window.dispatchEvent(new CustomEvent('yomu-passive-ads-preference-changed', {detail: {enabled: false}})); render(); }
});
if (window.__tradutorAuthState === 'authenticated') { trace('AUTH_RESTORED_SESSION_DETECTED', {step: 'auth_restore', authenticated: true}); void bootstrap(); }
