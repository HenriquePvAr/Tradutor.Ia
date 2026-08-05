// Authenticated community UI. Consumes ONLY the backend social endpoints via social_api;
// never the Supabase Data API directly. All user content is rendered with textContent /
// safe DOM building — no innerHTML with user data — so titles, bios, comments etc. cannot
// inject markup. Owner-only controls are hidden for non-owners, but the backend + RLS
// remain the authority (hiding a button is never the security boundary).
import * as api from '/static/social_api.js';
// Through the provider layer, never the Supabase module directly. This module is
// injected into every shell, so its static import decided what the browser
// fetched before anyone authenticated; the Supabase module pulls its SDK from a
// CDN, which reached a third party even under the local_test provider. The
// provider layer resolves the same helpers from whichever provider the backend
// reports, and imports the Supabase module only when that is the answer.
import { getSupabaseClient, onAuthChange, signOut } from '/static/auth_provider.js';

const TABS = [
  { id: 'explore', label: 'Explorar' },
  { id: 'favorites', label: 'Favoritos' },
  { id: 'reading', label: 'Continuar lendo' },
  { id: 'mine', label: 'Minhas obras' },
  { id: 'profile', label: 'Meu perfil' },
  { id: 'notifications', label: 'Notificações' },
];

const state = {
  session: null,
  profile: null,
  tab: 'explore',
  openWorkId: null,
  loading: false,
  abort: null,
  // True while a real SDK session arrived but the canonical (backend-confirmed)
  // auth state has not caught up yet. See waitForCanonicalAuthReady() below.
  pendingAuth: false,
};

// ---- safe DOM helpers (no innerHTML with user content) ----
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = String(opts.text); // escaping is automatic
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  if (opts.on) for (const [ev, fn] of Object.entries(opts.on)) node.addEventListener(ev, fn);
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
}

function toast(msg, kind = 'ok') {
  if (typeof window.__tradutorToast === 'function') { window.__tradutorToast(msg, kind); return; }
  const live = document.getElementById('socialLive');
  if (live) live.textContent = msg;
}

function fail(err) {
  toast(api.messageForError(err), 'err');
  if (err instanceof api.SocialApiError && err.status === 401) handleExpired();
}

function handleExpired() {
  if (state.abort) state.abort.abort();
  state.profile = null;
  state.openWorkId = null;
  toast('Sua sessão expirou. Entre novamente.', 'err');
  render();
}

function root() { return document.getElementById('view-community'); }

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString('pt-BR');
}

function statusLabel(s) {
  return { draft: 'rascunho', private: 'privado', community: 'comunidade', archived: 'arquivada' }[s] || s;
}

function isOwner(row) {
  return state.profile && row && row.owner_id && row.owner_id === state.profile.id;
}

// ---- shell ----
function render() {
  const host = root();
  if (!host) return;
  host.replaceChildren();
  if (state.pendingAuth) { host.appendChild(sectionShell(null).wrap); return; }
  if (!state.session) { host.appendChild(loginGate()); return; }
  host.appendChild(header());
  const body = el('div', { class: 'sc-body', attrs: { id: 'scBody' } });
  host.appendChild(body);
  renderTab(body);
}

function loginGate() {
  return el('div', { class: 'sc-gate' }, [
    el('div', { class: 'sc-gate-mark', text: '共' }),
    el('h2', { text: 'Comunidade' }),
    el('p', { class: 'sc-gate-sub', text: 'Entre para explorar obras, comentar e acompanhar sua leitura.' }),
    el('div', { class: 'sc-gate-actions' }, [
      el('button', { class: 'btn-primary', text: 'Entrar', on: { click: () => openAuth('login') } }),
      el('button', { class: 'btn-ghost', text: 'Criar conta', on: { click: () => openAuth('signup') } }),
    ]),
  ]);
}

function openAuth(mode) {
  // Reuse the existing full-page auth surface wired by auth_ui.js.
  document.querySelector(`.auth-tab[data-authmode="${mode}"]`)?.click();
  document.getElementById('authOpenBtn')?.click();
}

function header() {
  const displayName = String(window.__tradutorDisplayName || state.profile?.display_name || 'Usuário').trim() || 'Usuário';
  const nav = el('nav', { class: 'sc-nav', attrs: { 'aria-label': 'Comunidade' } });
  for (const t of TABS) {
    nav.appendChild(el('button', {
      class: `sc-tab${state.tab === t.id ? ' active' : ''}`, text: t.label,
      attrs: { 'aria-current': state.tab === t.id ? 'page' : 'false', 'data-tab': t.id },
      on: { click: () => { state.tab = t.id; state.openWorkId = null; render(); } },
    }));
  }
  return el('header', { class: 'sc-head' }, [
    el('div', { class: 'sc-head-top' }, [
      el('span', { class: 'sc-title', text: 'Comunidade' }),
      el('span', { class: 'sc-user', text: displayName, attrs: { title: displayName } }),
      el('button', { class: 'btn-ghost sc-logout', text: 'Sair', on: { click: doLogout } }),
    ]),
    nav,
  ]);
}

async function doLogout() {
  await signOut();
  // Drop the subscription and every cached fragment of the session, then re-arm the
  // mount so a later login builds one clean context (never a second, stacked one).
  unmountCommunity();
  render();
  toast('Você saiu da comunidade.', 'ok');
  await boot();
}

function sectionShell(title) {
  const wrap = el('section', { class: 'sc-section' });
  if (title) wrap.appendChild(el('h3', { class: 'sc-section-title', text: title }));
  const list = el('div', { class: 'sc-list', attrs: { 'aria-busy': 'true' } });
  wrap.appendChild(list);
  return { wrap, list };
}

function skeleton(n = 3) {
  const f = document.createDocumentFragment();
  for (let i = 0; i < n; i++) f.appendChild(el('div', { class: 'sc-skel' }));
  return f;
}

function empty(msg) { return el('div', { class: 'sc-empty', text: msg }); }

function errorBox(onRetry) {
  return el('div', { class: 'sc-error' }, [
    el('span', { text: 'Não foi possível carregar.' }),
    el('button', { class: 'btn-ghost', text: 'Tentar de novo', on: { click: onRetry } }),
  ]);
}

function loadMore(onClick) {
  return el('button', { class: 'btn-ghost sc-more', text: 'Carregar mais', on: { click: onClick } });
}

// ---- tab dispatch ----
function renderTab(body) {
  if (state.openWorkId) return renderWork(body, state.openWorkId);
  switch (state.tab) {
    case 'explore': return renderFeed(body);
    case 'favorites': return renderFavorites(body);
    case 'reading': return renderHistory(body);
    case 'mine': return renderMine(body);
    case 'profile': return renderProfile(body);
    case 'notifications': return renderNotifications(body);
  }
}

