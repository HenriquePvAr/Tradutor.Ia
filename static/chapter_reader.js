// In-app chapter reader.
//
// The backend hands us one JPEG per page (the image already stored inside the run's
// PDF), so "rendering" here is an <img> plus CSS width: no PDF engine, no canvas
// pool, no worker to leak. Only the current page is in the DOM; thumbnails load when
// they scroll into view.
//
// `createReaderState` below is pure and owns every decision (bounds, zoom steps, fit
// scale, staleness). It is exported so the contract can be tested in node without a
// DOM. Everything under "view" is dumb wiring around it.

export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 4;
// Explicit steps, so repeated zooming lands on the same values every time instead of
// drifting through floating point (1.1^n never returns to exactly 1).
export const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.25, 1.5, 2, 2.5, 3, 4];
const CLOSED = 'closed';
const LOADING = 'loading';
const READY = 'ready';
const ERROR = 'error';

export {CLOSED, LOADING, READY, ERROR};

function clampZoom(value) {
  if (!Number.isFinite(value)) return 1;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

export function createReaderState() {
  const state = {
    status: CLOSED,
    token: 0,
    jobId: '',
    title: '',
    mode: 'images',
    pageCount: 0,
    pages: [],
    page: 1,
    zoom: 1,
    fitMode: 'width',
    fullscreen: false,
    thumbnails: true,
    reviewRequired: false,
    error: '',
    viewport: {width: 0, height: 0},
  };

  function currentPage() {
    return state.pages[state.page - 1] || null;
  }

  // The effective scale: fit modes derive it from the viewport, manual zoom does not.
  function scale() {
    const page = currentPage();
    if (!page || !page.width || !page.height) return clampZoom(state.zoom);
    const {width, height} = state.viewport;
    if (state.fitMode === 'width' && width > 0) return clampZoom(width / page.width);
    if (state.fitMode === 'page' && width > 0 && height > 0) {
      return clampZoom(Math.min(width / page.width, height / page.height));
    }
    return clampZoom(state.zoom);
  }

  // Identity of what should be on screen right now. A render that finishes carrying
  // an older token is a result for a page or a scale the user already left behind.
  function renderToken() {
    return `${state.token}:${state.page}:${scale().toFixed(4)}`;
  }

  function open(jobId) {
    state.token += 1;
    state.status = LOADING;
    state.jobId = String(jobId || '');
    state.error = '';
    state.pages = [];
    state.pageCount = 0;
    state.page = 1;
    state.title = '';
    state.reviewRequired = false;
    return state.token;
  }

  function accept(token, document_) {
    if (token !== state.token) return false;
    const pages = Array.isArray(document_?.pages) ? document_.pages : [];
    state.status = READY;
    state.title = String(document_?.title || '');
    state.mode = document_?.mode === 'embed' ? 'embed' : 'images';
    state.pages = pages;
    state.pageCount = Number(document_?.page_count || pages.length) || 0;
    state.page = state.pageCount > 0 ? 1 : 0;
    state.reviewRequired = Boolean(document_?.review_required);
    state.fitMode = 'width';
    state.zoom = 1;
    return true;
  }

  function fail(token, code) {
    if (token !== state.token) return false;
    state.status = ERROR;
    state.error = String(code || 'reader_failed');
    return true;
  }

  function close() {
    state.token += 1;
    state.status = CLOSED;
    state.jobId = '';
    state.pages = [];
    state.pageCount = 0;
    state.page = 1;
    state.error = '';
    state.fullscreen = false;
  }

  function goto(value) {
    const page = Number.parseInt(value, 10);
    if (!Number.isFinite(page) || state.pageCount < 1) return state.page;
    state.page = Math.min(state.pageCount, Math.max(1, page));
    return state.page;
  }

  const next = () => goto(state.page + 1);
  const previous = () => goto(state.page - 1);
  const first = () => goto(1);
  const last = () => goto(state.pageCount);
  const canPrevious = () => state.status === READY && state.page > 1;
  const canNext = () => state.status === READY && state.page < state.pageCount;

  function setZoom(value) {
    state.fitMode = 'none';
    state.zoom = clampZoom(Number(value));
    return state.zoom;
  }

  function zoomIn() {
    const current = scale();
    return setZoom(ZOOM_STEPS.find(step => step > current + 1e-6) ?? MAX_ZOOM);
  }

  function zoomOut() {
    const current = scale();
    const lower = ZOOM_STEPS.filter(step => step < current - 1e-6);
    return setZoom(lower.length ? lower[lower.length - 1] : MIN_ZOOM);
  }

  const resetZoom = () => setZoom(1);
  const fitWidth = () => { state.fitMode = 'width'; return scale(); };
  const fitPage = () => { state.fitMode = 'page'; return scale(); };

  // A resize must move a fit mode and must not silently undo a manual zoom.
  function setViewport(width, height) {
    state.viewport = {width: Math.max(0, Number(width) || 0),
                      height: Math.max(0, Number(height) || 0)};
    return scale();
  }

  return {
    state, scale, renderToken, currentPage,
    open, accept, fail, close,
    goto, next, previous, first, last, canPrevious, canNext,
    setZoom, zoomIn, zoomOut, resetZoom, fitWidth, fitPage, setViewport,
  };
}

/* ------------------------------------------------------------------ view ---- */

const ERROR_MESSAGES = {
  artifact_unavailable: 'O PDF desta execução não está disponível.',
  artifact_missing: 'O arquivo deste capítulo não está mais disponível.',
  not_found: 'O PDF desta execução não está disponível.',
  page_not_available: 'Não foi possível abrir este PDF.',
  reader_failed: 'Não foi possível abrir este PDF.',
  // A 401/403 is a session problem, not a broken artifact. Reporting it as "this PDF
  // cannot be opened" is what hid the real defect: every reader request was anonymous.
  authentication_required: 'Sua sessão expirou. Entre novamente para ler este capítulo.',
  csrf_rejected: 'Sua sessão expirou. Entre novamente para ler este capítulo.',
};

export const readerErrorMessage = code =>
  ERROR_MESSAGES[code] || ERROR_MESSAGES.reader_failed;

// The app authenticates with a Bearer token in a header (Supabase), or with a
// same-origin session cookie (local/Better Auth). Sending both covers either
// provider, and the token never appears in a URL.
export function readerRequestInit(token, extra = {}) {
  const headers = {...(extra.headers || {})};
  if (token) headers.Authorization = `Bearer ${token}`;
  return {...extra, headers, credentials: 'same-origin', cache: 'no-store'};
}

function boot() {
  const root = document.getElementById('view-leitor');
  if (!root) return;
  const reader = createReaderState();
  const $ = id => document.getElementById(id);
  const stage = $('readerStage');
  const image = $('readerPage');
  const embed = $('readerEmbed');
  const thumbs = $('readerThumbs');
  const pageInput = $('readerPageInput');
  const pageTotal = $('readerPageTotal');
  const zoomLabel = $('readerZoomLabel');
  const statusBox = $('readerStatus');
  const titleBox = $('readerTitle');
  const badge = $('readerBadge');
  const shell = $('readerShell');
  let observer = null;

  const toast = (message, kind) => {
    if (typeof window.__tradutorToast === 'function') window.__tradutorToast(message, kind);
  };

  // ---- authenticated transport -------------------------------------------
  // `<img src>` and `<iframe src>` cannot carry an Authorization header, so every
  // byte the reader shows is fetched here and handed to the element as an object
  // URL. Same canonical token the rest of the app uses; no reader-only credential,
  // no token in a query string, ownership still proven server-side per request.
  async function sessionToken() {
    const cached = window.__tradutorAccessToken || '';
    if (cached) return cached;
    const resolve = window.__tradutorGetCanonicalAccessToken;
    if (typeof resolve !== 'function') return '';
    try { return (await resolve()) || ''; } catch (_) { return ''; }
  }

  const authFetch = async (url, extra) =>
    fetch(url, readerRequestInit(await sessionToken(), extra));

  let pageBlobUrl = '';
  let embedBlobUrl = '';
  const thumbBlobUrls = [];
  const releaseThumbnails = () => {
    while (thumbBlobUrls.length) URL.revokeObjectURL(thumbBlobUrls.pop());
  };

  // Loads `url` into `element.src`, keeping only the newest blob alive. `guard()`
  // must still return `stamp` when the bytes arrive, or the result is stale.
  async function loadInto(element, url, guard, stamp) {
    let blob;
    try {
      const response = await authFetch(url);
      if (!response.ok) return String(response.status);
      blob = await response.blob();
    } catch (_) {
      return 'reader_failed';
    }
    if (guard() !== stamp) return '';
    const object = URL.createObjectURL(blob);
    element.src = object;
    return object;
  }

  function measure() {
    if (!stage) return;
    const style = window.getComputedStyle(stage);
    const padding = parseFloat(style.paddingLeft || 0) + parseFloat(style.paddingRight || 0);
    // clientWidth already excludes the scrollbar, so fit-width cannot overshoot it.
    reader.setViewport(Math.max(0, stage.clientWidth - padding),
                       Math.max(0, stage.clientHeight - 8));
  }

  function setStatus(text, kind) {
    if (!statusBox) return;
    statusBox.hidden = !text;
    statusBox.className = `reader-status${kind ? ` ${kind}` : ''}`;
    statusBox.innerHTML = '';
    if (!text) return;
    const paragraph = document.createElement('p');
    paragraph.textContent = text;
    statusBox.appendChild(paragraph);
    if (kind !== 'error') return;
    const actions = document.createElement('div');
    actions.className = 'reader-status-actions';
    [['Tentar novamente', 'retry'], ['Abrir externamente', 'external'],
     ['Voltar ao Histórico', 'back']].forEach(([label, action]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn-ghost';
      button.dataset.readerAction = action;
      button.textContent = label;
      actions.appendChild(button);
    });
    statusBox.appendChild(actions);
  }

  // A thumbnail is fetched once, when it first scrolls into view, and never re-fetched.
  function loadThumbnail(img) {
    if (!img || img.dataset.loading || img.getAttribute('src')) return;
    img.dataset.loading = '1';
    const token = reader.state.token;
    void loadInto(img, img.dataset.src, () => reader.state.token, token)
      .then(object => {
        if (object && object.startsWith('blob:')) thumbBlobUrls.push(object);
        else delete img.dataset.loading;
      });
  }

  function renderThumbnails() {
    if (!thumbs) return;
    releaseThumbnails();
    thumbs.innerHTML = '';
    if (observer) observer.disconnect();
    if (reader.state.mode !== 'images') return;
    observer = 'IntersectionObserver' in window
      ? new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (!entry.isIntersecting) return;
          const button = entry.target;
          loadThumbnail(button.querySelector('img'));
          observer.unobserve(button);
        });
      }, {root: thumbs, rootMargin: '400px 0px'})
      : null;
    const fragment = document.createDocumentFragment();
    for (let page = 1; page <= reader.state.pageCount; page += 1) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'reader-thumb';
      button.dataset.page = String(page);
      button.setAttribute('aria-label', `Página ${page}`);
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      img.dataset.src = `/api/ui/reader/${encodeURIComponent(reader.state.jobId)}/thumb/${page}`;
      const number = document.createElement('span');
      number.textContent = String(page);
      button.append(img, number);
      fragment.appendChild(button);
      if (observer) observer.observe(button);
      else loadThumbnail(img);
    }
    thumbs.appendChild(fragment);
  }

  function syncThumbSelection() {
    if (!thumbs) return;
    thumbs.querySelectorAll('.reader-thumb.active').forEach(node => node.classList.remove('active'));
    const active = thumbs.querySelector(`.reader-thumb[data-page="${reader.state.page}"]`);
    if (!active) return;
    active.classList.add('active');
    loadThumbnail(active.querySelector('img'));
    if (typeof active.scrollIntoView === 'function') {
      active.scrollIntoView({block: 'nearest'});
    }
  }

  function renderPage(keepScroll) {
    const {state} = reader;
    if (!image) return;
    const page = reader.currentPage();
    if (state.status !== READY || state.mode !== 'images' || !page) return;
    const token = reader.renderToken();
    const width = Math.round(page.width * reader.scale());
    image.dataset.token = token;
    image.style.width = `${width}px`;
    const source = `/api/ui/reader/${encodeURIComponent(state.jobId)}/page/${state.page}`;
    if (!keepScroll && stage) stage.scrollTop = 0;
    // Zooming and fitting reuse the page already in memory; only a page change fetches.
    if (image.dataset.source === source) return;
    image.dataset.source = source;
    void loadInto(image, source, () => image.dataset.source, source).then(object => {
      if (!object) return;
      if (!object.startsWith('blob:')) {
        image.dataset.source = '';
        reader.fail(state.token, object === '401' || object === '403'
          ? 'authentication_required' : 'page_not_available');
        render(true);
        return;
      }
      if (pageBlobUrl) URL.revokeObjectURL(pageBlobUrl);
      pageBlobUrl = object;
    });
  }

  function syncToolbar() {
    const {state} = reader;
    const ready = state.status === READY;
    if (pageInput) {
      pageInput.value = ready ? String(state.page) : '';
      pageInput.disabled = !ready || state.mode !== 'images';
    }
    if (pageTotal) pageTotal.textContent = ready ? `/ ${state.pageCount}` : '/ —';
    if (zoomLabel) zoomLabel.textContent = `${Math.round(reader.scale() * 100)}%`;
    const prev = root.querySelector('[data-reader-action="prev"]');
    const next = root.querySelector('[data-reader-action="next"]');
    if (prev) prev.disabled = !reader.canPrevious();
    if (next) next.disabled = !reader.canNext();
    root.querySelectorAll('[data-reader-action="fit-width"],[data-reader-action="fit-page"]')
      .forEach(button => button.setAttribute('aria-pressed',
        String(button.dataset.readerAction === `fit-${state.fitMode}`)));
    if (titleBox) titleBox.textContent = state.title || 'Leitor';
    if (badge) {
      badge.hidden = !ready || !state.reviewRequired;
      badge.textContent = 'Revisão necessária';
    }
  }

  function render(keepScroll) {
    const {state} = reader;
    if (image) image.hidden = !(state.status === READY && state.mode === 'images');
    if (embed) {
      const useEmbed = state.status === READY && state.mode === 'embed';
      embed.hidden = !useEmbed;
      const source = useEmbed
        ? `/api/ui/reader/${encodeURIComponent(state.jobId)}/pdf` : '';
      if (embed.dataset.source !== source) {
        embed.dataset.source = source;
        if (embedBlobUrl) { URL.revokeObjectURL(embedBlobUrl); embedBlobUrl = ''; }
        if (!source) embed.removeAttribute('src');
        else void loadInto(embed, source, () => embed.dataset.source, source)
          .then(object => { if (object?.startsWith('blob:')) embedBlobUrl = object; });
      }
    }
    if (shell) shell.classList.toggle('no-thumbs', !state.thumbnails || state.mode !== 'images');
    if (state.status === LOADING) setStatus('Carregando capítulo…', 'loading');
    else if (state.status === ERROR) setStatus(readerErrorMessage(state.error), 'error');
    else if (state.status === CLOSED) setStatus('Escolha um capítulo em "Capítulos traduzidos" para ler aqui.', '');
    else setStatus('', '');
    syncToolbar();
    renderPage(keepScroll);
    syncThumbSelection();
  }

  function releaseAll() {
    releaseThumbnails();
    if (pageBlobUrl) { URL.revokeObjectURL(pageBlobUrl); pageBlobUrl = ''; }
    if (embedBlobUrl) { URL.revokeObjectURL(embedBlobUrl); embedBlobUrl = ''; }
    if (image) { image.removeAttribute('src'); image.dataset.source = ''; }
    if (embed) { embed.removeAttribute('src'); embed.dataset.source = ''; }
  }

  async function load(jobId) {
    const token = reader.open(jobId);
    releaseAll();
    if (thumbs) thumbs.innerHTML = '';
    render();
    let payload = null;
    try {
      const response = await authFetch(`/api/ui/reader/${encodeURIComponent(jobId)}`,
        {headers: {'Accept': 'application/json'}});
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        const code = typeof detail?.detail === 'object'
          ? detail.detail.code
          : String(detail?.detail
            || (response.status === 401 ? 'authentication_required' : 'reader_failed'));
        // A result for a chapter the user already left must never paint over the new one.
        reader.fail(token, code);
        render();
        return;
      }
      payload = await response.json();
    } catch (_) {
      reader.fail(token, 'reader_failed');
      render();
      return;
    }
    if (!reader.accept(token, payload)) return;
    measure();
    render();
    renderThumbnails();
    syncThumbSelection();
  }

  function toggleFullscreen() {
    const target = shell || root;
    try {
      if (document.fullscreenElement) {
        document.exitFullscreen?.();
        return;
      }
      const request = target.requestFullscreen?.bind(target);
      // Unsupported or denied: the reader keeps working exactly as it is.
      if (!request) { toast('Tela cheia não está disponível neste navegador.', 'warn'); return; }
      Promise.resolve(request()).catch(() =>
        toast('Tela cheia não está disponível neste navegador.', 'warn'));
    } catch (_) {
      toast('Tela cheia não está disponível neste navegador.', 'warn');
    }
  }

  function openExternally() {
    const jobId = reader.state.jobId;
    if (!jobId) return;
    window.dispatchEvent(new CustomEvent('tradutor-open-artifact',
      {detail: {jobId, artifact: 'pdf'}}));
  }

  const backToHistory = () => window.dispatchEvent(new CustomEvent('tradutor-goto-tab', {detail: {tab: 'hist'}}));

  const ACTIONS = {
    back: backToHistory,
    prev: () => { reader.previous(); render(); },
    next: () => { reader.next(); render(); },
    'zoom-in': () => { reader.zoomIn(); render(true); },
    'zoom-out': () => { reader.zoomOut(); render(true); },
    'zoom-reset': () => { reader.resetZoom(); render(true); },
    'fit-width': () => { measure(); reader.fitWidth(); render(true); },
    'fit-page': () => { measure(); reader.fitPage(); render(true); },
    fullscreen: toggleFullscreen,
    external: openExternally,
    thumbs: () => { reader.state.thumbnails = !reader.state.thumbnails; render(true); },
    retry: () => { if (reader.state.jobId) void load(reader.state.jobId); },
  };

  root.addEventListener('click', event => {
    const thumb = event.target.closest('.reader-thumb');
    if (thumb && thumbs?.contains(thumb)) {
      reader.goto(thumb.dataset.page);
      render();
      return;
    }
    const button = event.target.closest('[data-reader-action]');
    if (!button || button.disabled) return;
    ACTIONS[button.dataset.readerAction]?.();
  });

  pageInput?.addEventListener('change', () => {
    reader.goto(pageInput.value);
    render();
  });
  pageInput?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { reader.goto(pageInput.value); render(); }
  });

  // Global shortcuts belong to the reader only while it is the visible tab, and never
  // while the caret is in a field -- typing "12" in the page box must not zoom.
  const isTyping = target => {
    const tag = String(target?.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select'
      || Boolean(target?.isContentEditable);
  };
  document.addEventListener('keydown', event => {
    if (!root.classList.contains('active') || reader.state.status !== READY) return;
    if (isTyping(event.target) || event.ctrlKey || event.metaKey || event.altKey) return;
    const action = {
      ArrowRight: 'next', PageDown: 'next', ArrowLeft: 'prev', PageUp: 'prev',
      Home: 'first', End: 'last', '+': 'zoom-in', '=': 'zoom-in', '-': 'zoom-out',
      w: 'fit-width', W: 'fit-width', p: 'fit-page', P: 'fit-page',
    }[event.key];
    if (!action) return;
    event.preventDefault();
    if (action === 'first') { reader.first(); render(); return; }
    if (action === 'last') { reader.last(); render(); return; }
    ACTIONS[action]?.();
  });

  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      if (reader.state.status !== READY) return;
      measure();
      // Manual zoom is a user decision; only fit modes follow the container.
      if (reader.state.fitMode !== 'none') render(true);
      else syncToolbar();
    }, 120);
  });
  document.addEventListener('fullscreenchange', () => {
    reader.state.fullscreen = Boolean(document.fullscreenElement);
    measure();
    if (reader.state.status === READY) render(true);
  });

  window.addEventListener('tradutor-open-reader', event => {
    const jobId = String(event?.detail?.jobId || '');
    if (!jobId) return;
    void load(jobId);
  });
  window.addEventListener('tradutor-close-reader', () => { reader.close(); releaseAll(); render(); });

  window.__tradutorReader = reader;
  render();
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
}
