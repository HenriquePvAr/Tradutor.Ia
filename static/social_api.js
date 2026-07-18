// Frontend client for the authenticated social backend.
//
// The browser NEVER calls the Supabase Data API directly: every function here hits only
// the backend under /api/community/social, which forwards the user's JWT to PostgREST
// under RLS. The access token comes from the existing auth layer (the Supabase SDK, used
// solely for auth/session) and is attached as a Bearer by the shared api() wrapper — the
// token is never logged and the publishable key is never sent from here. Ownership and
// identity fields (owner_id/author_id/user_id/reporter_id/recipient_id/role/admin) are
// never sent; the backend derives them from the validated principal.
import { currentAccessToken } from '/static/supabase_auth.js';

const BASE = '/api/community/social';

// Fields a client must never send; the backend rejects them too (defense in depth).
const FORBIDDEN = new Set([
  'owner_id', 'author_id', 'user_id', 'reporter_id', 'recipient_id',
  'id', 'role', 'roles', 'admin', 'moderator', 'actor_id', 'status',
  'created_at', 'deleted_at', 'published_at', 'edited_at',
]);

export class SocialApiError extends Error {
  constructor(status, code) {
    super(code || `social_error_${status}`);
    this.status = status;
    this.code = code || '';
  }
}

// User-facing messages by status — never technical (no RLS/SQL/JWT wording).
const MESSAGES = {
  401: 'Sua sessão expirou. Entre novamente.',
  403: 'Você não tem acesso a este conteúdo.',
  404: 'Este conteúdo não está disponível.',
  409: 'Este nome já está em uso.',
  422: 'Revise os campos informados.',
  429: 'Muitas tentativas. Aguarde um momento.',
  503: 'A comunidade está temporariamente indisponível.',
};

export function messageForError(err) {
  if (err instanceof SocialApiError && MESSAGES[err.status]) return MESSAGES[err.status];
  return 'Não foi possível concluir a ação. Tente novamente.';
}

function sanitizeBody(body) {
  if (body == null) return undefined;
  const clean = {};
  for (const [k, v] of Object.entries(body)) {
    if (FORBIDDEN.has(k)) continue; // never let a client choose ownership/identity/status
    clean[k] = v;
  }
  return clean;
}

// One request. Attaches the Bearer from the auth layer; on a missing token it fails as
// 401 without calling the backend. Never throws the raw response text to the UI.
async function request(method, path, { body, signal } = {}) {
  const token = await currentAccessToken();
  if (!token) throw new SocialApiError(401, 'authentication_required');
  const init = {
    method,
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    credentials: 'same-origin',
    signal,
  };
  if (body !== undefined) init.body = JSON.stringify(sanitizeBody(body));
  const resp = await fetch(`${BASE}${path}`, init);
  let payload = null;
  try { payload = await resp.json(); } catch (_) { /* empty */ }
  if (!resp.ok) {
    const code = payload && typeof payload.detail === 'string' ? payload.detail : '';
    throw new SocialApiError(resp.status, code);
  }
  return payload;
}

function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === '') continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

const enc = encodeURIComponent;

// ---- profiles ----
export const getMyProfile = (o) => request('GET', '/profile/me', o);
export const updateMyProfile = (fields, o) => request('PATCH', '/profile/me', { ...o, body: fields });
export const getProfileByUsername = (username, o) => request('GET', `/profiles/${enc(username)}`, o);

// ---- works ----
export const getFeed = ({ cursor, limit } = {}, o) => request('GET', `/feed${qs({ cursor, limit })}`, o);
export const getMyWorks = ({ cursor, limit } = {}, o) => request('GET', `/my-works${qs({ cursor, limit })}`, o);
export const getWork = (workId, o) => request('GET', `/works/${enc(workId)}`, o);
export const createWork = (fields, o) => request('POST', '/works', { ...o, body: fields });
export const updateWork = (workId, fields, o) => request('PATCH', `/works/${enc(workId)}`, { ...o, body: fields });
export const deleteWork = (workId, o) => request('DELETE', `/works/${enc(workId)}`, o);

// ---- chapters ----
export const getWorkChapters = (workId, { cursor, limit } = {}, o) =>
  request('GET', `/works/${enc(workId)}/chapters${qs({ cursor, limit })}`, o);