// A generic paginated list renderer with loading/empty/error/more, backed by a per-section
// cache on the current mount (see sectionState/invalidateSection below): opening a tab
// whose section is already 'loaded' or 'error' renders from that cache with zero requests;
// only 'idle' (first visit, or after an explicit invalidation) fetches. Two concurrent
// callers for the same section share the same in-flight promise instead of duplicating it.
async function paginated(body, title, fetchPage, renderItem, emptyMsg, sectionKey) {
  const { wrap, list } = sectionShell(title);
  body.appendChild(wrap);
  const sec = sectionState(sectionKey);

  function renderItems() {
    list.replaceChildren();
    list.removeAttribute('aria-busy');
    if (!sec.items.length) { list.appendChild(empty(emptyMsg)); }
    else for (const it of sec.items) list.appendChild(renderItem(it));
    const old = wrap.querySelector('.sc-more'); if (old) old.remove();
    if (sec.cursor) wrap.appendChild(loadMore(loadPage));
  }

  function renderError() {
    list.replaceChildren(errorBox(() => { sec.status = 'idle'; sec.error = null; list.replaceChildren(skeleton()); loadPage(); }));
  }

  async function loadPage() {
    if (sec.promise) return sec.promise; // an equivalent load is already in flight — reuse it
    const active = mount, key = active && active.sessionKey;
    sec.status = 'loading';
    sec.promise = fetchPage(sec.cursor).then(
      (page) => {
        sec.promise = null;
        if (active && (mount !== active || active.sessionKey !== key)) return; // stale session
        const items = (page && page.items) || [];
        sec.items = sec.items.concat(items);
        sec.cursor = (page && page.next_cursor) || '';
        sec.status = 'loaded'; sec.error = null;
        renderItems();
      },
      (err) => {
        sec.promise = null;
        if (active && (mount !== active || active.sessionKey !== key)) return; // stale session
        sec.status = 'error'; sec.error = err;
        renderError();
        if (err instanceof api.SocialApiError && err.status === 401) handleExpired();
      },
    );
    return sec.promise;
  }

  if (sec.status === 'loaded') { renderItems(); return; }
  if (sec.status === 'error') { renderError(); return; }
  list.appendChild(skeleton());
  loadPage();
}

// ---- feed ----
function workCard(w, { showFav = true } = {}) {
  const card = el('article', { class: 'sc-card', attrs: { tabindex: '0', role: 'button' },
    on: { click: () => openWork(w.id), keydown: (e) => { if (e.key === 'Enter') openWork(w.id); } } });
  card.appendChild(el('div', { class: 'sc-cover', text: (w.title || '?').slice(0, 1).toUpperCase() }));
  const meta = el('div', { class: 'sc-card-meta' }, [
    el('div', { class: 'sc-card-title', text: w.title || 'Sem título' }),
    w.synopsis ? el('div', { class: 'sc-card-syn', text: w.synopsis }) : null,
    el('div', { class: 'sc-card-foot' }, [
      el('span', { class: 'sc-badge', text: statusLabel(w.status) }),
      w.published_at ? el('span', { class: 'sc-date', text: fmtDate(w.published_at) }) : null,
    ]),
  ]);
  card.appendChild(meta);
  if (showFav) {
    const favBtn = el('button', { class: 'sc-fav', text: '☆', attrs: { 'aria-label': 'Favoritar', title: 'Favoritar' },
      on: { click: (e) => { e.stopPropagation(); toggleFavorite(w.id, favBtn); } } });
    card.appendChild(favBtn);
  }
  return card;
}

function renderFeed(body) {
  paginated(body, 'Explorar',
    (cursor) => api.getFeed({ cursor, limit: 20 }),
    (w) => workCard(w),
    'Nenhuma obra publicada ainda.', 'feed');
}

async function toggleFavorite(workId, btn) {
  if (btn.dataset.busy) return;
  btn.dataset.busy = '1';
  const wasFav = btn.dataset.fav === '1';
  btn.textContent = wasFav ? '☆' : '★'; // optimistic
  btn.dataset.fav = wasFav ? '' : '1';
  try {
    if (wasFav) await api.unfavoriteWork(workId); else await api.favoriteWork(workId);
    invalidateSection('favorites');
  } catch (err) {
    btn.textContent = wasFav ? '★' : '☆'; // rollback
    btn.dataset.fav = wasFav ? '1' : '';
    fail(err);
  } finally { delete btn.dataset.busy; }
}

// ---- work page ----
function openWork(id) { state.openWorkId = id; render(); }

async function renderWork(body, workId) {
  const back = el('button', { class: 'btn-ghost sc-back', text: '← Voltar',
    on: { click: () => { state.openWorkId = null; render(); } } });
  body.appendChild(back);
  const container = el('div', { class: 'sc-work', attrs: { 'aria-busy': 'true' } });
  container.appendChild(skeleton(2));
  body.appendChild(container);
  try {
    const w = await api.getWork(workId);
    container.replaceChildren();
    container.removeAttribute('aria-busy');
    const owner = isOwner(w);
    container.appendChild(el('div', { class: 'sc-work-head' }, [
      el('div', { class: 'sc-cover sc-cover-lg', text: (w.title || '?').slice(0, 1).toUpperCase() }),
      el('div', {}, [
        el('h2', { class: 'sc-work-title', text: w.title || 'Sem título' }),
        w.synopsis ? el('p', { class: 'sc-work-syn', text: w.synopsis }) : null,
        el('div', { class: 'sc-card-foot' }, [
          owner ? el('span', { class: 'sc-badge', text: statusLabel(w.status) }) : null,
          w.published_at ? el('span', { class: 'sc-date', text: fmtDate(w.published_at) }) : null,
        ]),
      ]),
    ]));
    // actions
    const actions = el('div', { class: 'sc-actions' });
    const fav = el('button', { class: 'btn-ghost', text: '☆ Favoritar',
      on: { click: () => api.favoriteWork(w.id).then(() => { invalidateSection('favorites'); toast('Adicionado aos favoritos.'); }).catch(fail) } });
    actions.appendChild(fav);
    actions.appendChild(el('button', { class: 'btn-ghost', text: 'Denunciar',
      on: { click: () => reportModal('work', w.id) } }));
    if (owner) {
      actions.appendChild(el('button', { class: 'btn-ghost', text: 'Editar', on: { click: () => workForm(w) } }));
      actions.appendChild(el('button', {
        class: 'btn-ghost', text: w.status === 'community' ? 'Despublicar' : 'Publicar',
        on: { click: () => togglePublish(w) } }));
      actions.appendChild(el('button', { class: 'btn-ghost sc-danger', text: 'Excluir',
        on: { click: () => confirmDelete('obra', () => api.deleteWork(w.id).then(() => { invalidateSection('mine'); invalidateSection('feed'); toast('Obra excluída.'); state.openWorkId = null; render(); }).catch(fail)) } }));
      actions.appendChild(el('button', { class: 'btn-primary', text: 'Novo capítulo', on: { click: () => chapterForm(w.id) } }));
    }
    container.appendChild(actions);
    // chapters
    const chWrap = el('div', { class: 'sc-chapters' });
    container.appendChild(el('h3', { class: 'sc-section-title', text: 'Capítulos' }));
    container.appendChild(chWrap);
    loadChapters(chWrap, w, owner);
  } catch (err) {
    container.replaceChildren(errorBox(() => render()));
    if (err instanceof api.SocialApiError && err.status === 401) handleExpired();
  }
}

