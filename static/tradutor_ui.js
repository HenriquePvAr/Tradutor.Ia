(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const escapeHtml = value => {
    const node = document.createElement('div');
    node.textContent = String(value ?? '');
    return node.innerHTML;
  };
  const escapeAttr = value => escapeHtml(value).replace(/"/g, '&quot;');
  const appState = {
    bootstrap: null,
    history: [],
    profile: {},
    settings: {},
    queue: [],
    status: 'ready',
    cursor: 0,
    historyRevision: 0,
    selectedScope: 'full',
    selectedMode: 'fast',
    selectedSourceType: 'url',
    nameDirty: false,
    outputDirty: false,
    programmingFields: false,
    activeStage: 'prepare',
    polling: false,
    logs: [],
    visualLogClearedAt: 0,
    lastFinishedId: '',
    sourceReview: null,
    qualityReview: null,
    qualityReviewFilter: 'pending',
    cancelBusy: false,
    expandedFolders: new Set(),
    seriesQuery: '',
    seriesSort: 'recent',
    publicationRecord: null,
    publicationBusy: false,
    publicationDrafts: Object.create(null),
  };
  const runStatusLabels = {ready: 'pronto', staging: 'analisando fonte', queued: 'na fila', running: 'rodando', awaiting_source_review: 'revisão das páginas', finished: 'finalizado', review_required: 'revisão necessária', failed: 'erro', legacy_unverified: 'legado não verificado', error: 'erro', cancelled: 'cancelado'};
  const terminalRunStatuses = new Set(['finished', 'review_required']);
  const inFlightStatuses = new Set(['staging', 'queued', 'claiming', 'starting', 'running', 'cancelling', 'awaiting_source_review']);
  const boolish = value => {
    if (value === true || value === false) return value;
    if (value === 1 || value === '1') return true;
    if (value === 0 || value === '0') return false;
    if (typeof value === 'string') {
      const normalized = value.trim().toLowerCase();
      if (['true', 'yes'].includes(normalized)) return true;
      if (['false', 'no'].includes(normalized)) return false;
    }
    return null;
  };

  function cookieValue(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    const part = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith(prefix));
    return part ? decodeURIComponent(part.slice(prefix.length)) : '';
  }
  async function api(path, options = {}) {
    const method = String(options.method || 'GET').toUpperCase();
    const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      const csrf = cookieValue('tradutor_community_csrf');
      if (csrf) headers['X-Tradutor-CSRF'] = csrf;
    }
    // Supabase mode: attach the current access token (kept fresh by the SDK). The token
    // lives only in the auth module's cache; this never persists or logs it.
    const bearer = window.__tradutorAccessToken || '';
    if (bearer) headers['Authorization'] = `Bearer ${bearer}`;
    const init = {...options, method, headers, credentials: 'same-origin'};
    const response = await fetch(path, init);
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    if (!response.ok) {
      const detail = payload.detail;
      const error = new Error(
        (detail && typeof detail === 'object' ? detail.message : detail)
        || `Falha local (${response.status})`);
      error.status = response.status;
      if (detail && typeof detail === 'object') {
        error.code = detail.code || '';
        error.stage = detail.stage || '';
        error.action = detail.action || '';
        error.hosts = detail.hosts || null;
      }
      throw error;
    }
    return payload;
  }

  /* ---------- boot ---------- */
  const bootEl = $('#boot');
  function closeBoot() { if (bootEl) bootEl.classList.add('hide'); }
  const bootTimer = window.setTimeout(closeBoot, 2050);
  bootEl?.addEventListener('click', () => { window.clearTimeout(bootTimer); closeBoot(); });

  /* ---------- ambient canvas ---------- */
  const ambientGlow = $('#ambientGlow');
  const tabTheme = {
    inicio: {hex: '#c5372c', rgb: '197,55,44'},
    nova: {hex: '#c5372c', rgb: '197,55,44'},
    queue: {hex: '#4a7fb5', rgb: '74,127,181'},
    hist: {hex: '#c9a227', rgb: '201,162,39'},
    community: {hex: '#b8557a', rgb: '184,85,122'},
    cfg: {hex: '#2f7a6b', rgb: '47,122,107'},
    logs: {hex: '#8a8377', rgb: '138,131,119'},
    profile: {hex: '#8a5fa3', rgb: '138,95,163'},
  };
  function ambientSweep() {
    const sweep = document.createElement('div');
    sweep.className = 'sweep';
    document.body.appendChild(sweep);
    window.setTimeout(() => sweep.remove(), 950);
  }

  const canvas = $('#bgCanvas');
  const ctx = canvas?.getContext('2d');
  let width = 0;
  let height = 0;
  let animationFrame = 0;
  let canvasPaused = document.hidden;
  const particles = Array.from({length: 70}, () => ({
    x: Math.random(), y: Math.random(), r: .3 + Math.random() * 1.3,
    vx: (Math.random() - .5) * .00018, vy: (Math.random() - .5) * .00016,
    alpha: .08 + Math.random() * .18,
  }));
  const blots = Array.from({length: 2}, () => ({
    x: Math.random(), y: Math.random(), r: 90 + Math.random() * 170,
    drift: Math.random() * Math.PI * 2,
  }));
  let burstLines = [];
  function resizeCanvas() {
    if (!canvas) return;
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
  }
  function drawFrame() {
    if (!ctx || canvasPaused) return;
    ctx.clearRect(0, 0, width, height);
    const time = performance.now() * .00012;
    blots.forEach((blot, index) => {
      const x = blot.x * width + Math.sin(time + blot.drift) * 20;
      const y = blot.y * height + Math.cos(time * .7 + blot.drift) * 16;
      const gradient = ctx.createRadialGradient(x, y, 0, x, y, blot.r);
      gradient.addColorStop(0, index % 2 ? 'rgba(239,231,216,.014)' : 'rgba(138,131,119,.012)');
      gradient.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = gradient;
      ctx.fillRect(x - blot.r, y - blot.r, blot.r * 2, blot.r * 2);
    });
    particles.forEach(particle => {
      particle.x += particle.vx;
      particle.y += particle.vy;
      if (particle.x < 0) particle.x = 1;
      if (particle.x > 1) particle.x = 0;
      if (particle.y < 0) particle.y = 1;
      if (particle.y > 1) particle.y = 0;
      ctx.beginPath();
      ctx.arc(particle.x * width, particle.y * height, particle.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(239,231,216,${particle.alpha})`;
      ctx.fill();
    });
    burstLines = burstLines.filter(line => line.life > 0);
    burstLines.forEach(line => {
      line.life -= .035;
      ctx.beginPath();
      ctx.moveTo(line.x, line.y);
      ctx.lineTo(line.x + Math.cos(line.angle) * line.length, line.y + Math.sin(line.angle) * line.length);
      ctx.strokeStyle = `rgba(239,231,216,${Math.max(0, line.life) * .22})`;
      ctx.lineWidth = .7;
      ctx.stroke();
    });
    animationFrame = requestAnimationFrame(drawFrame);
  }
  function burstAt(x, y, count = 14) {
    for (let index = 0; index < count; index += 1) {
      burstLines.push({x, y, angle: Math.random() * Math.PI * 2, length: 18 + Math.random() * 58, life: 1});
    }
  }
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas, {passive: true});
  document.addEventListener('visibilitychange', () => {
    canvasPaused = document.hidden;
    if (canvasPaused) cancelAnimationFrame(animationFrame);
    else { drawFrame(); pollState(); }
  });
  drawFrame();

  /* ---------- tabs and motion ---------- */
  const views = {
    inicio: 'view-inicio', nova: 'view-nova', queue: 'view-queue', hist: 'view-hist',
    community: 'view-community', cfg: 'view-cfg', logs: 'view-logs', profile: 'view-profile',
  };
  const railIndicator = $('#railIndicator');
  function moveIndicator(tab) {
    if (!railIndicator || window.innerWidth <= 880) return;
    railIndicator.style.transform = `translateY(${tab.offsetTop - 18}px)`;
    railIndicator.style.height = `${tab.offsetHeight}px`;
  }
  function staggerReveal(view) {
    $$('.panel, .hist-item, .dash-stat-card', view).forEach((item, index) => {
      item.style.animation = 'none';
      void item.offsetHeight;
      item.style.animation = `riseIn .42s cubic-bezier(.2,.8,.25,1) ${Math.min(index, 8) * 70}ms both`;
    });
  }
  function activateTab(name) {
    const tab = $(`.rail-tab[data-tab="${name}"]`);
    const target = $(`#${views[name] || ''}`);
    if (!tab || !target) return;
    const previous = $('.panel-view.active');
    if (previous && previous !== target) {
      previous.classList.remove('active');
      previous.classList.add('leaving');
      window.setTimeout(() => previous.classList.remove('leaving'), 210);
    }
    $$('.rail-tab').forEach(item => item.classList.remove('active'));
    tab.classList.add('active');
    target.classList.add('active');
    moveIndicator(tab);
    staggerReveal(target);
    const theme = tabTheme[name] || tabTheme.inicio;
    document.documentElement.style.setProperty('--tab-accent', theme.hex);
    document.documentElement.style.setProperty('--tab-rgb', theme.rgb);
    railIndicator.style.borderColor = theme.hex;
    railIndicator.style.background = `rgba(${theme.rgb},.095)`;
    if (ambientGlow) ambientGlow.style.background = `radial-gradient(circle at 80% 8%, rgba(${theme.rgb},.105), transparent 38%)`;
    ambientSweep();
    const rect = tab.getBoundingClientRect();
    burstAt(rect.right, rect.top + rect.height / 2, 10);
    if (name === 'hist') renderHistory();
    if (name === 'inicio') renderDashboard();
    if (name === 'community') loadCommunityFeed();
  }
  $$('.rail-tab').forEach(tab => tab.addEventListener('click', () => activateTab(tab.dataset.tab)));
  $$('[data-goto]').forEach(button => button.addEventListener('click', () => activateTab(button.dataset.goto)));
  $('#railProfile')?.addEventListener('click', () => activateTab('profile'));
  window.setTimeout(() => moveIndicator($('.rail-tab.active')), 60);

  /* ---------- visual feedback ---------- */
  const reasonMessages = {
    source_not_ready: 'Não foi possível abrir o navegador necessário para analisar a fonte.',
    authentication_required: 'Essa fonte exige autenticação.',
    challenge_required: 'A fonte exige uma verificação interativa.',
    source_access_denied: 'A fonte recusou o acesso público.',
    source_rate_limited: 'A fonte limitou temporariamente o acesso.',
    incomplete_source_coverage: 'Não foi possível carregar todas as páginas do leitor.',
    no_chapter_images: 'Nenhuma página do capítulo foi encontrada.',
    // Kept distinct on purpose: a download error must never be shown for an analysis that
    // never reached the download stage.
    worker_unavailable: 'Servico de processamento indisponivel.',
    user_cancelled: 'O processamento foi cancelado.',
    review_required: 'O PDF foi criado, mas alguns itens precisam de revisao.',
    timeout: 'O site demorou demais para responder.',
    connection_error: 'Nao foi possivel conectar ao site.',
    transport_error: 'Ocorreu um problema ao baixar as imagens.',
    incomplete_download: 'Algumas páginas não puderam ser baixadas.',
    unsupported_source: 'Esta fonte ainda não é suportada.',
    environment_not_configured: 'Configure o arquivo .env e a NVIDIA_API_KEY antes de processar.',
  };
  function reasonText(code) {
    return reasonMessages[code] || '';
  }

  const stageMessages = {
    prepare: 'aguardando início',
    queued: 'na fila', worker_starting: 'iniciando processamento',
    source_lazy_resolution: 'carregando páginas do leitor',
    source_selection: 'preparando a ordem das páginas',
    preparing: 'preparando processamento', quality_gate: 'validando resultado',
    source_validation: 'validando a fonte', source_analysis: 'localizando páginas', browser_loading: 'abrindo o leitor', collecting_candidates: 'coletando páginas', clustering_candidates: 'validando páginas', awaiting_source_review: 'aguardando revisão das páginas', downloading_pages: 'baixando páginas', validating_pages: 'validando páginas', download: 'baixando páginas', validation: 'detectando balões',
    ocr: 'lendo texto', classification: 'organizando regiões', translate: 'traduzindo',
    render: 'redesenhando balões', pdf: 'gerando PDF', reports: 'finalizando', final: 'concluído',
  };
  const sfxWords = {download: '…', validation: '!!', ocr: 'スキャン', classification: 'CHK', translate: 'ZAP', render: 'SHK', pdf: 'PDF', reports: 'OK'};
  function sfxPop(key) {
    const stage = $('#balloonStage');
    if (!stage || !sfxWords[key]) return;
    const element = document.createElement('div');
    element.className = 'sfx-pop';
    element.innerHTML = `<span>${escapeHtml(sfxWords[key])}</span>`;
    stage.appendChild(element);
    window.setTimeout(() => element.remove(), 1100);
  }
  function flashFrame() {
    const flash = document.createElement('div');
    flash.className = 'flash-frame';
    document.body.appendChild(flash);
    window.setTimeout(() => flash.remove(), 600);
  }
  function showToast(message, type = 'ok') {
    const stack = $('#toastStack');
    if (!stack) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icon = type === 'error' ? '!' : type === 'warn' ? '△' : '✓';
    toast.innerHTML = `<span class="t-icon">${icon}</span><span>${escapeHtml(message)}</span>`;
    stack.appendChild(toast);
    window.setTimeout(() => { toast.classList.add('leaving'); window.setTimeout(() => toast.remove(), 280); }, 3200);
  }
  // Bridge so ES modules (e.g. the social community UI) reuse the same toast component.
  window.__tradutorToast = (message, kind) => showToast(message, kind === 'err' ? 'error' : kind || 'ok');
  function shake(element) {
    if (!element) return;
    element.style.animation = 'none';
    void element.offsetHeight;
    element.style.animation = 'shake .35s ease';
  }

  /* ---------- URL labels ---------- */
  function titleCase(value) {
    return value.replace(/[-_]+/g, ' ').replace(/\b\w/g, character => character.toUpperCase()).trim();
  }
  function slugify(value) {
    return String(value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
      .toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '').slice(0, 80) || 'webtoon_chapter';
  }
  function guessFromUrl(value) {
    try {
      const url = new URL(value);
      const parts = url.pathname.split('/').filter(Boolean);
      if (parts.at(-1)?.toLowerCase() === 'viewer') parts.pop();
      const chapterSlug = parts.at(-1) || 'chapter';
      const seriesSlug = parts.at(-2) || 'webtoon';
      const queryKeys = ['episode_no', 'episode', 'chapter_no', 'chapter', 'ep', 'cap', 'capitulo'];
      const number = queryKeys.map(key => url.searchParams.get(key)).find(Boolean) || '';
      const match = chapterSlug.match(/^(episode|chapter|capitulo|cap|ep|ch)[-_ ]*(\d+)$/i);
      let chapterLabel = titleCase(chapterSlug);
      if (match) {
        const labels = {episode: 'Episode', chapter: 'Chapter', capitulo: 'Capitulo', cap: 'CAP', ep: 'EP', ch: 'CH'};
        chapterLabel = `${labels[match[1].toLowerCase()]} ${match[2]}`;
      } else if (number) chapterLabel = `Episode ${number}`;
      return {
        title: `${titleCase(seriesSlug)} - ${chapterLabel}`,
        slug: slugify(`${seriesSlug}_${chapterSlug}`),
      };
    } catch (_) {
      return {title: '', slug: ''};
    }
  }
  function programField(input, value) {
    appState.programmingFields = true;
    input.value = value;
    appState.programmingFields = false;
  }
  function setSourceType(value) {
    const sourceType = value === 'local_folder' ? 'local_folder' : 'url';
    appState.selectedSourceType = sourceType;
    const local = sourceType === 'local_folder';
    $$('.source-type-card').forEach(card => {
      const selected = card.dataset.sourceType === sourceType;
      card.classList.toggle('selected', selected);
      card.setAttribute('aria-pressed', String(selected));
    });
    $('#urlSourceField').hidden = local;
    $('#localFolderSourceField').hidden = !local;
    $('#urlInput').disabled = local;
    $('#localFolderInput').disabled = !local;
    const profileToggle = $('#sourceProfileToggle');
    if (profileToggle) {
      profileToggle.disabled = local;
      if (local) profileToggle.checked = false;
    }
    $('#urlError')?.classList.remove('show');
    $('#localFolderError')?.classList.remove('show');
    // The local runner consumes a complete immutable snapshot. It deliberately does not
    // accept a UI-selected subset, so make that constraint visible before submission.
    if (local) {
      appState.selectedScope = 'full';
      $$('.scope-card').forEach(card => card.classList.toggle('selected', card.dataset.scope === 'full'));
      $('#scopeCustom')?.classList.remove('open');
    }
    $$('.scope-card').forEach(card => {
      const unavailable = local && card.dataset.scope !== 'full';
      card.classList.toggle('disabled', unavailable);
      card.setAttribute('aria-disabled', String(unavailable));
    });
  }
  $$('.source-type-card').forEach(card => card.addEventListener('click', () => setSourceType(card.dataset.sourceType)));
  setSourceType(appState.selectedSourceType);
  $('#urlInput')?.addEventListener('input', event => {
    const value = event.target.value.trim();
    if (!/^https?:\/\//i.test(value)) return;
    const guess = guessFromUrl(value);
    if (!appState.nameDirty) programField($('#nameInput'), guess.title);
    if (!appState.outputDirty) programField($('#outputInput'), guess.slug);
    $('#urlError')?.classList.remove('show');
  });
  $('#localFolderInput')?.addEventListener('input', () => $('#localFolderError')?.classList.remove('show'));
  $('#nameInput')?.addEventListener('input', () => { if (!appState.programmingFields) appState.nameDirty = true; });
  $('#outputInput')?.addEventListener('input', event => {
    if (!appState.programmingFields) appState.outputDirty = true;
    const start = event.target.selectionStart;
    event.target.value = slugify(event.target.value);
    try { event.target.setSelectionRange(start, start); } catch (_) { /* unsupported */ }
  });

  /* ---------- form ---------- */
  $$('.choice-card').forEach(card => card.addEventListener('click', () => {
    $$('.choice-card').forEach(item => item.classList.remove('selected'));
    card.classList.add('selected');
    appState.selectedMode = card.dataset.mode;
    const label = $('.stage-item[data-stage="ocr"] span:nth-of-type(2)');
    if (label) label.textContent = 'Leitura do texto';
  }));
  $$('.scope-card').forEach(card => card.addEventListener('click', () => {
    if (appState.selectedSourceType === 'local_folder' && card.dataset.scope !== 'full') return;
    $$('.scope-card').forEach(item => item.classList.remove('selected'));
    card.classList.add('selected');
    appState.selectedScope = card.dataset.scope;
    $('#scopeCustom')?.classList.toggle('open', card.dataset.scope === 'custom');
  }));
  $('#cacheToggle')?.addEventListener('change', event => {
    if (event.target.checked) $('#forceToggle').checked = false;
  });
  $('#forceToggle')?.addEventListener('change', event => {
    if (event.target.checked) $('#cacheToggle').checked = false;
  });
  function validateForm() {
    const local = appState.selectedSourceType === 'local_folder';
    const url = $('#urlInput').value.trim();
    const folder = $('#localFolderInput').value.trim();
    let message = '';
    if (local) {
      if (!folder) message = 'informe a pasta local antes de iniciar';
    } else {
      if (!url) message = 'informe a URL do capítulo antes de iniciar';
      else if (!/^https?:\/\//i.test(url)) message = 'a URL precisa começar com http:// ou https://';
      try { if (!message) new URL(url); } catch (_) { message = 'essa URL não parece válida'; }
    }
    const error = local ? $('#localFolderError') : $('#urlError');
    if (message) {
      error.textContent = message;
      error.classList.add('show');
      shake(local ? $('#localFolderInput') : $('#urlInput'));
      return false;
    }
    $('#urlError')?.classList.remove('show');
    $('#localFolderError')?.classList.remove('show');
    error.classList.remove('show');
    if (appState.selectedScope === 'custom' && Number($('#scopeCustomInput').value) <= 0) {
      shake($('#scopeCustomInput'));
      showToast('Informe uma quantidade positiva de páginas.', 'error');
      return false;
    }
    return true;
  }
  function formPayload() {
    const local = appState.selectedSourceType === 'local_folder';
    const full = appState.selectedScope === 'full';
    const maxImages = full ? null : Number(appState.selectedScope === 'custom' ? $('#scopeCustomInput').value : appState.selectedScope);
    const guess = local ? {title: 'Capítulo local', slug: 'capitulo_local'} : guessFromUrl($('#urlInput').value.trim());
    const payload = {
      source_type: appState.selectedSourceType,
      chapter_name: $('#nameInput').value.trim() || guess.title,
      slug: slugify($('#outputInput').value || guess.slug),
      mode: appState.selectedMode,
      full,
      max_images: maxImages,
      use_cache: $('#cacheToggle').checked,
      force: $('#forceToggle').checked,
      use_context: $('#ctxToggle').checked,
      open_output: $('#openToggle').checked,
      create_source_profile: !local && $('#sourceProfileToggle').checked,
    };
    if (local) payload.local_folder = $('#localFolderInput').value.trim();
    else payload.url = $('#urlInput').value.trim();
    return payload;
  }
  function resetRunPreview() {
    const summary = $('#runSummary');
    if (summary) { summary.hidden = true; summary.innerHTML = ''; }
    appState.lastFinishedId = '';
    appState.previewJobId = '';
    appState.sourceReview = null;
    $('#sourceReviewPanel') && ($('#sourceReviewPanel').hidden = true);
  }

  function showStartError(error) {
    const box = $('#startError');
    if (!box) { showToast(error.message, 'error'); return; }
    const parts = [`<strong>Não foi possível iniciar</strong>`];
    if (error.stage) parts.push(`Etapa: ${escapeHtml(error.stage)}`);
    const coded = reasonText(error.code);
    if (coded) parts.push(escapeHtml(coded));
    parts.push(escapeHtml(error.message || 'Erro desconhecido.'));
    if (Array.isArray(error.hosts) && error.hosts.length) {
      parts.push(`Fontes suportadas: ${escapeHtml(error.hosts.join(', '))}`);
    }
    if (error.action) parts.push(escapeHtml(error.action));
    if (error.code) parts.push(`<code>${escapeHtml(error.code)}</code>`);
    parts.push('<button class="btn-ghost" id="startRetryBtn">Tentar novamente</button>');
    box.innerHTML = parts.join('<br>');
    box.hidden = false;
    $('#startRetryBtn')?.addEventListener('click', () => { box.hidden = true; startTranslation(); });
  }

  async function startTranslation() {
    if (!validateForm()) return;
    const button = $('#startBtn');
    if (button?.dataset.busy === '1') return;      // guards a double click in-flight
    const previousLabel = button ? button.textContent : '';
    if (button) { button.dataset.busy = '1'; button.disabled = true; button.textContent = 'Iniciando processamento…'; }
    $('#startError') && ($('#startError').hidden = true);
    // The submit only enqueues now, so the response arrives with a job to poll. Controls
    // are flipped after it, not before, and the screen shows whatever stage the worker
    // actually reports rather than guessing at "analisando fonte".

    try {
      const result = await api('/api/ui/run', {method: 'POST', body: JSON.stringify(formPayload())});
      if (!result || result.ok === false) {
        const error = new Error((result && (result.message || result.reason_code)) || 'Não foi possível analisar esta fonte com segurança.');
        error.code = result && result.reason_code;
        error.stage = (result && result.stage) || 'análise da fonte';
        error.action = (result && result.action) || 'Revise a URL e tente novamente.';
        throw error;
      }
      appState.lastFinishedId = '';
      resetRunPreview();                            // never carry the previous job's card over
      const awaitingReview = Boolean(result.awaiting_source_review);
      setRunControls(true, awaitingReview);
      if (awaitingReview) {
        renderSourceReview({
          id: result.job_id,
          source_analysis: result.analysis || {},
          source_provenance: result.source_provenance || {},
        });
        showToast('Revise as páginas encontradas antes de iniciar o OCR.', 'warn');
      } else if (result && result.worker && result.worker.online === false) {
        showToast('Job enfileirado, mas o worker não está online.', 'warn');
      } else {
        showToast(result && result.duplicate
          ? 'Este capítulo já está na fila.' : 'Pipeline real iniciado.', 'ok');
      }
      activateTab('nova');
      return result;
    } catch (error) {
      // The backend rejected it: return control to the user with a readable reason.
      if (button) { button.disabled = false; button.textContent = previousLabel || 'Iniciar tradução'; }
      showStartError(error);
    } finally {
      if (button) delete button.dataset.busy;
    }
  }
  async function cancelTranslation(queue = false, jobId = '') {
    if (appState.cancelBusy) return;
    if (!window.confirm('Deseja cancelar este processamento? Os arquivos ja produzidos serao preservados.')) return;
    appState.cancelBusy = true;
    const button = $('#runCancelAction') || $('#cancelBtn') || $('#cancelSourceReview');
    if (button) { button.disabled = true; button.textContent = 'Cancelando...'; }
    try {
      const payload = {queue};
      if (jobId) payload.job_id = jobId;
      await api('/api/ui/cancel', {method: 'POST', body: JSON.stringify(payload)});
      showToast('Cancelamento solicitado. Os arquivos parciais serao preservados.', 'warn');
      pollState();
    } catch (error) {
      showToast(error.message || 'Nao foi possivel cancelar.', 'error');
      appState.cancelBusy = false;
      if (button) { button.disabled = false; button.textContent = 'Cancelar processamento'; }
    }
  }
  function setRunControls(active, awaitingReview = false) {
    $('#startBtn').disabled = active;
    $('#startBtn').textContent = awaitingReview ? 'Aguardando revisão…' : active ? 'Processando…' : 'Iniciar tradução';
    $('#cancelBtn').disabled = !active;
    const action = $('#runCancelAction');
    if (action && !appState.cancelBusy) { action.hidden = !active; action.disabled = !active; action.textContent = 'Cancelar processamento'; }
    if (!active) appState.cancelBusy = false;
  }
  $('#startBtn')?.addEventListener('click', startTranslation);
  $('#cancelBtn')?.addEventListener('click', () => cancelTranslation(
    false, appState.sourceReview?.job_id || ''));
  $('#runCancelAction')?.addEventListener('click', () => cancelTranslation(false, appState.activeJobId || ''));
  $('#urlInput')?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); startTranslation(); }
  });
  $('#localFolderInput')?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); startTranslation(); }
  });

  /* ---------- real pipeline presentation ---------- */
  const stageOrder = ['source_analysis', 'awaiting_source_review', 'download', 'validation', 'ocr', 'translate', 'render', 'pdf'];
  const visualStageKey = key => ({source_validation: 'source_analysis', browser_loading: 'source_analysis', collecting_candidates: 'source_analysis', clustering_candidates: 'source_analysis', downloading_pages: 'download', validating_pages: 'download', classification: 'ocr', reports: 'pdf'}[key] || key);
  function renderRuntime(runtime) {
    appState.status = runtime.status || 'ready';
    appState.queue = runtime.queue || [];
    const awaitingReview = appState.status === 'awaiting_source_review';
    const running = inFlightStatuses.has(appState.status);
    const analyzing = appState.status === 'staging';
    appState.activeJobId = runtime.active?.id || runtime.source_review?.id || '';
    appState.latestJobId = runtime.latest?.id || '';
    setRunControls(running || analyzing, awaitingReview);
    const status = $('#appStatus');
    status.textContent = runStatusLabels[appState.status] || appState.status;
    status.dataset.state = appState.status;
    renderProgress(runtime.progress || {});
    renderRunStatus(runtime);
    renderQueue();
    appendLogs(runtime.logs || []);
    if (awaitingReview && runtime.source_review && shouldRenderSourceReview(runtime.source_review)) {
      renderSourceReview(runtime.source_review);
    }
    else if (!awaitingReview) $('#sourceReviewPanel') && ($('#sourceReviewPanel').hidden = true);
    if (runtime.quality_review) renderQualityReview(runtime.quality_review);
    else if ($('#qualityReviewPanel')) $('#qualityReviewPanel').hidden = true;
    if (runtime.latest && !awaitingReview) renderResult(runtime.latest);
    if (runtime.history_revision !== appState.historyRevision) refreshBootstrap();
    if (terminalRunStatuses.has(appState.status) && runtime.latest?.id && runtime.latest.id !== appState.lastFinishedId) {
      appState.lastFinishedId = runtime.latest.id;
      flashFrame();
      showToast(appState.status === 'review_required' ? 'PDF gerado, mas requer revisão de qualidade.' : 'PDF finalizado e registrado no histórico.', appState.status === 'review_required' ? 'warn' : 'ok');
    }
  }
  function renderRunStatus(runtime) {
    const card = $('#runStatusCard');
    if (!card) return;
    const active = inFlightStatuses.has(runtime.status);
    const record = runtime.active || runtime.latest || null;
    const status = runtime.status === 'ready' && record ? record.status : runtime.status;
    const progress = runtime.progress || {};
    card.hidden = !active && !record;
    if (card.hidden) return;
    const failed = status === 'failed';
    $('#runStatusHuman').textContent = failed ? 'Ocorreu um problema' : (stageMessages[progress.stage_key] || runStatusLabels[status] || 'Processamento');
    const count = progress.total ? `${progress.current || 0} de ${progress.total}` : 'progresso sendo calculado';
    $('#runProgressHuman').textContent = active ? count : (status === 'finished' ? 'PDF pronto' : status === 'review_required' ? 'Alguns itens precisam de revisao' : failed ? (reasonText(record?.reason_code) || 'O processamento nao foi concluido.') : runStatusLabels[status] || '');
    $('#runEtaHuman').textContent = active ? (progress.eta_label || 'Tempo variavel nesta etapa') : '';
    const updated = progress.updated_at ? new Date(Number(progress.updated_at) * 1000) : null;
    $('#runUpdatedHuman').textContent = updated && !Number.isNaN(updated.getTime()) ? `Atualizado ha ${Math.max(0, Math.floor((Date.now() - updated.getTime()) / 1000))}s` : '';
    $('#runNextHuman').textContent = progress.stale ? 'O processamento esta demorando mais que o esperado.' : (active ? 'Processamento em andamento' : failed ? 'Voce pode tentar novamente.' : '');
    const retry = $('#runRetryAction');
    if (retry) retry.hidden = !record || !['failed', 'cancelled'].includes(status);
    const cancel = $('#runCancelAction');
    if (cancel && !appState.cancelBusy) cancel.hidden = !active;
  }

  function renderQualityReview(review) {
    const panel = $('#qualityReviewPanel');
    const list = $('#qualityReviewList');
    if (!panel || !list) return;
    appState.qualityReview = review;
    panel.hidden = false;
    const items = Array.isArray(review.items) ? review.items : [];
    const filter = appState.qualityReviewFilter || 'pending';
    const visible = items.filter(item => filter === 'all' || (filter === 'pending' ? item.state === 'pending' : item.state !== 'pending'));
    $('#qualityReviewMeta').textContent = `${review.pending_count || 0} pendentes · ${items.length} itens · ${review.confirmed ? 'revisao confirmada' : 'confirmacao necessaria'}`;
    list.innerHTML = visible.length ? visible.map(item => {
      const actionClass = item.state === 'pending' ? ' show' : '';
      return `<article class="quality-review-item" data-state="${escapeAttr(item.state)}" data-review-key="${escapeAttr(item.key)}"><div class="quality-review-item-head"><strong>Pagina ${escapeHtml(item.page)} · ${escapeHtml(item.label)}</strong><span class="quality-review-state">${escapeHtml(item.state === 'pending' ? 'pendente' : item.state === 'preserved_original' ? 'original mantido' : 'revisado')}</span></div><div class="quality-review-reason">${escapeHtml(item.reason)}</div><div class="quality-review-text"><div><small>Original</small>${escapeHtml(item.original || '—')}</div><div><small>Traducao atual</small>${escapeHtml(item.translation || '—')}</div></div>${item.page_url ? `<img class="quality-review-thumb" src="${escapeAttr(item.page_url)}" alt="Miniatura da pagina ${escapeAttr(item.page)}" loading="lazy">` : ''}<div class="cta-row"><button type="button" class="btn-ghost review-mark${actionClass}" data-review-action="reviewed">Marcar como revisado</button><button type="button" class="btn-ghost review-preserve${actionClass}" data-review-action="preserved_original">Manter original</button></div></article>`;
    }).join('') : '<div class="muted">Nenhum item neste filtro.</div>';
    const confirm = $('#confirmQualityReview');
    if (confirm) { confirm.disabled = Number(review.pending_count || 0) > 0; confirm.title = confirm.disabled ? 'Revise cada item ou mantenha o original antes de confirmar.' : ''; }
  }

  async function qualityReviewAction(event) {
    const button = event.target.closest('[data-review-action]');
    if (!button || button.dataset.busy === '1') return;
    const item = button.closest('[data-review-key]');
    if (!item || !appState.qualityReview?.job_id) return;
    button.dataset.busy = '1'; button.disabled = true;
    try {
      const review = await api('/api/ui/quality-review/action', {method: 'POST', body: JSON.stringify({job_id: appState.qualityReview.job_id, item_key: item.dataset.reviewKey, action: button.dataset.reviewAction})});
      renderQualityReview(review); showToast(button.dataset.reviewAction === 'preserved_original' ? 'Texto original mantido.' : 'Item marcado como revisado.', 'ok');
    } catch (error) { showToast(error.message || 'Nao foi possivel atualizar o item.', 'error'); button.disabled = false; }
    finally { delete button.dataset.busy; }
  }

  async function confirmQualityReview() {
    const button = $('#confirmQualityReview');
    if (!appState.qualityReview?.job_id || !button || button.disabled || button.dataset.busy === '1') return;
    button.dataset.busy = '1'; button.disabled = true;
    try { await api('/api/ui/quality-review/confirm', {method: 'POST', body: JSON.stringify({job_id: appState.qualityReview.job_id})}); showToast('Revisao confirmada.', 'ok'); pollState(); }
    catch (error) { showToast(error.message || 'Nao foi possivel confirmar a revisao.', 'error'); button.disabled = false; }
    finally { delete button.dataset.busy; }
  }

  $('#qualityReviewList')?.addEventListener('click', qualityReviewAction);
  $$('[data-review-filter]').forEach(button => button.addEventListener('click', () => { appState.qualityReviewFilter = button.dataset.reviewFilter || 'pending'; $$('[data-review-filter]').forEach(item => item.classList.toggle('selected', item === button)); if (appState.qualityReview) renderQualityReview(appState.qualityReview); }));
  $('#confirmQualityReview')?.addEventListener('click', confirmQualityReview);
  $('#runRetryAction')?.addEventListener('click', async () => { const id = appState.latestJobId || ''; if (!id) return; try { await api('/api/ui/retry', {method: 'POST', body: JSON.stringify({job_id: id})}); showToast('Nova tentativa iniciada.', 'ok'); pollState(); } catch (error) { showToast(error.message || 'Nao foi possivel tentar novamente.', 'error'); } });

  function renderProgress(progress) {
    const runtimeKey = progress.stage_key || 'prepare';
    const key = visualStageKey(runtimeKey);
    const activeIndex = stageOrder.indexOf(key);
    $$('.stage-item').forEach(item => {
      const index = stageOrder.indexOf(item.dataset.stage);
      const pct = $('.stage-pct', item);
      const fill = $('.stage-fill', item);
      item.classList.toggle('done', key === 'final' || (activeIndex >= 0 && index < activeIndex));
      item.classList.toggle('active', item.dataset.stage === key && (appState.status === 'running' || appState.status === 'staging' || appState.status === 'awaiting_source_review'));
      item.classList.toggle('indeterminate', item.classList.contains('active') && progress.indeterminate);
      if (item.classList.contains('done')) { pct.textContent = '100%'; fill.style.width = '100%'; }
      else if (item.classList.contains('active') && progress.fraction != null) {
        const percent = Math.max(0, Math.min(100, Math.round(progress.fraction * 100)));
        pct.textContent = progress.total ? `${progress.current}/${progress.total} · ${percent}%` : `${percent}%`;
        fill.style.width = `${percent}%`;
      } else if (item.classList.contains('active')) { pct.textContent = 'em andamento'; fill.style.width = '38%'; }
      else { pct.textContent = '—'; fill.style.width = '0%'; }
    });
    if (key !== appState.activeStage) {
      appState.activeStage = key;
      sfxPop(runtimeKey);
    }
    $('#balloonText').textContent = stageMessages[runtimeKey] || 'aguardando início';
    $('#scanline')?.classList.toggle('run', appState.status === 'running' && ['ocr', 'classification', 'render'].includes(runtimeKey));
    const summary = $('#runSummary');
    if (appState.status === 'running') {
      summary.hidden = false;
      const counterOwner = progress.counter_stage || progress.stage_key || '';
      const ownsCounter = !counterOwner || counterOwner === (progress.stage_key || '');
      const count = (ownsCounter && progress.total)
        ? `${progress.current}/${progress.total}`
        : 'contador indisponível';
      // elapsed_label is authoritative: the server freezes it and emits 'Tempo indisponível'
      // for an unusable timestamp. Never substitute a locally computed number here.
      const elapsed = progress.elapsed_label || 'Tempo indisponível';
      const stale = progress.stale ? `<br><em>${escapeHtml(progress.stale_label || 'Sem atualização recente')}</em>` : '';
      summary.innerHTML = `<strong>${escapeHtml(progress.stage || 'Preparando')}</strong><br>${escapeHtml(count)} · ${escapeHtml(elapsed)}${stale}<br>${escapeHtml(progress.last_message || '')}`;
    } else if (appState.status === 'awaiting_source_review') {
      summary.hidden = false;
      summary.innerHTML = '<strong>Revisão das páginas necessária</strong><br>O OCR ainda não foi iniciado.';
    }
  }
  function shouldRenderSourceReview(record) {
    const incomingJobId = String(record?.id || record?.job_id || '');
    // Source analyses are frozen while awaiting review. Replacing this DOM on every polling
    // tick would silently restore removed pages and discard an intentional manual reorder.
    return !appState.sourceReview || appState.sourceReview.job_id !== incomingJobId;
  }
  function renderSourceReview(record) {
    const analysis = record?.source_analysis || record?.analysis || {};
    const provenance = record?.source_provenance || {};
    const panel = $('#sourceReviewPanel');
    if (!panel) return;
    const accepted = Array.isArray(analysis.accepted) ? analysis.accepted : [];
    appState.sourceReview = {job_id: record?.id || record?.job_id || '', analysis};
    panel.hidden = false;
    const warnings = Array.isArray(analysis.warnings) && analysis.warnings.length ? ` · avisos: ${analysis.warnings.map(escapeHtml).join(', ')}` : '';
    const safeReason = value => /^[a-z][a-z0-9_]{0,79}$/.test(String(value || '')) ? String(value) : '';
    const reasons = new Map();
    (Array.isArray(analysis.discarded) ? analysis.discarded : []).forEach(item => {
      const reason = safeReason(item?.reason);
      if (reason) reasons.set(reason, (reasons.get(reason) || 0) + 1);
    });
    const reasonText = Array.from(reasons.entries()).slice(0, 6)
      .map(([reason, count]) => `${escapeHtml(reason)} (${count})`).join(', ');
    const outcome = safeReason(analysis.outcome);
    const adapter = provenance.adapter_name || analysis.adapter || 'universal';
    const version = provenance.adapter_version || analysis.adapter_version || '—';
    const transport = provenance.transport_name || 'pending';
    const score = provenance.score ?? analysis.confidence ?? '—';
    const candidates = Number(provenance.candidate_count ?? analysis.candidate_count ?? accepted.length);
    const acceptedCount = Number(provenance.accepted_page_count ?? analysis.accepted_count ?? accepted.length);
    const rejectedCount = Number(provenance.rejected_page_count ?? analysis.discarded_count ?? 0);
    $('#sourceReviewMeta').innerHTML = `Adapter: <strong>${escapeHtml(adapter)}</strong> v${escapeHtml(version)} · transporte: <strong>${escapeHtml(transport)}</strong> · confiança: <strong>${escapeHtml(score)}</strong> · ${candidates} candidatos · ${acceptedCount} páginas aceitas · ${rejectedCount} descartadas${outcome ? ` · resultado: <code>${escapeHtml(outcome)}</code>` : ''}${reasonText ? ` · motivos: ${reasonText}` : ''}${warnings}`;
    $('#sourceReviewList').innerHTML = accepted.length ? accepted.map((item, index) => `<label class="source-page-option"><input type="checkbox" data-source-candidate-id="${escapeAttr(item.id || '')}" checked><span><strong>Página ${escapeHtml(Number(item.order || index + 1))}</strong><span>${escapeHtml(item.width || '—')} × ${escapeHtml(item.height || '—')} · ${escapeHtml(item.origin || 'dom')}</span></span></label>`).join('') : '<div class="muted">Nenhuma página confirmável foi preservada.</div>';
    prepareSourceReviewControls();
    $('#confirmSourcePages').disabled = !accepted.length;
  }
  function safeReviewThumbnail(value) {
    const thumbnail = String(value || '').trim();
    return /^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/=]{16,24000}$/i.test(thumbnail) ? thumbnail : '';
  }
  function prepareSourceReviewControls() {
    const list = $('#sourceReviewList');
    if (!list) return;
    const thumbnails = new Map(
      (appState.sourceReview?.analysis?.accepted || [])
        .map(item => [String((item && item.id) || ''), safeReviewThumbnail((item && item.thumbnail) || '')])
        .filter(([id, thumbnail]) => id && thumbnail));
    $$('.source-page-option', list).forEach(option => {
      const card = document.createElement('div');
      card.className = 'source-page-review-card';
      option.parentNode?.insertBefore(card, option);
      option.classList.add('source-page-select');
      card.appendChild(option);
      const candidateId = option.querySelector('[data-source-candidate-id]')?.dataset.sourceCandidateId || '';
      const thumbnail = thumbnails.get(candidateId);
      if (thumbnail) {
        const preview = document.createElement('img');
        preview.className = 'source-page-thumbnail';
        preview.src = thumbnail;
        preview.alt = 'Miniatura local da pagina detectada';
        preview.decoding = 'async';
        preview.loading = 'lazy';
        option.insertBefore(preview, option.querySelector('span'));
      }
      const position = document.createElement('small');
      position.dataset.sourceReviewPosition = '';
      option.querySelector('span')?.appendChild(position);
      const controls = document.createElement('div');
      controls.className = 'source-page-order-actions';
      controls.setAttribute('aria-label', 'Ordem da pagina');
      controls.innerHTML = '<button type="button" class="btn-ghost source-page-order-button" data-source-move="up" aria-label="Mover para cima">&uarr;</button><button type="button" class="btn-ghost source-page-order-button" data-source-move="down" aria-label="Mover para baixo">&darr;</button>';
      card.appendChild(controls);
    });
    refreshSourceReviewOrder();
  }
  function refreshSourceReviewOrder() {
    const entries = $$('.source-page-review-card', $('#sourceReviewList'));
    entries.forEach((entry, index) => {
      const position = entry.querySelector('[data-source-review-position]');
      if (position) position.textContent = `Posicao de processamento ${index + 1}`;
      const up = entry.querySelector('[data-source-move="up"]');
      const down = entry.querySelector('[data-source-move="down"]');
      if (up) up.disabled = index === 0;
      if (down) down.disabled = index === entries.length - 1;
    });
  }
  function moveSourceReviewPage(event) {
    const button = event.target.closest('[data-source-move]');
    if (!button) return;
    const entry = button.closest('.source-page-review-card');
    const list = $('#sourceReviewList');
    if (!entry || !list) return;
    if (button.dataset.sourceMove === 'up') {
      const previous = entry.previousElementSibling;
      if (previous) list.insertBefore(entry, previous);
    } else if (button.dataset.sourceMove === 'down') {
      const next = entry.nextElementSibling;
      if (next) list.insertBefore(next, entry);
    }
    refreshSourceReviewOrder();
  }
  async function confirmSourcePages() {
    const review = appState.sourceReview;
    const ids = $$('.source-page-option input:checked').map(input => input.dataset.sourceCandidateId).filter(Boolean);
    if (!review?.job_id || !ids.length) { showToast('Selecione ao menos uma página.', 'warn'); return; }
    const button = $('#confirmSourcePages');
    if (button) button.disabled = true;
    try {
      const result = await api('/api/ui/source/confirm', {method: 'POST', body: JSON.stringify({job_id: review.job_id, candidate_ids: ids})});
      if (!result?.ok) throw new Error('Não foi possível confirmar as páginas.');
      $('#sourceReviewPanel').hidden = true;
      appState.sourceReview = null;
      showToast('Páginas confirmadas. O processamento foi enfileirado.', 'ok');
    } catch (error) {
      showToast(error.message || 'Não foi possível confirmar as páginas.', 'error');
      if (button) button.disabled = false;
    }
  }
  async function retrySourceReview() {
    const review = appState.sourceReview;
    if (!review?.job_id) return;
    const button = $('#retrySourceReview');
    if (button) button.disabled = true;
    try {
      const result = await api('/api/ui/source/retry', {
        method: 'POST', body: JSON.stringify({job_id: review.job_id}),
      });
      if (!result?.ok) throw new Error(result?.reason_code || 'A nova análise foi recusada.');
      appState.sourceReview = null;
      if (result.awaiting_source_review) {
        renderSourceReview({
          id: result.job_id,
          source_analysis: result.analysis || {},
          source_provenance: result.source_provenance || {},
        });
        showToast('Análise refeita. Revise as páginas novamente.', 'warn');
      } else {
        $('#sourceReviewPanel').hidden = true;
        showToast('Análise refeita e processamento enfileirado.', 'ok');
      }
    } catch (error) {
      showToast(error.message || 'Não foi possível repetir a análise.', 'error');
      if (button) button.disabled = false;
    }
  }
  $('#confirmSourcePages')?.addEventListener('click', confirmSourcePages);
  $('#retrySourceReview')?.addEventListener('click', retrySourceReview);
  $('#sourceReviewList')?.addEventListener('click', moveSourceReviewPage);
  $('#cancelSourceReview')?.addEventListener('click', () => cancelTranslation(
    false, appState.sourceReview?.job_id || ''));
  function renderResult(record) {
    const summary = $('#runSummary');
    if (!record || record.status === 'running' || record.status === 'staging' || record.status === 'awaiting_source_review') return;
    summary.hidden = false;
    const gateValue = boolish(record.quality_gate);
    const gate = gateValue === true ? 'aprovado' : gateValue === false ? 'reprovado' : 'não informado';
    summary.innerHTML = `<strong>${escapeHtml(record.chapter_name || record.slug || 'Capítulo')}</strong><br>${Number(record.pages_processed || 0)} páginas · ${Number(record.groups_translated || 0)} grupos · ${formatSeconds(record.total_seconds)}<br>${Number(record.errors || 0)} erros · gate ${gate}`;
    renderArtifactButtons($('#artifactActions'), record);
  }

  /* ---------- logs ---------- */
  function appendLogs(entries) {
    const terminal = $('#terminal');
    entries.forEach(entry => {
      appState.cursor = Math.max(appState.cursor, Number(entry.seq || 0));
      appState.logs.push(entry);
      if (Number(entry.seq || 0) <= appState.visualLogClearedAt) return;
      const row = document.createElement('div');
      row.innerHTML = `<span class="l-time">${escapeHtml(entry.time || '')}</span><span class="log-${escapeAttr(entry.kind || 'info')}">${escapeHtml(entry.text || '')}</span>`;
      terminal.appendChild(row);
    });
    appState.logs = appState.logs.slice(-3000);
    if (entries.length) terminal.scrollTop = terminal.scrollHeight;
  }
  $('#copyLogsBtn')?.addEventListener('click', async () => {
    const text = appState.logs.map(entry => `[${entry.time}] ${entry.text}`).join('\n');
    try { await navigator.clipboard.writeText(text); showToast('Logs copiados.', 'ok'); }
    catch (_) { showToast('Não foi possível copiar os logs.', 'error'); }
  });
  $('#clearLogsBtn')?.addEventListener('click', () => {
    $('#terminal').innerHTML = '';
    appState.visualLogClearedAt = appState.cursor;
  });

  /* ---------- history ---------- */
  function seriesFromRecord(record) {
    if (record.series_name) return String(record.series_name);
    const title = String(record.chapter_name || record.slug || '');
    const explicit = title.split(/\s[-—]\s/)[0].trim();
    try {
      const parts = new URL(record.url || '').pathname.split('/').filter(Boolean);
      if (parts.at(-1)?.toLowerCase() === 'viewer') parts.pop();
      const fromUrl = parts.at(-2);
      if (fromUrl) return titleCase(fromUrl);
    } catch (_) { /* history discovered without URL */ }
    return explicit || 'Sem série identificada';
  }
  function formatSeconds(value) {
    if (value === null || value === undefined || value === '') return 'Tempo indisponível';
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return 'Tempo indisponível';
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}min`;
    if (minutes) return `${minutes}min ${String(whole % 60).padStart(2, '0')}s`;
    return `${whole}s`;
  }
  function actionButton(label, action, path = '') {
    if (!path && action !== 'reprocess') return '';
    return `<button class="btn-ghost" data-action="${action}" data-path="${encodeURIComponent(path || '')}">${label}</button>`;
  }
  function publicationEligibility(record) {
    const terminal = terminalRunStatuses.has(String(record.status || '').toLowerCase());
    const hasPdf = Boolean(record.pdf_path);
    const manifest = record.output_verification === 'manifest_verified' || Boolean(record.manifest_path);
    const authenticated = Boolean(
      appState.bootstrap?.community?.authenticated ||
      window.__tradutorCommunityAuthenticated ||
      window.__tradutorAccessToken,
    );
    const review = String(record.status || '').toLowerCase() === 'review_required' || boolish(record.quality_gate) === false;
    const published = String(record.publication_status || '').toLowerCase() === 'published';
    const changed = published && record.publication_pdf_sha256 && record.pdf_sha256 && record.publication_pdf_sha256 !== record.pdf_sha256;
    return {terminal, hasPdf, manifest, authenticated, review, published, changed,
      eligible: hasPdf && manifest && terminal && authenticated};
  }
  function publicationAction(record) {
    const eligibility = publicationEligibility(record);
    if (!eligibility.hasPdf) return '';
    if (!eligibility.authenticated) return '<button class="btn-ghost" data-action="publish" disabled title="Entre para publicar">Publicação indisponível</button>';
    if (!eligibility.manifest || !eligibility.terminal) return '<button class="btn-ghost" data-action="publish" disabled>Publicação indisponível</button>';
    if (eligibility.published && !eligibility.changed) return '<button class="btn-ghost" data-action="publish">Publicado</button>';
    return `<button class="btn-ghost" data-action="publish">${eligibility.changed ? 'Atualizar publicação' : 'Publicar na comunidade'}</button>`;
  }
  function renderHistoryCard(record) {
    const title = record.chapter_name || record.slug || 'Capítulo';
    const engine = record.mode === 'fast' ? 'rapid' : 'paddle';
    const gateValue = boolish(record.quality_gate);
    const gate = gateValue === true ? 'gate aprovado' : gateValue === false ? 'gate reprovado' : 'gate pendente';
    const provenance = record.output_verification === 'legacy_unverified' ? 'origem não verificada' : record.output_verification === 'e2e_evidence' ? 'evidência E2E' : record.output_verification === 'manifest_verified' ? 'manifest verificado' : 'origem não informada';
    const meta = `${Number(record.pages_processed || 0)} páginas · ${Number(record.groups_translated || 0)} grupos · ${formatSeconds(record.total_seconds)} · ${gate} · ${provenance}`;
    return `<div class="hist-item" data-id="${escapeAttr(record.id || '')}">
      <div class="hist-cover" style="background:${engine === 'rapid' ? '#2f7a6b' : '#c9a227'}">${escapeHtml(title.slice(0, 1).toUpperCase())}</div>
      <div class="hist-meta"><div class="hm-title">${escapeHtml(title)}</div><div class="hm-sub">${escapeHtml(meta)}</div>
      <div class="hm-badges"><span class="badge ep">${escapeHtml(runStatusLabels[record.status] || record.status || 'local')}</span><span class="badge ${engine}">${engine === 'rapid' ? 'Rápido' : 'Qualidade'}</span></div></div>
      <div class="hm-actions">${actionButton('Abrir PDF', 'open', record.pdf_path)}${actionButton('Pasta', 'open', record.output_folder)}${actionButton('Relatório', 'open', record.quality_report_path)}${actionButton('Compare', 'open', record.compare_sheet_path)}${actionButton('Contexto', 'open', record.session_context_path)}${actionButton('Reprocessar', 'reprocess')}${publicationAction(record)}</div>
    </div>`;
  }
  function renderHistory() {
    const list = $('#histList');
    if (!list) return;
    const query = ($('#histSearch')?.value || '').trim().toLowerCase();
    const records = appState.history.filter(record => !query || `${record.chapter_name || ''} ${record.slug || ''}`.toLowerCase().includes(query));
    $('#histCount').textContent = query ? `${records.length} de ${appState.history.length}` : `${records.length} ${records.length === 1 ? 'capítulo' : 'capítulos'}`;
    if (!records.length) {
      list.innerHTML = `<div class="empty-real-state">${appState.history.length ? 'nenhum capítulo corresponde à busca' : 'nenhum capítulo real no histórico local'}</div>`;
      return;
    }
    const groups = new Map();
    records.forEach(record => {
      const series = seriesFromRecord(record);
      const key = series.toLowerCase();
      if (!groups.has(key)) groups.set(key, {series, records: []});
      groups.get(key).records.push(record);
    });
    list.innerHTML = Array.from(groups.entries()).map(([key, group]) => {
      const open = appState.expandedFolders.has(key) || groups.size === 1;
      const folderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h8l2 2"/></svg>';
      return `<div class="community-folder ${open ? 'open' : ''}" data-folder="${escapeAttr(key)}"><div class="cf-header" data-folder="${escapeAttr(key)}"><span class="cf-icon">${folderIcon}</span><span class="cf-name">${escapeHtml(group.series)}</span><span class="cf-count">${group.records.length} ${group.records.length === 1 ? 'capítulo' : 'capítulos'}</span><span class="cf-chevron">⌄</span></div><div class="cf-body">${group.records.map(renderHistoryCard).join('')}</div></div>`;
    }).join('');
  }
  $('#histSearch')?.addEventListener('input', renderHistory);
  window.addEventListener('tradutor-auth-changed', () => renderHistory());
  $('#histList')?.addEventListener('click', async event => {
    const folder = event.target.closest('.cf-header');
    if (folder) {
      const key = folder.dataset.folder;
      appState.expandedFolders.has(key) ? appState.expandedFolders.delete(key) : appState.expandedFolders.add(key);
      renderHistory();
      return;
    }
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const record = appState.history.find(item => String(item.id) === String(button.closest('.hist-item')?.dataset.id));
    if (!record) return;
    if (button.dataset.action === 'reprocess') { loadRecordIntoForm(record); return; }
    if (button.dataset.action === 'publish') { openPublicationModal(record); return; }
    await openArtifact(decodeURIComponent(button.dataset.path || ''));
  });

  /* ---------- community publishing ---------- */
  function closePublicationModal() {
    const overlay = $('#publicationModalOverlay');
    if (appState.publicationRecord && overlay?.classList.contains('show')) {
      const key = String(appState.publicationRecord.id || appState.publicationRecord.slug || '');
      if (key) appState.publicationDrafts[key] = {
        title: String($('#publicationTitle')?.value || ''),
        description: String($('#publicationDescription')?.value || ''),
        tags: String($('#publicationTags')?.value || ''),
        visibility: $('#publicationVisibility')?.value === 'private' ? 'private' : 'public',
      };
    }
    if (overlay) { overlay.classList.remove('show'); overlay.setAttribute('aria-hidden', 'true'); }
    appState.publicationRecord = null;
    appState.publicationBusy = false;
  }
  function openPublicationModal(record) {
    const eligibility = publicationEligibility(record);
    if (!eligibility.eligible) {
      showToast(eligibility.authenticated ? 'Publicação indisponível para este resultado.' : 'Entre para publicar na comunidade.', 'warn');
      return;
    }
    const overlay = $('#publicationModalOverlay');
    const summary = $('#publicationSummary');
    const pending = $('#publicationPending');
    const form = $('#publicationForm');
    if (!overlay || !summary || !form) return;
    appState.publicationRecord = record;
    appState.publicationBusy = false;
    const draftKey = String(record.id || record.slug || '');
    const draft = appState.publicationDrafts[draftKey] || {};
    const review = eligibility.review;
    const pendingCount = Number(record.manual_review_count || record.rejected_count || 0);
    summary.innerHTML = [
      ['Obra', record.series_name || record.chapter_name || '—'],
      ['Capítulo', record.chapter_name || record.slug || '—'],
      ['Páginas', Number(record.pages_processed || 0)],
      ['Qualidade', review ? 'revisão necessária' : 'gate aprovado'],
    ].map(([label, value]) => `<div class="pub-meta"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`).join('');
    pending.hidden = !review;
    pending.textContent = review
      ? `Este capítulo tem ${pendingCount || 'pendências de'} revisão. Confirme que você conferiu o resultado antes de publicar.`
      : '';
    $('#publicationTitle').value = draft.title || record.chapter_name || record.slug || '';
    $('#publicationDescription').value = draft.description || '';
    $('#publicationTags').value = draft.tags || (Array.isArray(record.publication_tags || record.tags)
      ? (record.publication_tags || record.tags).join(', ')
      : String(record.publication_tags || record.tags || ''));
    $('#publicationVisibility').value = draft.visibility || 'public';
    $('#publicationConfirm').checked = false;
    $('#publicationSubmit').textContent = eligibility.published ? 'Atualizar publicação' : 'Publicar';
    $('#publicationError').hidden = true;
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    $('#publicationTitle')?.focus();
  }
  async function publishToCommunity(record) {
    if (appState.publicationBusy) return;
    const eligibility = publicationEligibility(record);
    if (!eligibility.eligible) return;
    const trustedJobId = /^[0-9a-f]{32}$/.test(String(record.job_id || '')) ? String(record.job_id) : '';
    const guess = guessFromUrl(record.url || '');
    const payload = {
      slug: record.slug || guess.slug,
      series_title: record.series_name || record.chapter_name || '',
      series_slug: record.series_slug || guess.slug,
      episode_number: record.episode_number || '',
      title: String($('#publicationTitle')?.value || record.chapter_name || '').trim(),
      description: String($('#publicationDescription')?.value || '').slice(0, 2000),
      tags: String($('#publicationTags')?.value || '').split(',').map(value => value.trim()).filter(Boolean).slice(0, 20),
      visibility: $('#publicationVisibility')?.value === 'private' ? 'private' : 'public',
    };
    if (trustedJobId) payload.source_job_id = trustedJobId;
    appState.publicationBusy = true;
    const submit = $('#publicationSubmit');
    if (submit) { submit.disabled = true; submit.textContent = 'Publicando…'; }
    try {
      await api('/api/community/publish', {method: 'POST', body: JSON.stringify(payload)});
      record.publication_status = 'published';
      record.publication_tags = payload.tags;
      const key = String(record.id || record.slug || '');
      if (key) delete appState.publicationDrafts[key];
      closePublicationModal();
      showToast('Publicação enviada à fila. O worker fará o upload.', 'ok');
      renderHistory();
      await loadCommunityFeed();
    } catch (errorValue) {
      const error = $('#publicationError');
      if (error) { error.textContent = errorValue.message || 'Não foi possível publicar.'; error.hidden = false; }
      appState.publicationBusy = false;
      if (submit) { submit.disabled = false; submit.textContent = eligibility.published ? 'Atualizar publicação' : 'Publicar'; }
    }
  }
  $('#publicationCancel')?.addEventListener('click', closePublicationModal);
  $('#publicationModalClose')?.addEventListener('click', closePublicationModal);
  $('#publicationModalOverlay')?.addEventListener('click', event => {
    if (event.target === $('#publicationModalOverlay')) closePublicationModal();
  });
  $('#publicationForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    if (!$('#publicationConfirm')?.checked) return;
    if (appState.publicationRecord) await publishToCommunity(appState.publicationRecord);
  });

  async function loadCommunityFeed() {
    const container = $('#communityFeed');
    if (!container) return;
    try {
      const data = await api('/api/community/posts');
      const posts = (data && data.posts) || [];
      if (!posts.length) { container.innerHTML = '<div class="empty-real-state">nenhuma publicação na comunidade ainda</div>'; return; }
      container.innerHTML = posts.map(renderCommunityCard).join('');
    } catch (error) { container.innerHTML = `<div class="empty-real-state">${escapeHtml(error.message)}</div>`; }
  }

  $('#communityRefreshBtn')?.addEventListener('click', loadCommunityFeed);

  function renderCommunityCard(post) {
    const title = post.title || post.series_title || 'Capítulo';
    const sub = `${escapeHtml(post.series_title || '')} · ep ${escapeHtml(String(post.episode_number || ''))} · ${Number(post.views || 0)} leituras`;
    const pdfUrl = `/api/community/posts/${encodeURIComponent(post.post_id)}/pdf`;
    return `<div class="hist-item"><div class="hist-cover" style="background:#b8557a">${escapeHtml(title.slice(0,1).toUpperCase())}</div>
      <div class="hist-meta"><div class="hm-title">${escapeHtml(title)}</div><div class="hm-sub">${sub}</div></div>
      <div class="hm-actions"><a class="btn-ghost" href="${pdfUrl}" target="_blank" rel="noopener">Abrir PDF</a></div></div>`;
  }
  function loadRecordIntoForm(record) {
    const local = record.source_type === 'local_folder';
    setSourceType(local ? 'local_folder' : 'url');
    appState.programmingFields = true;
    // A persisted local job has only an opaque snapshot reference. Never reconstruct or
    // display a source filesystem path when loading history back into the form.
    $('#urlInput').value = local ? '' : (record.url || '');
    $('#localFolderInput').value = '';
    $('#nameInput').value = record.chapter_name || '';
    $('#outputInput').value = record.slug || '';
    appState.programmingFields = false;
    appState.nameDirty = true;
    appState.outputDirty = true;
    appState.selectedMode = record.mode === 'quality' ? 'quality' : 'fast';
    $$('.choice-card').forEach(card => card.classList.toggle('selected', card.dataset.mode === appState.selectedMode));
    const scope = local ? 'full' : (record.max_images ? String(record.max_images) : 'full');
    const direct = $(`.scope-card[data-scope="${scope}"]`);
    const chosen = direct || $('.scope-card[data-scope="custom"]');
    $$('.scope-card').forEach(card => card.classList.toggle('selected', card === chosen));
    appState.selectedScope = direct ? scope : 'custom';
    if (!direct && record.max_images) $('#scopeCustomInput').value = Number(record.max_images);
    $('#scopeCustom').classList.toggle('open', appState.selectedScope === 'custom');
    $('#cacheToggle').checked = true;
    $('#forceToggle').checked = false;
    activateTab('nova');
    showToast('Execução carregada para revisão.', 'ok');
  }

  /* ---------- artifact actions ---------- */
  function renderArtifactButtons(container, record) {
    if (!container) return;
    container.innerHTML = [
      ['PDF', record.pdf_path], ['Pasta', record.output_folder], ['Qualidade', record.quality_report_path],
      ['Compare', record.compare_sheet_path], ['Contexto', record.session_context_path],
    ].filter(([, path]) => path).map(([label, path]) => `<button class="btn-ghost" data-path="${encodeURIComponent(path)}">${label}</button>`).join('');
  }
  $('#artifactActions')?.addEventListener('click', event => {
    const button = event.target.closest('[data-path]');
    if (button) openArtifact(decodeURIComponent(button.dataset.path));
  });
  async function openArtifact(path) {
    if (!path) return;
    try { await api('/api/ui/open', {method: 'POST', body: JSON.stringify({path})}); }
    catch (error) { showToast(error.message, 'error'); }
  }

  /* ---------- queue ---------- */
  function queuePayload(url) {
    const guess = guessFromUrl(url);
    return {
      url, chapter_name: guess.title, slug: guess.slug, mode: appState.selectedMode,
      full: true, use_cache: $('#cacheToggle').checked, force: $('#forceToggle').checked,
      use_context: $('#ctxToggle').checked, open_output: false,
    };
  }
  async function addQueueItem() {
    const input = $('#queueUrlInput');
    const url = input.value.trim();
    if (!/^https?:\/\//i.test(url)) { shake(input); showToast('Cole uma URL válida.', 'error'); return; }
    try {
      await api('/api/ui/queue/add', {method: 'POST', body: JSON.stringify(queuePayload(url))});
      input.value = '';
      await pollState();
      showToast('Capítulo adicionado à fila real.', 'ok');
    } catch (error) { showToast(error.message, 'error'); }
  }
  function renderQueue() {
    const list = $('#queueList');
    if (!list) return;
    const items = appState.queue || [];
    const completed = items.filter(item => terminalRunStatuses.has(item.status)).length;
    $('#queueCount').textContent = items.length ? `${completed} de ${items.length} concluídos` : 'fila vazia';
    $('#queueProgressFill').style.width = items.length ? `${Math.round((completed / items.length) * 100)}%` : '0%';
    const checkIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 12 4 4 8-9"/></svg>';
    const closeIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m6 6 12 12M18 6 6 18"/></svg>';
    const queueClass = status => status === 'finished' ? 'done' : status === 'review_required' ? 'review_required' : escapeAttr(status);
    list.innerHTML = items.length ? items.map((item, index) => `<div class="queue-item status-${queueClass(item.status)}" data-id="${escapeAttr(item.id)}"><span class="qi-num">${terminalRunStatuses.has(item.status) ? checkIcon : index + 1}</span><span class="qi-url">${escapeHtml(item.chapter_name || item.url)}</span><span class="qi-status">${runStatusLabels[item.status] || item.status}</span>${item.status === 'waiting' ? `<button class="qi-remove" data-id="${escapeAttr(item.id)}" aria-label="Remover da fila">${closeIcon}</button>` : ''}</div>`).join('') : '<div class="empty-real-state">fila vazia · adicione uma URL acima</div>';
  }
  $('#queueAddBtn')?.addEventListener('click', addQueueItem);
  $('#queueUrlInput')?.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); addQueueItem(); } });
  $('#queueList')?.addEventListener('click', async event => {
    const button = event.target.closest('.qi-remove');
    if (!button) return;
    await api('/api/ui/queue/remove', {method: 'POST', body: JSON.stringify({id: button.dataset.id})});
    await pollState();
  });
  $('#queueClearBtn')?.addEventListener('click', async () => { await api('/api/ui/queue/clear', {method: 'POST'}); await pollState(); });
  $('#queueStartBtn')?.addEventListener('click', async () => {
    try { await api('/api/ui/queue/start', {method: 'POST'}); showToast('Fila real iniciada.', 'ok'); }
    catch (error) { showToast(error.message, 'error'); }
  });
  $('#queueCancelBtn')?.addEventListener('click', () => cancelTranslation(true));

  /* ---------- dashboard ---------- */
  function animateCount(element, target) {
    if (!element) return;
    const start = performance.now();
    const initial = Number(element.textContent || 0);
    const tick = now => {
      const progress = Math.min((now - start) / 500, 1);
      element.textContent = Math.round(initial + (target - initial) * (1 - Math.pow(1 - progress, 3)));
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  function renderDashboard() {
    const hour = new Date().getHours();
    $('#dashGreeting').textContent = `${hour < 12 ? 'Bom dia' : hour < 18 ? 'Boa tarde' : 'Boa noite'}. Seu painel mostra somente dados locais reais.`;
    $('#dashStreak').textContent = '';
    const series = new Map();
    appState.history.forEach(record => {
      const name = seriesFromRecord(record);
      const key = String(record.series_slug || name).toLowerCase();
      if (!series.has(key)) series.set(key, {name, records: []});
      series.get(key).records.push(record);
    });
    animateCount($('#dashChapters'), appState.history.length);
    animateCount($('#dashSeries'), series.size);
    animateCount($('#dashPages'), appState.history.reduce((total, record) => total + Number(record.pages_processed || 0), 0));
    animateCount($('#dashApproved'), appState.history.filter(record => boolish(record.quality_gate) === true).length);
    const list = $('#dashSeriesList');
    const query = appState.seriesQuery.toLowerCase();
    const entries = Array.from(series.values()).map(group => {
      group.records.sort((a, b) => String(b.finished_at || b.started_at || '').localeCompare(String(a.finished_at || a.started_at || '')));
      return group;
    }).filter(group => !query || group.name.toLowerCase().includes(query));
    entries.sort((a, b) => appState.seriesSort === 'az'
      ? a.name.localeCompare(b.name, 'pt-BR')
      : appState.seriesSort === 'chapters'
        ? b.records.length - a.records.length
        : String(b.records[0]?.finished_at || b.records[0]?.started_at || '').localeCompare(String(a.records[0]?.finished_at || a.records[0]?.started_at || '')));
    $('#seriesCount').textContent = `${entries.length} ${entries.length === 1 ? 'série' : 'séries'}`;
    const arrow = '<svg class="series-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>';
    list.className = 'series-library';
    list.innerHTML = entries.length ? entries.map(group => {
      const latest = group.records[0] || {};
      const date = latest.finished_at || latest.started_at;
      const activity = date ? new Date(date).toLocaleDateString('pt-BR', {day:'2-digit', month:'short'}) : 'atividade local';
      return `<button class="series-card" data-series="${escapeAttr(group.name)}"><span class="series-monogram">${escapeHtml(group.name.slice(0,1).toUpperCase())}</span><span class="series-copy"><strong>${escapeHtml(group.name)}</strong><span>${group.records.length} ${group.records.length === 1 ? 'capítulo' : 'capítulos'} · ${escapeHtml(activity)}</span></span>${arrow}</button>`;
    }).join('') : '<div class="empty-real-state">nenhuma série encontrada</div>';
    const activity = $('#dashActivityList');
    activity.className = 'activity-list';
    activity.innerHTML = appState.history.length ? appState.history.slice(0, 7).map(record => {
      const status = record.status === 'error' ? 'erro no processamento' : record.status === 'review_required' || boolish(record.quality_gate) === false ? 'revisão necessária' : record.pdf_path ? 'PDF gerado' : 'tradução concluída';
      const date = record.finished_at || record.started_at;
      const when = date ? new Date(date).toLocaleDateString('pt-BR', {day:'2-digit', month:'short'}) : 'local';
      return `<div class="activity-item"><span class="activity-mark"></span><span class="activity-copy"><strong>${escapeHtml(record.chapter_name || record.slug || 'Capítulo')}</strong><span>${escapeHtml(status)}</span></span><span class="activity-time">${escapeHtml(when)}</span></div>`;
    }).join('') : '<div class="empty-real-state">suas traduções recentes aparecem aqui</div>';
  }
  $('#seriesSearch')?.addEventListener('input', event => { appState.seriesQuery = event.target.value.trim(); renderDashboard(); });
  $('#seriesSort')?.addEventListener('change', event => { appState.seriesSort = event.target.value; renderDashboard(); });
  $('#dashSeriesList')?.addEventListener('click', event => {
    const item = event.target.closest('[data-series]');
    if (!item) return;
    $('#histSearch').value = item.dataset.series;
    activateTab('hist');
  });
  $('#dashGotoHistory')?.addEventListener('click', () => activateTab('hist'));

  /* ---------- settings ---------- */
  function trueValue(value) { return String(value).toLowerCase() === 'true'; }
  function renderSettings(settings) {
    const apiReady = Boolean(settings.nvidia_configured);
    $('#railApiStatus').innerHTML = `<span class="dot"></span>${apiReady ? 'serviço conectado' : 'configuração necessária'}`;
    $('#railApiStatus').parentElement?.classList.toggle('is-error', !apiReady);
    $('#settingServiceFriendly').textContent = apiReady ? 'Conectado' : 'Não configurado';
    $('#settingModeFriendly').textContent = 'Rápido';
    $('#settingReadingFriendly').textContent = settings.paddle_available || settings.rapidocr_available ? 'Disponível' : 'Indisponível';
    $('#settingParallelFriendly').textContent = trueValue(settings.ocr_parallel) ? 'Ativo' : 'Automático';
    $('#settingContextFriendly').textContent = 'Ativo';
    $('#settingTranslationMode').textContent = settings.translation_mode || '—';
    $('#settingApiKey').textContent = apiReady ? 'configurada' : 'não configurada';
    $('#settingApiKey').className = `kv-val ${apiReady ? 'ok' : 'warn'}`;
    $('#settingModel').textContent = settings.translation_model || '—';
    $('#settingBatch').textContent = settings.translation_batch_size || '—';
    $('#settingRate').textContent = settings.max_requests_per_minute || '—';
    $('#settingTranslateSfx').checked = trueValue(settings.translate_sfx);
    $('#settingPrioritize').checked = trueValue(settings.prioritize_enclosed_text);
    $('#settingPaddle').textContent = settings.paddle_available ? `disponível · ${settings.paddleocr_version}` : 'não disponível';
    $('#settingRapid').textContent = settings.rapidocr_available ? `disponível · ${settings.rapidocr_version}` : 'não disponível';
    $('#confRange').value = Number(settings.rapidocr_min_confidence || .55);
    $('#confVal').textContent = Number(settings.rapidocr_min_confidence || .55).toFixed(2);
    $('#settingParallel').textContent = settings.ocr_parallel || '—';
    $('#settingRepair').textContent = settings.ocr_text_repair_mode || '—';
    $('#settingPython').textContent = settings.python_version || '—';
    $('#settingNicegui').textContent = settings.nicegui_version || '—';
    $('#settingBuild').textContent = 'local';
  }

  /* ---------- local profile ---------- */
  const statusLabels = {online: 'online', away: 'ausente', busy: 'ocupado', offline: 'offline'};
  function applyProfileToForm(profile) {
    $('#profileName').value = profile.display_name || '';
    $('#profileTitle').value = profile.title || '';
    $('#profilePronouns').value = profile.pronouns || '';
    $('#profileStatusText').value = profile.status_text || '';
    $('#profileBio').value = profile.bio || '';
    $('#profileBioCount').textContent = `${($('#profileBio').value || '').length}/190`;
    $$('#profileStatusChips .chip').forEach(chip => chip.classList.toggle('selected', chip.dataset.status === (profile.status || 'online')));
    $$('#profileAvatarMode .chip').forEach(chip => chip.classList.toggle('selected', chip.dataset.mode === (profile.avatar_mode || 'letter')));
    $$('#profileColorRow .color-swatch').forEach(swatch => swatch.classList.toggle('selected', swatch.dataset.color === (profile.avatar_color || '#c5372c')));
    $$('#profileBannerRow .banner-swatch').forEach(swatch => swatch.classList.toggle('selected', swatch.dataset.banner === (profile.banner || 'ink')));
    $('#avatarImagePicker')?.classList.toggle('show', (profile.avatar_mode || 'letter') === 'image');
    renderMediaCard('avatar', profile);
    renderMediaCard('banner', profile);
    renderProfile(profile);
  }
  function profileFromForm() {
    return {
      ...appState.profile,
      display_name: $('#profileName').value.trim() || 'você',
      title: $('#profileTitle').value,
      pronouns: $('#profilePronouns').value.trim(),
      status: $('#profileStatusChips .chip.selected')?.dataset.status || 'online',
      status_text: $('#profileStatusText').value.trim(),
      bio: $('#profileBio').value.trim(),
      avatar_mode: $('#profileAvatarMode .chip.selected')?.dataset.mode || 'letter',
      avatar_color: $('#profileColorRow .color-swatch.selected')?.dataset.color || '#c5372c',
      banner: $('#profileBannerRow .banner-swatch.selected')?.dataset.banner || 'ink',
    };
  }
  function setMedia(element, source, mediaType, fallback) {
    element.innerHTML = '';
    element.style.backgroundImage = '';
    if (source && String(mediaType).startsWith('image/')) {
      const image = document.createElement('img');
      image.src = source; image.alt = '';
      element.appendChild(image);
    } else if (source && String(mediaType).startsWith('video/')) {
      const video = document.createElement('video');
      video.src = source; video.autoplay = true; video.loop = true; video.muted = true; video.playsInline = true;
      element.appendChild(video);
    } else element.textContent = fallback;
  }
  function renderProfile(profile = profileFromForm()) {
    const name = profile.display_name || 'você';
    const avatar = name.slice(0, 1).toUpperCase();
    const avatarData = profile.avatar_mode === 'image' ? profile.avatar_media_url : '';
    [$('#pcAvatar'), $('#rpAvatar')].forEach(element => {
      element.style.backgroundColor = profile.avatar_color || '#c5372c';
      setMedia(element, avatarData, profile.avatar_media_type, avatar);
    });
    const banner = $('#pcBanner');
    banner.className = `pc-banner banner-${profile.banner || 'ink'}`;
    if (profile.banner === 'custom') setMedia(banner, profile.banner_media_url, profile.banner_media_type, '');
    else { banner.innerHTML = ''; banner.style.backgroundImage = ''; }
    $('#pcName').textContent = name;
    $('#rpName').textContent = name;
    $('#pcTitleRole').textContent = profile.title || '';
    $('#pcPronouns').textContent = profile.pronouns || '';
    $('#pcStatusLine').textContent = [statusLabels[profile.status] || 'online', profile.status_text].filter(Boolean).join(' · ');
    $('#pcBio').textContent = profile.bio || 'Seu perfil local ainda não tem bio.';
    $('#pcBadges').innerHTML = '';
    const since = profile.created_at ? new Date(profile.created_at).toLocaleDateString('pt-BR') : 'hoje';
    $('#pcSince').textContent = `perfil local desde ${since}`;
    [$('#pcStatusDot'), $('#rpStatusDot')].forEach(dot => { dot.className = `${dot.id === 'pcStatusDot' ? 'pc-status-dot' : 'rp-dot'} ${profile.status || 'online'}`; });
    $('#rpStatusLabel').textContent = statusLabels[profile.status] || 'online';
  }
  function bindChoiceGroup(selector, dataKey) {
    $(selector)?.addEventListener('click', event => {
      const item = event.target.closest(`[data-${dataKey}]`);
      if (!item) return;
      $$(`[data-${dataKey}]`, $(selector)).forEach(candidate => candidate.classList.remove('selected'));
      item.classList.add('selected');
      if (selector === '#profileAvatarMode') $('#avatarImagePicker')?.classList.toggle('show', item.dataset.mode === 'image');
      renderProfile();
    });
  }
  bindChoiceGroup('#profileStatusChips', 'status');
  bindChoiceGroup('#profileAvatarMode', 'mode');
  bindChoiceGroup('#profileColorRow', 'color');
  bindChoiceGroup('#profileBannerRow', 'banner');
  ['profileName', 'profileTitle', 'profilePronouns', 'profileStatusText', 'profileBio'].forEach(id => $(`#${id}`)?.addEventListener('input', () => {
    $('#profileBioCount').textContent = `${($('#profileBio').value || '').length}/190`;
    renderProfile();
  }));
  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  function renderMediaCard(kind, profile) {
    const card = $(`#${kind}MediaCard`);
    const source = profile[`${kind}_media_url`];
    if (!card) return;
    card.hidden = !source;
    if (!source) return;
    $(`#${kind}MediaName`).textContent = profile[`${kind}_media_name`] || 'mídia local';
    $(`#${kind}MediaMeta`).textContent = `${formatBytes(profile[`${kind}_media_size`])} · ${profile[`${kind}_media_type`] || 'arquivo'}`;
    setMedia($(`#${kind}MediaPreview`), source, profile[`${kind}_media_type`], '');
  }
  async function uploadProfileMedia(kind, file) {
    if (!file) return;
    const allowed = new Set(['image/png','image/jpeg','image/webp','image/gif','video/mp4','video/webm']);
    if (!allowed.has(file.type) || file.size > 12 * 1024 * 1024) {
      showToast('Use PNG, JPG, WEBP, GIF, MP4 ou WEBM de até 12 MB.', 'error');
      return;
    }
    const response = await fetch(`/api/ui/profile/media/${kind}?filename=${encodeURIComponent(file.name)}&content_type=${encodeURIComponent(file.type)}`, {
      method: 'POST', headers: {'Content-Type': file.type}, body: file,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Não foi possível salvar a mídia.');
    appState.profile = payload.profile;
    applyProfileToForm(appState.profile);
    showToast('Mídia salva neste computador.', 'ok');
  }
  async function removeProfileMedia(kind) {
    const response = await fetch(`/api/ui/profile/media/${kind}`, {method:'DELETE'});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || 'Não foi possível remover a mídia.');
    appState.profile = payload.profile;
    applyProfileToForm(appState.profile);
    showToast('Mídia removida.', 'ok');
  }
  function bindDropzone(kind) {
    const dropzone = $(`#${kind}ImagePicker`);
    const input = $(`#${kind}ImageInput`);
    if (!dropzone || !input) return;
    input.addEventListener('change', async () => {
      try { await uploadProfileMedia(kind, input.files?.[0]); } catch (error) { showToast(error.message, 'error'); }
      input.value = '';
    });
    ['dragenter','dragover'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add('dragging'); }));
    ['dragleave','drop'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove('dragging'); }));
    dropzone.addEventListener('drop', async event => {
      try { await uploadProfileMedia(kind, event.dataTransfer?.files?.[0]); } catch (error) { showToast(error.message, 'error'); }
    });
    dropzone.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); } });
    $(`#${kind}MediaReplace`)?.addEventListener('click', () => input.click());
    $(`#${kind}MediaRemove`)?.addEventListener('click', async () => { try { await removeProfileMedia(kind); } catch (error) { showToast(error.message, 'error'); } });
  }
  bindDropzone('avatar');
  bindDropzone('banner');
  $('#bannerMediaTrigger')?.addEventListener('click', () => $('#bannerImageInput')?.click());
  $('#profileSave')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/ui/profile', {method: 'POST', body: JSON.stringify(profileFromForm())});
      appState.profile = result.profile;
      applyProfileToForm(appState.profile);
      showToast('Perfil salvo neste computador.', 'ok');
    } catch (error) { showToast(error.message, 'error'); }
  });

  /* ---------- modal, copy and tilt ---------- */
  $('#visitorModalClose')?.addEventListener('click', () => $('#visitorModalOverlay').classList.remove('open'));
  $('#visitorModalOverlay')?.addEventListener('click', event => { if (event.target.id === 'visitorModalOverlay') event.currentTarget.classList.remove('open'); });
  $('#copyRepo')?.addEventListener('click', async event => {
    try { await navigator.clipboard.writeText(event.currentTarget.dataset.copy || ''); showToast('Copiado.', 'ok'); }
    catch (_) { showToast('Não foi possível copiar.', 'error'); }
  });
  document.addEventListener('mousemove', event => {
    const card = event.target.closest('.dash-stat-card, .profile-card');
    if (!card || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const rect = card.getBoundingClientRect();
    const rotateY = ((event.clientX - rect.left - rect.width / 2) / (rect.width / 2)) * 5;
    const rotateX = -((event.clientY - rect.top - rect.height / 2) / (rect.height / 2)) * 5;
    card.style.transform = `perspective(700px) rotateX(${rotateX}deg) rotateY(${rotateY}deg)`;
  });
  document.addEventListener('mouseleave', () => $$('.dash-stat-card, .profile-card').forEach(card => { card.style.transform = ''; }));
  document.addEventListener('keydown', event => {
    const tag = document.activeElement?.tagName || '';
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
    const index = Number(event.key) - 1;
    const tabs = $$('.rail-tab');
    if (index >= 0 && index < tabs.length) activateTab(tabs[index].dataset.tab);
  });

  /* ---------- data lifecycle ---------- */
  async function refreshBootstrap() {
    try {
      const data = await api(`/api/ui/bootstrap?cursor=${appState.cursor}`);
      appState.bootstrap = data;
      appState.history = Array.isArray(data.history) ? data.history : [];
      appState.profile = data.profile || {};
      appState.settings = data.settings || {};
      appState.historyRevision = data.history_revision || appState.historyRevision;
      renderSettings(appState.settings);
      applyProfileToForm(appState.profile);
      renderHistory();
      renderDashboard();
      renderRuntime(data);
    } catch (error) {
      showToast(`Interface local: ${error.message}`, 'error');
    }
  }
  async function pollState() {
    if (appState.polling || document.hidden) return;
    appState.polling = true;
    try {
      const data = await api(`/api/ui/state?cursor=${appState.cursor}`);
      renderRuntime(data);
      appState.cursor = Math.max(appState.cursor, Number(data.log_cursor || 0));
    } catch (error) {
      $('#appStatus').textContent = 'sem conexão';
      $('#appStatus').dataset.state = 'error';
    } finally { appState.polling = false; }
  }
  refreshBootstrap();
  const pollingTimer = window.setInterval(pollState, 850);
  window.addEventListener('beforeunload', () => { window.clearInterval(pollingTimer); cancelAnimationFrame(animationFrame); });
})();
