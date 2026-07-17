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
    nameDirty: false,
    outputDirty: false,
    programmingFields: false,
    activeStage: 'prepare',
    polling: false,
    logs: [],
    visualLogClearedAt: 0,
    lastFinishedId: '',
    expandedFolders: new Set(),
    seriesQuery: '',
    seriesSort: 'recent',
  };
  const runStatusLabels = {ready: 'pronto', running: 'rodando', finished: 'finalizado', review_required: 'revisão necessária', legacy_unverified: 'legado não verificado', error: 'erro', cancelled: 'cancelado'};
  const terminalRunStatuses = new Set(['finished', 'review_required']);
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
    if (!response.ok) throw new Error(payload.detail || `Falha local (${response.status})`);
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
  const stageMessages = {
    prepare: 'aguardando início', download: 'baixando páginas', validation: 'detectando balões',
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
  $('#urlInput')?.addEventListener('input', event => {
    const value = event.target.value.trim();
    if (!/^https?:\/\//i.test(value)) return;
    const guess = guessFromUrl(value);
    if (!appState.nameDirty) programField($('#nameInput'), guess.title);
    if (!appState.outputDirty) programField($('#outputInput'), guess.slug);
    $('#urlError')?.classList.remove('show');
  });
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
    const url = $('#urlInput').value.trim();
    let message = '';
    if (!url) message = 'informe a URL do capítulo antes de iniciar';
    else if (!/^https?:\/\//i.test(url)) message = 'a URL precisa começar com http:// ou https://';
    try { if (!message) new URL(url); } catch (_) { message = 'essa URL não parece válida'; }
    const error = $('#urlError');
    if (message) {
      error.textContent = message;
      error.classList.add('show');
      shake($('#urlInput'));
      return false;
    }
    error.classList.remove('show');
    if (appState.selectedScope === 'custom' && Number($('#scopeCustomInput').value) <= 0) {
      shake($('#scopeCustomInput'));
      showToast('Informe uma quantidade positiva de páginas.', 'error');
      return false;
    }
    return true;
  }
  function formPayload() {
    const full = appState.selectedScope === 'full';
    const maxImages = full ? null : Number(appState.selectedScope === 'custom' ? $('#scopeCustomInput').value : appState.selectedScope);
    const guess = guessFromUrl($('#urlInput').value.trim());
    return {
      url: $('#urlInput').value.trim(),
      chapter_name: $('#nameInput').value.trim() || guess.title,
      slug: slugify($('#outputInput').value || guess.slug),
      mode: appState.selectedMode,
      full,
      max_images: maxImages,
      use_cache: $('#cacheToggle').checked,
      force: $('#forceToggle').checked,
      use_context: $('#ctxToggle').checked,
      open_output: $('#openToggle').checked,
    };
  }
  async function startTranslation() {
    if (!validateForm()) return;
    try {
      const result = await api('/api/ui/run', {method: 'POST', body: JSON.stringify(formPayload())});
      appState.lastFinishedId = '';
      setRunControls(true);
      showToast('Pipeline real iniciado.', 'ok');
      activateTab('nova');
      return result;
    } catch (error) {
      showToast(error.message, 'error');
    }
  }
  async function cancelTranslation(queue = false) {
    try {
      await api('/api/ui/cancel', {method: 'POST', body: JSON.stringify({queue})});
      showToast('Cancelamento solicitado.', 'warn');
    } catch (error) { showToast(error.message, 'error'); }
  }
  function setRunControls(running) {
    $('#startBtn').disabled = running;
    $('#startBtn').textContent = running ? 'Processando…' : 'Iniciar tradução';
    $('#cancelBtn').disabled = !running;
  }
  $('#startBtn')?.addEventListener('click', startTranslation);
  $('#cancelBtn')?.addEventListener('click', () => cancelTranslation(false));
  $('#urlInput')?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); startTranslation(); }
  });

  /* ---------- real pipeline presentation ---------- */
  const stageOrder = ['download', 'validation', 'ocr', 'translate', 'render', 'pdf'];
  const visualStageKey = key => ({classification: 'ocr', reports: 'pdf'}[key] || key);
  function renderRuntime(runtime) {
    appState.status = runtime.status || 'ready';
    appState.queue = runtime.queue || [];
    const running = appState.status === 'running';
    setRunControls(running);
    const status = $('#appStatus');
    status.textContent = runStatusLabels[appState.status] || appState.status;
    status.dataset.state = appState.status;
    renderProgress(runtime.progress || {});
    renderQueue();
    appendLogs(runtime.logs || []);
    if (runtime.latest) renderResult(runtime.latest);
    if (runtime.history_revision !== appState.historyRevision) refreshBootstrap();
    if (terminalRunStatuses.has(appState.status) && runtime.latest?.id && runtime.latest.id !== appState.lastFinishedId) {
      appState.lastFinishedId = runtime.latest.id;
      flashFrame();
      showToast(appState.status === 'review_required' ? 'PDF gerado, mas requer revisão de qualidade.' : 'PDF finalizado e registrado no histórico.', appState.status === 'review_required' ? 'warn' : 'ok');
    }
  }
  function renderProgress(progress) {
    const runtimeKey = progress.stage_key || 'prepare';
    const key = visualStageKey(runtimeKey);
    const activeIndex = stageOrder.indexOf(key);
    $$('.stage-item').forEach(item => {
      const index = stageOrder.indexOf(item.dataset.stage);
      const pct = $('.stage-pct', item);
      const fill = $('.stage-fill', item);
      item.classList.toggle('done', key === 'final' || (activeIndex >= 0 && index < activeIndex));
      item.classList.toggle('active', item.dataset.stage === key && appState.status === 'running');
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
      const count = progress.total ? `${progress.current}/${progress.total}` : 'contador indisponível';
      summary.innerHTML = `<strong>${escapeHtml(progress.stage || 'Preparando')}</strong><br>${escapeHtml(count)} · ${escapeHtml(progress.elapsed_label || '0.0s')}<br>${escapeHtml(progress.last_message || '')}`;
    }
  }
  function renderResult(record) {
    const summary = $('#runSummary');
    if (!record || record.status === 'running') return;
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
    const seconds = Math.max(0, Number(value || 0));
    if (seconds >= 60) return `${Math.floor(seconds / 60)}min ${String(Math.floor(seconds % 60)).padStart(2, '0')}s`;
    return `${seconds.toFixed(1)}s`;
  }
  function actionButton(label, action, path = '') {
    if (!path && action !== 'reprocess') return '';
    return `<button class="btn-ghost" data-action="${action}" data-path="${encodeURIComponent(path || '')}">${label}</button>`;
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
      <div class="hm-actions">${actionButton('Abrir PDF', 'open', record.pdf_path)}${actionButton('Pasta', 'open', record.output_folder)}${actionButton('Relatório', 'open', record.quality_report_path)}${actionButton('Compare', 'open', record.compare_sheet_path)}${actionButton('Contexto', 'open', record.session_context_path)}${actionButton('Reprocessar', 'reprocess')}${record.pdf_path ? actionButton('Publicar na comunidade', 'publish') : ''}</div>
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
    if (button.dataset.action === 'publish') { await publishToCommunity(record); return; }
    await openArtifact(decodeURIComponent(button.dataset.path || ''));
  });

  /* ---------- community publishing ---------- */
  async function publishToCommunity(record) {
    const guess = guessFromUrl(record.url || '');
    const description = (window.prompt('Descrição (opcional) — publique apenas conteúdo cuja publicação você tem direito de fazer:', '') || '').slice(0, 2000);
    if (description === null) return;
    const trustedJobId = /^[0-9a-f]{32}$/.test(String(record.job_id || '')) ? String(record.job_id) : '';
    const payload = {
      slug: record.slug || guess.slug,
      series_title: record.series_name || record.chapter_name || '',
      series_slug: record.series_slug || guess.slug,
      episode_number: record.episode_number || '',
      title: record.chapter_name || '',
      description,
      visibility: 'public',
    };
    // Discovered legacy ids (for example "discovered-series") are presentation ids,
    // never job identities.  Omitting source_job_id lets the server apply its explicit
    // admin-only legacy slug policy instead of turning every old record into a false 404.
    if (trustedJobId) payload.source_job_id = trustedJobId;
    try {
      await api('/api/community/publish', {method: 'POST', body: JSON.stringify(payload)});
      showToast('Publicação enviada à fila. O worker fará o upload.', 'ok');
      await loadCommunityFeed();
    } catch (error) { showToast(error.message, 'error'); }
  }

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
    appState.programmingFields = true;
    $('#urlInput').value = record.url || '';
    $('#nameInput').value = record.chapter_name || '';
    $('#outputInput').value = record.slug || '';
    appState.programmingFields = false;
    appState.nameDirty = true;
    appState.outputDirty = true;
    appState.selectedMode = record.mode === 'quality' ? 'quality' : 'fast';
    $$('.choice-card').forEach(card => card.classList.toggle('selected', card.dataset.mode === appState.selectedMode));
    const scope = record.max_images ? String(record.max_images) : 'full';
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