async function loadChapters(host, work, owner) {
  host.replaceChildren(skeleton(2));
  try {
    const page = await api.getWorkChapters(work.id, { limit: 50 });
    host.replaceChildren();
    const items = (page && page.items) || [];
    if (!items.length) { host.appendChild(empty('Nenhum capítulo ainda.')); return; }
    for (const c of items) host.appendChild(chapterRow(c, work, owner));
  } catch (err) { host.replaceChildren(errorBox(() => loadChapters(host, work, owner))); if (err.status === 401) handleExpired(); }
}

function chapterRow(c, work, owner) {
  const row = el('div', { class: 'sc-chapter' });
  row.appendChild(el('div', { class: 'sc-chapter-main' }, [
    el('span', { class: 'sc-chapter-num', text: `#${c.chapter_number}` }),
    el('span', { class: 'sc-chapter-title', text: c.title || 'Sem título' }),
    owner ? el('span', { class: 'sc-badge sc-badge-sm', text: statusLabel(c.status) }) : null,
  ]));
  const note = el('div', { class: 'sc-chapter-note' });
  if (owner) row.appendChild(note);
  const acts = el('div', { class: 'sc-chapter-acts' });
  acts.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Comentários', on: { click: () => commentsModal(c) } }));
  const likeBtn = el('button', { class: 'btn-ghost btn-sm sc-like', text: '♡', attrs: { 'aria-label': 'Curtir capítulo' },
    on: { click: () => toggleLike('chapter', c.id, likeBtn) } });
  acts.appendChild(likeBtn);
  const assetActs = el('span', { class: 'sc-asset-acts' });
  acts.appendChild(assetActs);
  if (owner) {
    acts.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Editar', on: { click: () => chapterForm(work.id, c) } }));
    acts.appendChild(el('button', { class: 'btn-ghost btn-sm sc-danger', text: 'Excluir',
      on: { click: () => confirmDelete('capítulo', () => api.deleteChapter(c.id).then(() => { toast('Capítulo excluído.'); render(); }).catch(fail)) } }));
  }
  row.appendChild(acts);
  // Asset state drives the read/publish controls; fetched lazily per row.
  api.getAsset(c.id).then((asset) => renderAssetControls(assetActs, note, c, work, owner, asset)).catch(() => {});
  return row;
}

function confirmRestore(c) {
  if (!window.confirm('Restaurar este PDF retido como arquivo ativo do capítulo?')) return;
  api.restoreAsset(c.id)
    .then(() => { toast('PDF restaurado.'); render(); })
    .catch((e) => fail(e && e.status === 409
      ? new Error('O capítulo já tem um PDF ativo. Desvincule-o antes de restaurar.') : e));
}

export function retainedAssetsPanel(host) {
  host.replaceChildren();
  // Owner-only listing; the DTO carries no Drive id, no path and no storage id.
  api.retainedAssets().then((r) => {
    const items = (r && r.items) || [];
    if (!items.length) { host.appendChild(el('p', { class: 'sc-muted', text: 'Nenhum arquivo retido.' })); return; }
    items.forEach((it) => {
      const row = el('div', { class: 'sc-retained-row' });
      row.appendChild(el('span', { text: `Capítulo ${it.chapter_id} — ${it.reason}` }));
      row.appendChild(el('span', { class: 'sc-muted', text: `expira em ${it.days_remaining} dia(s)` }));
      if (it.restorable) {
        row.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Restaurar',
          on: { click: () => confirmRestore({ id: it.chapter_id }) } }));
      }
      host.appendChild(row);
    });
  }).catch(fail);
}

function renderAssetControls(host, note, c, work, owner, asset) {
  host.replaceChildren();
  const available = asset && asset.available;
  if (available) {
    host.appendChild(el('button', { class: 'btn-primary btn-sm', text: 'Ler', on: { click: () => readerModal(c) } }));
    if (owner) {
      note.textContent = '';
      host.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Substituir PDF', on: { click: () => publishModal(c, 'replace') } }));
      host.appendChild(el('button', { class: 'btn-ghost btn-sm sc-danger', text: 'Desvincular',
        on: { click: () => confirmDelete('vínculo do PDF', () => api.unlinkAsset(c.id).then(() => { toast('PDF desvinculado. O arquivo fica retido e pode ser restaurado.'); render(); }).catch(fail)) } }));
    }
  } else if (owner) {
    note.textContent = 'Arquivo ainda não vinculado';
    host.appendChild(el('button', { class: 'btn-primary btn-sm', text: 'Publicar PDF', on: { click: () => publishModal(c, 'publish') } }));
    // A retained (replaced/unlinked) PDF can be brought back while the window is open.
    api.assetRetention(c.id).then((r) => {
      if (!r || !r.restorable) return;
      note.textContent = `Arquivo retido — restauração disponível por ${r.days_remaining} dia(s)`;
      host.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Restaurar PDF',
        on: { click: () => confirmRestore(c) } }));
    }).catch(() => {});
  }
  // For a non-owner without an available asset: no controls, no internal details.
}

async function togglePublish(w) {
  const next = w.status === 'community' ? 'private' : 'community';
  try {
    await api.updateWork(w.id, { status: next });
    invalidateSection('mine'); invalidateSection('feed');
    toast(next === 'community' ? 'Obra publicada.' : 'Obra despublicada.'); render();
  } catch (err) { fail(err); }
}

async function toggleLike(kind, id, btn) {
  if (btn.dataset.busy) return; btn.dataset.busy = '1';
  const liked = btn.dataset.liked === '1';
  btn.textContent = liked ? '♡' : '♥'; btn.dataset.liked = liked ? '' : '1';
  try {
    if (kind === 'chapter') { liked ? await api.unlikeChapter(id) : await api.likeChapter(id); }
    else { liked ? await api.unlikeComment(id) : await api.likeComment(id); }
  } catch (err) { btn.textContent = liked ? '♥' : '♡'; btn.dataset.liked = liked ? '1' : ''; fail(err); }
  finally { delete btn.dataset.busy; }
}

// ---- my works ----
function renderMine(body) {
  const bar = el('div', { class: 'sc-bar' }, [
    el('button', { class: 'btn-primary', text: 'Nova obra', on: { click: () => workForm(null) } }),
  ]);
  body.appendChild(bar);
  paginated(body, 'Minhas obras',
    (cursor) => api.getMyWorks({ cursor, limit: 20 }),
    (w) => workCard(w, { showFav: false }),
    'Você ainda não criou obras.', 'mine');
}