export const getChapter = (chapterId, o) => request('GET', `/chapters/${enc(chapterId)}`, o);
export const createChapter = (workId, fields, o) => request('POST', `/works/${enc(workId)}/chapters`, { ...o, body: fields });
export const updateChapter = (chapterId, fields, o) => request('PATCH', `/chapters/${enc(chapterId)}`, { ...o, body: fields });
export const deleteChapter = (chapterId, o) => request('DELETE', `/chapters/${enc(chapterId)}`, o);

// ---- comments ----
export const getComments = (chapterId, { cursor, limit } = {}, o) =>
  request('GET', `/chapters/${enc(chapterId)}/comments${qs({ cursor, limit })}`, o);
export const createComment = (chapterId, { content, parent_id } = {}, o) =>
  request('POST', `/chapters/${enc(chapterId)}/comments`, { ...o, body: { content, parent_id } });
export const updateComment = (commentId, content, o) =>
  request('PATCH', `/comments/${enc(commentId)}`, { ...o, body: { content } });
export const deleteComment = (commentId, o) => request('DELETE', `/comments/${enc(commentId)}`, o);

// ---- likes (idempotent) ----
export const likeChapter = (chapterId, o) => request('PUT', `/chapters/${enc(chapterId)}/like`, o);
export const unlikeChapter = (chapterId, o) => request('DELETE', `/chapters/${enc(chapterId)}/like`, o);
export const likeComment = (commentId, o) => request('PUT', `/comments/${enc(commentId)}/like`, o);
export const unlikeComment = (commentId, o) => request('DELETE', `/comments/${enc(commentId)}/like`, o);

// ---- favorites ----
export const getFavorites = ({ cursor, limit } = {}, o) => request('GET', `/favorites${qs({ cursor, limit })}`, o);
export const favoriteWork = (workId, o) => request('PUT', `/works/${enc(workId)}/favorite`, o);
export const unfavoriteWork = (workId, o) => request('DELETE', `/works/${enc(workId)}/favorite`, o);

// ---- reading history ----
export const getHistory = ({ cursor, limit } = {}, o) => request('GET', `/history${qs({ cursor, limit })}`, o);
export const updateHistory = (chapterId, { progress_value, last_position, completed } = {}, o) =>
  request('PUT', `/chapters/${enc(chapterId)}/history`, { ...o, body: { progress_value, last_position, completed } });

// ---- reports ----
export const createReport = ({ target_type, target_id, reason, details } = {}, o) =>
  request('POST', '/reports', { ...o, body: { target_type, target_id, reason, details } });
export const getMyReports = ({ cursor, limit } = {}, o) => request('GET', `/reports/my${qs({ cursor, limit })}`, o);

// ---- notifications ----
export const getNotifications = ({ cursor, limit } = {}, o) => request('GET', `/notifications${qs({ cursor, limit })}`, o);
export const markNotificationRead = (id, o) => request('PATCH', `/notifications/${enc(id)}/read`, o);

// ---- explicit PDF publishing + asset lifecycle ----
export const listLocalResults = (o) => request('GET', '/local-pdf-results', o);
export const publishPdf = (chapterId, { source_job_id, target_status } = {}, o) =>
  request('POST', `/chapters/${enc(chapterId)}/publish-pdf`, { ...o, body: { source_job_id, target_status } });
export const publishStatus = (chapterId, o) => request('GET', `/chapters/${enc(chapterId)}/publish-status`, o);
export const getAsset = (chapterId, o) => request('GET', `/chapters/${enc(chapterId)}/asset`, o);
export const replaceAsset = (chapterId, source_job_id, o) =>
  request('POST', `/chapters/${enc(chapterId)}/asset/replace`, { ...o, body: { source_job_id } });
export const unlinkAsset = (chapterId, o) => request('DELETE', `/chapters/${enc(chapterId)}/asset`, o);

// Authenticated PDF fetch → object URL for the reader. The Bearer travels in the header
// (never the URL); the browser gets an in-memory blob URL, not a Google Drive link.
export async function fetchChapterPdfUrl(chapterId, signal) {
  const token = await currentAccessToken();
  if (!token) throw new SocialApiError(401, 'authentication_required');
  const resp = await fetch(`${BASE}/chapters/${enc(chapterId)}/content`, {
    headers: { 'Authorization': `Bearer ${token}` }, credentials: 'same-origin', signal,
  });
  if (!resp.ok) throw new SocialApiError(resp.status, '');
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}