// ---- favorites ----
function renderFavorites(body) {
  paginated(body, 'Favoritos',
    async (cursor) => {
      const page = await api.getFavorites({ cursor, limit: 20 });
      return page; // items are {user_id, work_id, created_at}
    },
    (f) => el('div', { class: 'sc-fav-row' }, [
      el('span', { class: 'sc-fav-id', text: 'Obra favoritada' }),
      el('button', { class: 'btn-ghost btn-sm', text: 'Abrir', on: { click: () => openWork(f.work_id) } }),
      el('button', { class: 'btn-ghost btn-sm sc-danger', text: 'Remover',
        on: { click: (e) => { const row = e.target.closest('.sc-fav-row'); api.unfavoriteWork(f.work_id).then(() => { invalidateSection('favorites'); row.remove(); toast('Removido dos favoritos.'); }).catch(fail); } } }),
    ]),
    'Nenhum favorito ainda.', 'favorites');
}

// ---- history ----
function renderHistory(body) {
  paginated(body, 'Continuar lendo',
    (cursor) => api.getHistory({ cursor, limit: 20 }),
    (h) => el('div', { class: 'sc-hist-row' }, [
      el('div', { class: 'sc-hist-bar' }, [ el('div', { class: 'sc-hist-fill', attrs: { style: `width:${Math.max(0, Math.min(100, Number(h.progress_value) || 0))}%` } }) ]),
      el('span', { class: 'sc-hist-pct', text: `${Math.round(Number(h.progress_value) || 0)}%` }),
      h.completed_at ? el('span', { class: 'sc-badge sc-badge-sm', text: 'concluído' }) : null,
      el('span', { class: 'sc-date', text: fmtDate(h.last_read_at) }),
    ]),
    'Você ainda não tem leituras registradas.', 'history');
}

// ---- profile ----
async function renderProfile(body) {
  const wrap = el('div', { class: 'sc-profile', attrs: { 'aria-busy': 'true' } });
  wrap.appendChild(skeleton(2));
  body.appendChild(wrap);
  try {
    // Reuses the load the mount already performed for this session; only an explicit
    // retry re-requests. Opening this tab therefore adds no second profile/me GET.
    const p = await loadMyProfile();
    wrap.replaceChildren();
    wrap.removeAttribute('aria-busy');
    // An authenticated account with no social profile yet is a valid, configurable
    // state — not an error. The backend answers it with an empty profile document.
    const needsOnboarding = !p.username || !p.display_name;
    if (needsOnboarding) wrap.appendChild(el('div', { class: 'sc-onboard', text: 'Complete seu perfil para participar melhor da comunidade.' }));
    wrap.appendChild(profileForm(p));
  } catch (err) {
    wrap.replaceChildren(errorBox(() => { loadMyProfile({ refresh: true }).catch(() => {}); render(); }));
    if (err.status === 401) handleExpired();
  }
}

function field(label, input, hint) {
  return el('label', { class: 'sc-field' }, [
    el('span', { class: 'sc-field-label', text: label }),
    input,
    hint ? el('span', { class: 'sc-field-hint', text: hint }) : null,
  ]);
}

function profileForm(p) {
  const username = el('input', { attrs: { type: 'text', maxlength: '32', value: p.username || '', 'aria-label': 'Nome de usuário' } });
  const display = el('input', { attrs: { type: 'text', maxlength: '60', value: p.display_name || '', 'aria-label': 'Nome de exibição' } });
  const bio = el('textarea', { attrs: { maxlength: '500', rows: '3', 'aria-label': 'Bio' } });
  bio.value = p.bio || '';
  const color = el('input', { attrs: { type: 'color', value: /^#[0-9a-fA-F]{6}$/.test(p.theme_color || '') ? p.theme_color : '#b8557a', 'aria-label': 'Cor do tema' } });
  const avatar = el('span', { class: 'sc-avatar', text: (p.display_name || p.username || '?').slice(0, 1).toUpperCase() });
  const setAvatarColor = () => { avatar.style.background = color.value; };
  setAvatarColor(); color.addEventListener('input', setAvatarColor);
  const showFav = checkbox('Mostrar favoritos no perfil', p.show_favorites);
  const showHist = checkbox('Mostrar histórico no perfil', p.show_history);
  const allowComments = checkbox('Permitir comentários no perfil', p.allow_profile_comments);
  const count = el('span', { class: 'sc-field-hint', text: `${(p.bio || '').length}/500` });
  bio.addEventListener('input', () => { count.textContent = `${bio.value.length}/500`; });
  const save = el('button', { class: 'btn-primary', text: 'Salvar perfil' });
  const form = el('form', { class: 'sc-profile-form', on: { submit: async (e) => {
    e.preventDefault();
    save.disabled = true; save.textContent = 'Salvando…';
    try {
      const fields = {
        username: username.value.trim() || null,
        display_name: display.value.trim() || null,
        bio: bio.value.trim() || null,
        theme_color: color.value,
        show_favorites: showFav.querySelector('input').checked,
        show_history: showHist.querySelector('input').checked,
        allow_profile_comments: allowComments.querySelector('input').checked,
      };
      const updated = await api.updateMyProfile(fields);
      state.profile = updated;
      if (mount) mount.profileRequest = Promise.resolve(updated); // keep the cache truthful
      toast('Perfil salvo.', 'ok');
    } catch (err) { fail(err); }
    finally { save.disabled = false; save.textContent = 'Salvar perfil'; }
  } } }, [
    el('div', { class: 'sc-profile-head' }, [avatar, el('div', {}, [field('Nome de usuário', username, 'letras minúsculas, números e _ (3–32)'), field('Nome de exibição', display)])]),
    field('Bio', bio), count,
    field('Cor do tema', color),
    showFav, showHist, allowComments,
    save,
  ]);
  return form;
}

function checkbox(label, checked) {
  const input = el('input', { attrs: { type: 'checkbox' } });
  input.checked = !!checked;
  return el('label', { class: 'sc-check' }, [input, el('span', { text: label })]);
}

// ---- notifications ----
function renderNotifications(body) {
  paginated(body, 'Notificações',
    (cursor) => api.getNotifications({ cursor, limit: 20 }),
    (n) => {
      const row = el('div', { class: `sc-notif${n.read_at ? '' : ' unread'}` }, [
        el('span', { class: 'sc-notif-type', text: n.notification_type || 'notificação' }),
        el('span', { class: 'sc-date', text: fmtDate(n.created_at) }),
      ]);
      if (!n.read_at) row.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Marcar como lida',
        on: { click: (e) => { api.markNotificationRead(n.id).then(() => { invalidateSection('notifications'); row.classList.remove('unread'); e.target.remove(); }).catch(fail); } } }));
      return row;
    },
    'Nenhuma notificação.', 'notifications');
}

// ---- modals (accessible: focus trap, Escape, focus return) ----
let lastFocus = null;
function modal(title, contentNodes) {
  lastFocus = document.activeElement;
  const overlay = el('div', { class: 'sc-modal-overlay show', attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title } });
  const card = el('div', { class: 'sc-modal-card' });
  const close = el('button', { class: 'sc-modal-close', text: '✕', attrs: { 'aria-label': 'Fechar' }, on: { click: destroy } });
  card.appendChild(close);
  card.appendChild(el('h3', { class: 'sc-modal-title', text: title }));
  for (const n of [].concat(contentNodes)) if (n) card.appendChild(n);
  overlay.appendChild(card);
  function destroy() { overlay.remove(); document.removeEventListener('keydown', onKey); if (lastFocus) lastFocus.focus(); }
  function onKey(e) {
    if (e.key === 'Escape') destroy();
    if (e.key === 'Tab') {
      const f = card.querySelectorAll('button, input, textarea, select, [tabindex]');
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  overlay.addEventListener('click', (e) => { if (e.target === overlay) destroy(); });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
  (card.querySelector('input, textarea, button') || close).focus();
  return { destroy, card };
}

function confirmDelete(kind, onConfirm) {
  const m = modal(`Excluir ${kind}?`, [
    el('p', { class: 'sc-modal-text', text: `Esta ação marca a ${kind} como excluída. Você pode confirmar abaixo.` }),
    el('div', { class: 'sc-modal-actions' }, [
      el('button', { class: 'btn-ghost', text: 'Cancelar', on: { click: () => m.destroy() } }),
      el('button', { class: 'btn-primary sc-danger', text: 'Excluir', on: { click: () => { m.destroy(); onConfirm(); } } }),
    ]),
  ]);
}

function workForm(existing) {
  const title = el('input', { attrs: { type: 'text', maxlength: '200', value: existing?.title || '', 'aria-label': 'Título' } });
  const slug = el('input', { attrs: { type: 'text', maxlength: '120', value: existing?.slug || '', 'aria-label': 'Slug', pattern: '[a-z0-9-]+' } });
  const syn = el('textarea', { attrs: { maxlength: '4000', rows: '4', 'aria-label': 'Sinopse' } });
  syn.value = existing?.synopsis || '';
  const submit = el('button', { class: 'btn-primary', text: existing ? 'Salvar' : 'Criar obra' });
  const form = el('form', {}, [
    field('Título', title), field('Slug', slug, 'letras minúsculas, números e hífen'), field('Sinopse', syn),
    el('div', { class: 'sc-modal-actions' }, [submit]),
  ]);
  const m = modal(existing ? 'Editar obra' : 'Nova obra', form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault(); submit.disabled = true;
    try {
      const fields = { title: title.value.trim(), slug: slug.value.trim(), synopsis: syn.value.trim() || null };
      if (existing) await api.updateWork(existing.id, fields); else await api.createWork(fields);
      invalidateSection('mine');
      m.destroy(); toast(existing ? 'Obra salva.' : 'Obra criada.'); render();
    } catch (err) { submit.disabled = false; fail(err); }
  });
}

function chapterForm(workId, existing) {
  const num = el('input', { attrs: { type: 'number', step: '0.01', min: '0.01', value: existing?.chapter_number || '', 'aria-label': 'Número' } });
  const title = el('input', { attrs: { type: 'text', maxlength: '200', value: existing?.title || '', 'aria-label': 'Título' } });
  const submit = el('button', { class: 'btn-primary', text: existing ? 'Salvar' : 'Criar capítulo' });
  const form = el('form', {}, [field('Número', num), field('Título', title), el('div', { class: 'sc-modal-actions' }, [submit])]);
  const m = modal(existing ? 'Editar capítulo' : 'Novo capítulo', form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault(); submit.disabled = true;
    try {
      const fields = { chapter_number: Number(num.value), title: title.value.trim() || null };
      if (existing) await api.updateChapter(existing.id, fields); else await api.createChapter(workId, fields);
      m.destroy(); toast(existing ? 'Capítulo salvo.' : 'Capítulo criado.'); render();
    } catch (err) { submit.disabled = false; fail(err); }
  });
}

function reportModal(targetType, targetId) {
  const reason = el('input', { attrs: { type: 'text', maxlength: '100', 'aria-label': 'Motivo' } });
  const details = el('textarea', { attrs: { maxlength: '2000', rows: '3', 'aria-label': 'Detalhes' } });
  const submit = el('button', { class: 'btn-primary', text: 'Enviar denúncia' });
  const form = el('form', {}, [field('Motivo', reason), field('Detalhes (opcional)', details), el('div', { class: 'sc-modal-actions' }, [submit])]);
  const m = modal('Denunciar conteúdo', form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault(); submit.disabled = true;
    try {
      await api.createReport({ target_type: targetType, target_id: targetId, reason: reason.value.trim(), details: details.value.trim() || null });
      m.destroy(); toast('Denúncia enviada. Obrigado.', 'ok');
    } catch (err) { submit.disabled = false; fail(err); }
  });
}

function commentsModal(chapter) {
  const list = el('div', { class: 'sc-comments', attrs: { 'aria-busy': 'true' } });
  const input = el('textarea', { attrs: { maxlength: '4000', rows: '2', 'aria-label': 'Escreva um comentário' } });
  const count = el('span', { class: 'sc-field-hint', text: '0/4000' });
  input.addEventListener('input', () => { count.textContent = `${input.value.length}/4000`; });
  const send = el('button', { class: 'btn-primary', text: 'Comentar' });
  const composer = el('form', { class: 'sc-composer' }, [input, count, send]);
  const m = modal(`Comentários — capítulo #${chapter.chapter_number}`, [list, composer]);
  list.appendChild(skeleton(2));
  async function reload() {
    try {
      const page = await api.getComments(chapter.id, { limit: 50 });
      list.replaceChildren(); list.removeAttribute('aria-busy');
      const items = (page && page.items) || [];
      if (!items.length) { list.appendChild(empty('Seja o primeiro a comentar.')); return; }
      for (const c of items) list.appendChild(commentRow(c, chapter, reload));
    } catch (err) { list.replaceChildren(errorBox(reload)); if (err.status === 401) handleExpired(); }
  }
  composer.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!input.value.trim()) return;
    send.disabled = true;
    try { await api.createComment(chapter.id, { content: input.value.trim() }); input.value = ''; count.textContent = '0/4000'; reload(); }
    catch (err) { fail(err); } finally { send.disabled = false; }
  });
  reload();
}

function commentRow(c, chapter, reload, depth = 0) {
  const mine = state.profile && c.author_id === state.profile.id;
  const row = el('div', { class: `sc-comment${depth ? ' sc-comment-reply' : ''}` });
  const textNode = c.is_deleted
    ? el('em', { class: 'sc-comment-removed', text: 'Comentário removido' })
    : el('div', { class: 'sc-comment-text', text: c.content || '' }); // textContent = XSS-safe
  row.appendChild(el('div', { class: 'sc-comment-head' }, [
    el('span', { class: 'sc-comment-author', text: mine ? 'Você' : 'Membro' }),
    c.edited_at && !c.is_deleted ? el('span', { class: 'sc-comment-edited', text: '(editado)' }) : null,
    el('span', { class: 'sc-date', text: fmtDate(c.created_at) }),
  ]));
  row.appendChild(textNode);
  const acts = el('div', { class: 'sc-comment-acts' });
  const likeBtn = el('button', { class: 'btn-ghost btn-sm sc-like', text: '♡', attrs: { 'aria-label': 'Curtir comentário' },
    on: { click: () => toggleLike('comment', c.id, likeBtn) } });
  acts.appendChild(likeBtn);
  if (!c.is_deleted && depth < 1) acts.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Responder',
    on: { click: () => replyBox(row, c, chapter, reload) } }));
  if (mine && !c.is_deleted) {
    acts.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Editar', on: { click: () => editBox(textNode, c, reload) } }));
    acts.appendChild(el('button', { class: 'btn-ghost btn-sm sc-danger', text: 'Apagar',
      on: { click: () => api.deleteComment(c.id).then(reload).catch(fail) } }));
  }
  if (!c.is_deleted) acts.appendChild(el('button', { class: 'btn-ghost btn-sm', text: 'Denunciar', on: { click: () => reportModal('comment', c.id) } }));
  row.appendChild(acts);
  return row;
}

function replyBox(after, parent, chapter, reload) {
  const ta = el('textarea', { attrs: { maxlength: '4000', rows: '2', 'aria-label': 'Sua resposta' } });
  const send = el('button', { class: 'btn-primary btn-sm', text: 'Responder' });
  const box = el('form', { class: 'sc-reply' }, [ta, send]);
  after.appendChild(box); ta.focus();
  box.addEventListener('submit', async (e) => {
    e.preventDefault(); if (!ta.value.trim()) return; send.disabled = true;
    try { await api.createComment(chapter.id, { content: ta.value.trim(), parent_id: parent.id }); reload(); }
    catch (err) { send.disabled = false; fail(err); }
  });
}

function editBox(textNode, c, reload) {
  const ta = el('textarea', { attrs: { maxlength: '4000', rows: '2', 'aria-label': 'Editar comentário' } });
  ta.value = c.content || '';
  const save = el('button', { class: 'btn-primary btn-sm', text: 'Salvar' });
  const box = el('form', { class: 'sc-reply' }, [ta, save]);
  textNode.replaceWith(box); ta.focus();
  box.addEventListener('submit', async (e) => {
    e.preventDefault(); save.disabled = true;
    try { await api.updateComment(c.id, ta.value.trim()); reload(); }
    catch (err) { save.disabled = false; fail(err); }
  });
}

// ---- explicit PDF publishing + reader ----
function publishModal(chapter, mode) {
  const isReplace = mode === 'replace';
  const list = el('div', { class: 'sc-pub-list', attrs: { 'aria-busy': 'true' } });
  list.appendChild(skeleton(2));
  let chosen = null;
  const status = el('div', { class: 'sc-pub-status' });
  const visibility = el('div', { class: 'sc-pub-visibility' });
  if (!isReplace) {
    visibility.appendChild(el('p', { class: 'sc-modal-text',
      text: 'Este PDF ainda está apenas no seu computador. Ao continuar, ele será enviado para o armazenamento privado e publicado conforme a visibilidade escolhida.' }));
    const priv = radio('publishVis', 'private', 'Privado — somente você poderá ler.', true);
    const comm = radio('publishVis', 'community', 'Comunidade — qualquer usuário autenticado poderá ler.', false);
    visibility.append(priv, comm);
  }
  const submit = el('button', { class: 'btn-primary', text: isReplace ? 'Substituir' : 'Publicar', attrs: { disabled: 'true' } });
  const m = modal(isReplace ? 'Substituir PDF' : 'Publicar na comunidade',
    [el('p', { class: 'sc-field-hint', text: 'Escolha um resultado local concluído:' }), list, visibility, status, el('div', { class: 'sc-modal-actions' }, [submit])]);
  api.listLocalResults().then((res) => {
    list.replaceChildren(); list.removeAttribute('aria-busy');
    const items = (res && res.items) || [];
    if (!items.length) { list.appendChild(empty('Nenhum resultado local publicável. Conclua uma tradução autenticada primeiro.')); return; }
    for (const r of items) {
      const opt = el('label', { class: 'sc-pub-opt' }, [
        el('input', { attrs: { type: 'radio', name: 'localResult', value: r.source_job_id } }),
        el('span', { text: `${r.title} · ${fmtDate(r.created_at ? r.created_at * 1000 : '')}` }),
      ]);
      opt.querySelector('input').addEventListener('change', () => { chosen = r.source_job_id; submit.removeAttribute('disabled'); });
      list.appendChild(opt);
    }
  }).catch((err) => { list.replaceChildren(errorBox(() => publishModal(chapter, mode))); if (err.status === 401) handleExpired(); });

  submit.addEventListener('click', async () => {
    if (!chosen || submit.dataset.busy) return;
    submit.dataset.busy = '1'; submit.disabled = true; submit.textContent = 'Enviando…';
    status.textContent = 'Enviando o PDF para o armazenamento privado…';
    try {
      if (isReplace) {
        await api.replaceAsset(chapter.id, chosen);
      } else {
        const target = (visibility.querySelector('input[name="publishVis"]:checked') || {}).value || 'private';
        await api.publishPdf(chapter.id, { source_job_id: chosen, target_status: target });
      }
      await pollPublish(chapter.id, status);
      m.destroy(); toast(isReplace ? 'PDF substituído.' : 'Publicado com sucesso.', 'ok'); render();
    } catch (err) {
      submit.disabled = false; delete submit.dataset.busy; submit.textContent = isReplace ? 'Substituir' : 'Publicar';
      status.textContent = ''; fail(err);
    }
  });
}

function radio(name, value, label, checked) {
  const input = el('input', { attrs: { type: 'radio', name, value } });
  if (checked) input.checked = true;
  return el('label', { class: 'sc-pub-opt' }, [input, el('span', { text: label })]);
}

async function pollPublish(chapterId, statusNode) {
  // Poll the backend until the async upload finishes; never a tight loop.
  for (let i = 0; i < 60; i++) {
    const s = await api.publishStatus(chapterId);
    if (s.status === 'published') return;
    if (s.status === 'failed') throw new api.SocialApiError(503, 'publish_failed');
    statusNode.textContent = 'Enviando… isso pode levar alguns instantes.';
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new api.SocialApiError(503, 'publish_timeout');
}

function readerModal(chapter) {
  const controller = new AbortController();
  let objectUrl = null;
  const frame = el('div', { class: 'sc-reader-frame', attrs: { 'aria-busy': 'true' } });
  frame.appendChild(el('div', { class: 'sc-reader-loading', text: 'Carregando…' }));
  const m = modal(`Leitura — capítulo #${chapter.chapter_number}`, [frame]);
  const origDestroy = m.destroy;
  m.destroy = () => {
    controller.abort();
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; } // free memory + token-less blob
    frame.replaceChildren();
    origDestroy();
  };
  // Rebind close/escape to the wrapped destroy.
  m.card.querySelector('.sc-modal-close')?.addEventListener('click', m.destroy, { once: true });
  api.fetchChapterPdfUrl(chapter.id, controller.signal).then((url) => {
    objectUrl = url;
    frame.replaceChildren(el('object', { class: 'sc-reader-object',
      attrs: { data: url, type: 'application/pdf', 'aria-label': 'PDF do capítulo' } },
      [el('p', { text: 'Não foi possível exibir o PDF neste navegador.' })]));
    frame.removeAttribute('aria-busy');
  }).catch((err) => {
    if (controller.signal.aborted) return;
    frame.replaceChildren(errorBox(() => { m.destroy(); readerModal(chapter); }));
    if (err instanceof api.SocialApiError && err.status === 401) handleExpired();
  });
}

// ---- provider selection: backend state decides, never a DOM element ----
// The legacy SQLite community panel markup (#communityFeed and siblings) is always
// present in ui_shell.html, whatever COMMUNITY_SOCIAL_PROVIDER is configured to — its
// presence cannot tell us which provider is active. The backend is the only source of
// truth: /api/ui/bootstrap carries a sanitized `community.social` record (provider,
// available, reason_code — never a URL, key, path or raw exception). Single-flight
// cached, same pattern as tradutor_ui.js's own bootstrap refresh, so a retry after
// failure re-fetches but normal re-entry never re-requests.
let communityProviderStatePromise = null;
async function fetchCommunityProviderState() {
  if (!communityProviderStatePromise) {
    communityProviderStatePromise = fetch('/api/ui/bootstrap', { credentials: 'same-origin', cache: 'no-store' })
      .then((response) => response.json())
      .then((data) => (data && data.community && data.community.social) || null)
      .catch(() => null);
  }
  return communityProviderStatePromise;
}

function communityUnavailablePanel(onRetry) {
  return el('div', { class: 'sc-gate' }, [
    el('div', { class: 'sc-gate-mark', text: '共' }),
    el('h2', { text: 'Comunidade' }),
    el('p', { class: 'sc-gate-sub', text: 'A Comunidade remota está indisponível no momento. Tente novamente em instantes.' }),
    el('div', { class: 'sc-gate-actions' }, [
      el('button', { class: 'btn-primary', text: 'Tentar de novo', on: { click: onRetry } }),
    ]),
  ]);
}

// Mounts at most one of: the Supabase social UI (this module), or nothing (leaving the
// legacy SQLite UI in full control of #view-community). Never both, and never silently:
// an unknown/unconfigured/unreachable Supabase provider replaces the panel with a
// sanitized error instead of letting the legacy markup show as if it were the real thing.
// ---- mount lifecycle ------------------------------------------------------
// Exactly one mount per browser context, holding exactly one auth subscription.
//
// The auth layer calls its handler several times for a single sign-in: the SDK emits
// INITIAL_SESSION on subscribe, onAuthChange replays an explicit INITIAL_SESSION once the
// bounded session restore settles, a slow restore replays INITIAL_SESSION_RESTORED in the
// background, and SIGNED_IN/TOKEN_REFRESHED follow. Every one of those used to re-enter
// render() (one feed request each) and re-request the profile (because a failed profile
// left state.profile null), which is what produced the burst of identical GETs.
//
// A first sign-in replays that burst through TWO independent onAuthChange subscriptions
// (this module's own, and auth_ui.js's), each running its own bounded session-restore race
// against the same shared SDK client. That is enough for a session object to reach this
// handler in more than one shape across the burst (e.g. a snapshot taken mid-restore before
// the SDK has finished attaching everything it eventually attaches) - so keying the reload
// on a string derived from the session's own fields (user id / token) does not fully
// collapse the burst: two shapes of the *same* sign-in can compute two different keys and
// each looks like a new identity. What is genuinely single-valued across the whole burst is
// simpler: whether the browser is authenticated at all. That flips exactly once per real
// sign-in and once per real sign-out, never more - so it is the load's single-flight key,
// with the identity string kept only to detect an actual account switch and to guard a
// late response against a newer session. No debounce, no timer, no counter.
let mount = null;
let mountRequest = null;

function sessionKey(session) {
  if (!session) return 'anonymous';
  return String(session.user?.id || session.access_token || session.provider || 'session');
}

// Single-flight per session: concurrent equivalent callers share one request. A failure
// stays cached (the rejected promise) so merely reopening the profile tab reuses the known
// error instead of refetching — only an explicit refresh (the retry button) clears it.
function loadMyProfile({ refresh = false } = {}) {
  const active = mount;
  if (!active) return api.getMyProfile();
  if (refresh || !active.profileRequest) {
    const key = active.sessionKey;
    active.profileRequest = api.getMyProfile().then((profile) => {
      // A late answer must never overwrite the state of a newer session.
      if (mount === active && active.sessionKey === key) state.profile = profile;
      return profile;
    });
  }
  return active.profileRequest;
}

// ---- per-section lifecycle (favorites/history/mine/notifications/feed) ----
// Each section is idle/loading/loaded/error with at most one in-flight promise, scoped to
// the current mount so logout/re-login (a brand new `active` object) starts every section
// clean. Opening a tab whose section already loaded (or errored) renders from that state
// with zero requests; only 'idle' fetches. See paginated() for the renderer that consumes
// this, and invalidateSection() for how a mutating action (favorite, publish, delete...)
// marks its own section dirty without ever touching an unrelated one.
function sectionState(key) {
  const active = mount;
  if (!active) return { status: 'idle', items: [], cursor: '', error: null, promise: null };
  if (!active.sections) active.sections = {};
  if (!active.sections[key]) active.sections[key] = { status: 'idle', items: [], cursor: '', error: null, promise: null };
  return active.sections[key];
}

function invalidateSection(key) {
  const active = mount;
  if (active && active.sections) delete active.sections[key];
}

// ---- canonical auth-ready gate ---------------------------------------------
// The Supabase SDK session (above) and the canonical, backend-confirmed auth
// state (window.__tradutorAuthState, driven by auth_ui.js's own independent
// onAuthStateChange subscription + its /api/community/auth/session round trip)
// are two separate listeners racing the same underlying client. auth_ui.js
// explicitly regresses to 'auth_loading' on every SDK event and only flips to
// 'authenticated' once the backend agrees — proof that the raw SDK session
// alone is not sufficient evidence of readiness. Firing a private request the
// instant the SDK hands back a session (previous behaviour) could beat that
// confirmation and go out while the canonical state was still
// anonymous/authenticating, which is what produced a 401 on an otherwise
// normal first login.
//
// No timers: this resolves the instant the condition is already true (covers
// bootstrap/canonical state arriving first, e.g. after F5), or the next time
// tradutor-auth-changed announces authenticated:true (covers the SDK session
// arriving first, e.g. right after a fresh sign-in).
let canonicalAuthReadyWaiters = [];

function getGlobal(name) {
  try { return window[name]; } catch (_) { return undefined; }
}

// Undefined means no canonical arbiter is running in this context at all (e.g. this
// module loaded standalone, without auth_ui.js) - nothing to wait for, so treat that as
// ready rather than deadlocking. In the real shell auth_ui.js sets this synchronously to
// 'auth_loading' before any async gap, so production always has a defined, meaningful
// value here; only the *absence* of a value is permissive.
function canonicalAuthReady() {
  const raw = getGlobal('__tradutorAuthState');
  return raw === undefined || String(raw) === 'authenticated';
}

function waitForCanonicalAuthReady() {
  if (canonicalAuthReady()) return Promise.resolve();
  return new Promise((resolve) => { canonicalAuthReadyWaiters.push(resolve); });
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('tradutor-auth-changed', (event) => {
    if (String(event?.detail?.state || '') !== 'authenticated' && event?.detail?.authenticated !== true) return;
    const waiters = canonicalAuthReadyWaiters;
    canonicalAuthReadyWaiters = [];
    waiters.forEach((resolve) => resolve());
  });
}

async function applySession(session, active, event) {
  if (mount !== active) return; // unmounted while the event was in flight
  const authenticated = Boolean(session);
  const key = sessionKey(session);
  // supabase_auth.js races two independent signals for the SAME restored session: its own
  // explicit getSession()/getUser() restore, and the SDK's native onAuthStateChange replay.
  // Either can resolve first. When the native replay loses that race and arrives afterwards
  // still carrying no session (its own internal recovery had not caught up yet), it looks
  // identical to a sign-out unless the event name says otherwise - and treating it as one
  // wipes the profile/feed cache this mount already populated, so the SIGNED_IN that follows
  // moments later re-fires both requests. A real sign-out is always reported as SIGNED_OUT
  // by the SDK; nothing else legitimately regresses an already-authenticated mount, so only
  // that explicit event (or the very first event this mount ever sees) may do so.
  if (!authenticated && event !== 'SIGNED_OUT' && active.authenticated === true) return;
  if (authenticated === active.authenticated) {
    // Same side of the sign-in/sign-out edge as the last processed event: an SDK replay,
    // a background restore finishing late, or a proactive token refresh. Keep the latest
    // session snapshot for callers that read it, but the load for this edge already ran
    // (or is running) - issuing it again is exactly the duplicate this guard exists for.
    active.sessionKey = key;
    if (canonicalAuthReady() || !authenticated) state.session = session;
    return;
  }
  active.authenticated = authenticated;
  active.sessionKey = key;
  active.profileRequest = null;
  active.sections = {};
  state.profile = null;
  state.openWorkId = null;
  if (state.abort) { state.abort.abort(); state.abort = null; }
  if (!authenticated) {
    state.session = null;
    state.pendingAuth = false;
    render();
    return;
  }
  // A session arrived but no private request may leave until the canonical,
  // backend-confirmed generation agrees. Show a neutral loading shell instead
  // of the login gate while this settles (mount was requested; the load is
  // merely pending).
  state.session = null;
  state.pendingAuth = true;
  render();
  await waitForCanonicalAuthReady();
  // Re-check after the await: a logout, a brand new sign-in, or an unmount
  // that happened while waiting must invalidate this generation instead of
  // resuming it. This is the only guard against an old generation acting on
  // a stale wakeup.
  if (mount !== active || active.sessionKey !== key || active.authenticated !== true) return;
  state.pendingAuth = false;
  state.session = session;
  try { await loadMyProfile(); } catch (_) { /* the profile tab surfaces it */ }
  if (mount !== active || active.sessionKey !== key) return;
  render();
}

// Releases the subscription and every cached fragment of the signed-in session, so a
// later login mounts a clean context instead of resuming a stale one.
export function unmountCommunity() {
  const active = mount;
  if (!active) return;
  mount = null;
  mountRequest = null;
  state.session = null;
  state.profile = null;
  state.openWorkId = null;
  state.pendingAuth = false;
  if (state.abort) { state.abort.abort(); state.abort = null; }
  window.__socialCommunityMounted = false;
  try { active.unsubscribe?.(); } catch (_) { /* already released */ }
}

async function mountCommunityProvider(host, social) {
  if (social && social.provider === 'local') {
    // Explicit local/SQLite mode: the legacy CommunityApi UI already owns this panel.
    window.__socialCommunitySkipped = 'local_provider_active';
    return;
  }
  if (social && social.provider === 'supabase' && social.available) {
    // Idempotent: a repeated bootstrap, a re-entered boot() or a second module consumer
    // all await the same mount instead of building a second one.
    if (!mountRequest) mountRequest = mountSupabaseCommunity();
    return mountRequest;
  }
  // Fail closed: missing bootstrap state, an unrecognized provider, or Supabase selected
  // but not currently available. Neither UI mounts silently as the other.
  window.__socialCommunitySkipped = (social && social.reason_code) || 'social_provider_unknown';
  host.replaceChildren(communityUnavailablePanel(async () => {
    communityProviderStatePromise = null;
    await mountCommunityProvider(host, await fetchCommunityProviderState());
  }));
}

// Declared after the provider gate on purpose: no Supabase client is ever built before
// the backend has said "supabase". Function declarations hoist, so the gate can call it.
async function mountSupabaseCommunity() {
  // In local-session auth mode there is no Supabase client; leave the legacy UI be.
  const client = await getSupabaseClient();
  if (!client) { mountRequest = null; return; }
  const active = { unsubscribe: null, sessionKey: null, profileRequest: null, authenticated: undefined, sections: {} };
  mount = active;
  window.__socialCommunityMounted = true;
  const unsubscribe = await onAuthChange((session, event) => { void applySession(session, active, event); });
  if (mount === active) active.unsubscribe = unsubscribe;
  else { try { unsubscribe?.(); } catch (_) { /* already released */ } }
}

async function boot() {
  const host = root();
  if (!host) return;
  await mountCommunityProvider(host, await fetchCommunityProviderState());
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
