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
  const safeGlobals = Object.create(null);
  function getGlobal(name, fallback = undefined) {
    try {
      if (Object.prototype.hasOwnProperty.call(window, name)) return window[name];
    } catch (_) { /* fall back to local namespace */ }
    return Object.prototype.hasOwnProperty.call(safeGlobals, name) ? safeGlobals[name] : fallback;
  }
  function setGlobal(name, value) {
    try {
      window[name] = value;
      if (window[name] === value) return value;
    } catch (_) { /* non-extensible window; use local namespace */ }
    safeGlobals[name] = value;
    return value;
  }
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
    qualityReviewSelection: new Set(),
    qualityReviewUndo: [],
    qualityReviewBulkBusy: false,
    qualityRevisionPoll: null,
    currentPipelineState: null,
    cancelBusy: false,
    expandedFolders: new Set(),
    seriesQuery: '',
    seriesSort: 'recent',
    publicationRecord: null,
    publicationBusy: false,
    publicationCorrelation: '',
    claimRecord: null,
    claimBusy: false,
    publicationDrafts: Object.create(null),
    pendingHumanPreviews: {items: [], item_count: 0, ready_count: 0, blocked_count: 0},
    communityObjectUrls: new Set(),
    communityTab: 'explore',
    communityCache: {explore: [], favorites: [], reading: [], mine: [], notifications: []},
    localDeleteRecord: null,
    localDeleteBusy: false,
    profileMedia: {
      avatar: {requestId: '', controller: null, objectUrl: ''},
      banner: {requestId: '', controller: null, objectUrl: ''},
    },
    currentRequestId: '',
    currentJobId: '',
    currentRunId: '',
    currentSourceUrl: '',
    currentChapterName: '',
    currentStageVersion: 0,
    newTranslationDraft: false,
    terminalStatusByIdentity: new Map(),
  };
  const runStatusLabels = {ready: 'pronto', staging: 'analisando fonte', queued: 'na fila', running: 'rodando', awaiting_source_review: 'revisão das páginas', source_analysis_ready: 'fonte analisada', finished: 'finalizado', review_required: 'revisão necessária', review_completed: 'revisão concluída', failed: 'erro', legacy_unverified: 'legado não verificado', error: 'erro', cancelled: 'cancelado'};
  const terminalRunStatuses = new Set(['finished', 'review_required', 'review_completed', 'failed', 'cancelled']);
  const inFlightStatuses = new Set(['staging', 'queued', 'claiming', 'starting', 'running', 'cancelling', 'awaiting_source_review']);
  const MAX_VISIBLE_TOASTS = 3;
  const TOAST_DISMISS_MS = 3200;
  const TOAST_DEDUP_LIMIT = 80;
  const TERMINAL_NOTIFICATION_STORAGE_KEY = 'tradutor.terminalNotifications.v1';
  const toastRegistry = new Map();
  const consumedTerminalNotifications = new Set();
  try {
    const stored = JSON.parse(sessionStorage.getItem(TERMINAL_NOTIFICATION_STORAGE_KEY) || '[]');
    if (Array.isArray(stored)) stored.slice(-TOAST_DEDUP_LIMIT).forEach(key => {
      if (typeof key === 'string' && key) consumedTerminalNotifications.add(key);
    });
  } catch (_) { /* session storage is advisory only */ }
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
  setGlobal('__tradutorUiTrace', Array.isArray(getGlobal('__tradutorUiTrace')) ? getGlobal('__tradutorUiTrace') : []);
  function uiTrace(event, fields = {}) {
    const safe = {event: String(event || ''), at: Date.now()};
    for (const key of [
      'code', 'status', 'authenticated', 'valid', 'correlation_id', 'endpoint',
      'method', 'auth_transport', 'token_available', 'authorization_header_present',
      'authorization_scheme', 'reason_code', 'kind', 'request_id', 'publication_id',
      'job_id', 'run_id', 'stage',
    ]) {
      if (fields[key] !== undefined) safe[key] = fields[key];
    }
    const trace = getGlobal('__tradutorUiTrace', []);
    trace.push(safe);
    if (trace.length > 80) trace.shift();
    setGlobal('__tradutorUiTrace', trace);
  }
  function correlationId() {
    try { return crypto.randomUUID(); } catch (_) { return `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
  }
  function bootstrapCommunityAuthenticated(data = appState.bootstrap) {
    return data?.community?.authenticated === true;
  }
  function currentCanonicalAuthState() {
    if (String(getGlobal('__tradutorAuthState') || '') === 'authenticated') return 'authenticated';
    if (bootstrapCommunityAuthenticated()) return 'authenticated';
    return String(getGlobal('__tradutorAuthState') || 'auth_loading');
  }
  function isCanonicalCommunityAuthenticated() {
    if (bootstrapCommunityAuthenticated()) return true;
    return String(getGlobal('__tradutorAuthState') || '') === 'authenticated' && Boolean(
      getGlobal('__tradutorCommunityAuthenticated') ||
      getGlobal('__tradutorAccessToken') ||
      getGlobal('__tradutorAuthStore')?.authenticated,
    );
  }
  function syncCanonicalAuthFromBootstrap(data) {
    const community = data?.community || {};
    const backendAuthenticated = community.authenticated === true;
    const backendUnauthenticated = community.authenticated === false;
    if (backendAuthenticated) {
      const userId = String(community.user_id || data?.profile?.user_id || getGlobal('__tradutorCommunityUserId') || '');
      setGlobal('__tradutorAuthState', 'authenticated');
      setGlobal('__tradutorCommunityAuthenticated', true);
      if (userId) setGlobal('__tradutorCommunityUserId', userId);
      setGlobal('__tradutorAuthStore', {status: 'authenticated', authenticated: true, user_id: userId});
      if (document.body) document.body.dataset.authState = 'authenticated';
      document.documentElement.dataset.shellState = 'authenticated';
      uiTrace('canonical_auth_bootstrap_applied', {authenticated: true});
      return 'authenticated';
    }
    const rawAuthState = String(getGlobal('__tradutorAuthState') || '');
    if (backendUnauthenticated && !getGlobal('__tradutorAccessToken') && rawAuthState !== 'auth_loading') {
      setGlobal('__tradutorAuthState', 'unauthenticated');
      setGlobal('__tradutorCommunityAuthenticated', false);
      setGlobal('__tradutorCommunityUserId', '');
      setGlobal('__tradutorAuthStore', {status: 'unauthenticated', authenticated: false, user_id: ''});
      if (document.body) document.body.dataset.authState = 'unauthenticated';
      document.documentElement.dataset.shellState = 'unauthenticated';
      uiTrace('canonical_auth_bootstrap_applied', {authenticated: false});
      return 'unauthenticated';
    }
    return currentCanonicalAuthState();
  }

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
    let bearer = getGlobal('__tradutorAccessToken') || '';
    const canonicalAccessToken = getGlobal('__tradutorGetCanonicalAccessToken');
    if (!bearer && typeof canonicalAccessToken === 'function') {
      try { bearer = await canonicalAccessToken(); } catch (_) { bearer = ''; }
      if (bearer) setGlobal('__tradutorAccessToken', bearer);
    }
    if (bearer) headers['Authorization'] = `Bearer ${bearer}`;
    const authTransport = String(getGlobal('__tradutorAuthTransport') || '');
    uiTrace('community_request_started', {
      endpoint: String(path || '').split('?')[0], method,
      authenticated: getGlobal('__tradutorAuthState') === 'authenticated',
      auth_transport: authTransport, token_available: Boolean(bearer),
      authorization_header_present: Boolean(headers.Authorization),
      authorization_scheme: headers.Authorization ? 'Bearer' : '',
    });
    const timeoutMs = Number(options.timeoutMs || 15000);
    const controller = options.signal ? null : new AbortController();
    const timer = controller ? window.setTimeout(() => controller.abort(), timeoutMs) : 0;
    const init = {...options, method, headers, credentials: 'same-origin', cache: 'no-store'};
    delete init.timeoutMs;
    delete init.rawResponse;
    if (controller) init.signal = controller.signal;
    let response;
    try {
      response = await fetch(path, init);
    } catch (cause) {
      if (cause?.name === 'AbortError') {
        const error = new Error('O serviço demorou para responder. Verificando o estado…');
        error.code = 'timeout'; error.status = 408; throw error;
      }
      const error = new Error('Não foi possível conectar ao serviço local.');
      error.code = 'connection_error'; throw error;
    } finally {
      if (timer) window.clearTimeout(timer);
    }
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch (_) { /* empty response */ }
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
      uiTrace('community_request_failed', {
        endpoint: String(path || '').split('?')[0], method, status: response.status,
        reason_code: error.code || (typeof detail === 'string' ? detail : ''),
        authenticated: getGlobal('__tradutorAuthState') === 'authenticated',
        auth_transport: authTransport, token_available: Boolean(bearer),
        authorization_header_present: Boolean(headers.Authorization),
        authorization_scheme: headers.Authorization ? 'Bearer' : '',
      });
      throw error;
    }
    uiTrace('community_request_response', {
      endpoint: String(path || '').split('?')[0], method, status: response.status,
      authenticated: getGlobal('__tradutorAuthState') === 'authenticated',
      auth_transport: authTransport, token_available: Boolean(bearer),
      authorization_header_present: Boolean(headers.Authorization),
      authorization_scheme: headers.Authorization ? 'Bearer' : '',
    });
    if (options.rawResponse === true) return response;
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    return payload;
  }

  /* ---------- boot ---------- */
  const bootEl = $('#boot');
  const bootVisualTest = (() => {
    const params = new URLSearchParams(window.location.search || '');
    const raw = String(params.get('visual_boot_stage') || '').trim().toLowerCase();
    if (!raw) return null;
    const local = ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);
    const enabled = getGlobal('__tradutorVisualTestEnabled') === true;
    if (!local || !enabled) return null;
    const stageMap = {init: 1, local: 2, auth: 3, session: 4, profile: 5, settings: 6, community: 7, ready: 8};
    if (raw === 'error' || raw === 'disk_full' || raw === 'retry') {
      return {kind: 'error', code: raw === 'error' ? String(params.get('visual_boot_error') || 'boot_error') : raw, reducedMotion: params.get('visual_reduced_motion') === '1'};
    }
    const stage = Number(raw) || stageMap[raw] || 0;
    if (stage < 1 || stage > 8) return null;
    return {kind: 'stage', index: stage - 1, reducedMotion: params.get('visual_reduced_motion') === '1'};
  })();
  let bootHighestStage = 0;
  const bootStages = [
    'loading.stage.init', 'loading.stage.local', 'loading.stage.auth', 'loading.stage.session',
    'loading.stage.profile', 'loading.stage.settings', 'loading.stage.community', 'loading.stage.ready',
  ];
  const bootStageMeta = [
    {sub: 'preparando painel local', icon: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.4"/><path d="M21 15l-5-5-4 4-3-3-6 6"/>'},
    {sub: 'conectando ao servidor local', icon: '<path d="M5 12h14"/><path d="M12 5l7 7-7 7"/><path d="M5 5v14"/>'},
    {sub: 'preparando provider de autenticação', icon: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'},
    {sub: 'validando cookie e sessão canônica', icon: '<path d="M20 6 9 17l-5-5"/>'},
    {sub: 'carregando identidade local', icon: '<path d="M20 21a8 8 0 0 0-16 0"/><circle cx="12" cy="8" r="5"/>'},
    {sub: 'aplicando preferências do painel', icon: '<path d="M12 15.5A3.5 3.5 0 1 0 12 8a3.5 3.5 0 0 0 0 7.5z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.37a1.7 1.7 0 0 0-1 .57V20a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1-.57 1.7 1.7 0 0 0-1.88.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.63 15a1.7 1.7 0 0 0-.57-1H4a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 .57-1 1.7 1.7 0 0 0-.34-1.88l-.06-.06A2 2 0 1 1 7.09 4.2l.06.06A1.7 1.7 0 0 0 9 4.63h.09A1.7 1.7 0 0 0 10 4.06V4a2 2 0 1 1 4 0v.09c.35.13.68.32 1 .57a1.7 1.7 0 0 0 1.88-.34l.06-.06A2 2 0 1 1 19.77 7.1l-.06.06A1.7 1.7 0 0 0 19.37 9c.25.32.44.65.57 1H20a2 2 0 1 1 0 4h-.09c-.13.35-.32.68-.57 1z"/>'},
    {sub: 'sincronizando comunidade e histórico', icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>'},
    {sub: 'interface pronta', icon: '<path d="M20 6 9 17l-5-5"/>'},
  ];
  const bootTips = [
    'dica: termos glossados ficam marcados em vermelho no PDF final.',
    'o pipeline nunca aprova uma etapa crítica em silêncio.',
    'webtoons de rolagem longa são fatiados automaticamente por painel.',
    'toda tradução revisada é salva em cache para reaproveitar depois.',
  ];
  document.documentElement.dataset.shellState = document.documentElement.dataset.shellState || 'booting';
  const bootNodes = $('#bootNodes');
  const bootLabels = $('#bootNodeLabels');
  if (bootNodes && !bootNodes.children.length) {
    bootStageMeta.forEach((stage, index) => {
      const node = document.createElement('div');
      node.className = 'app-loading-node';
      node.dataset.bootNode = String(index);
      node.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${stage.icon}</svg>`;
      bootNodes.appendChild(node);
      if (index < bootStageMeta.length - 1) {
        const connector = document.createElement('div');
        connector.className = 'app-loading-connector';
        connector.innerHTML = '<div class="app-loading-connector-fill"></div><div class="app-loading-connector-spark"></div>';
        bootNodes.appendChild(connector);
      }
      const label = document.createElement('span');
      label.textContent = String(index + 1);
      bootLabels?.appendChild(label);
    });
  }
  const bootParticles = $('#bootParticles');
  if (bootParticles && !bootParticles.children.length) {
    for (let index = 0; index < 14; index += 1) {
      const particle = document.createElement('div');
      particle.className = 'app-loading-particle';
      const size = 2 + Math.random() * 3;
      particle.style.width = `${size}px`;
      particle.style.height = `${size}px`;
      particle.style.left = `${Math.random() * 100}%`;
      particle.style.setProperty('--drift', `${Math.random() * 60 - 30}px`);
      particle.style.animationDuration = `${9 + Math.random() * 10}s`;
      particle.style.animationDelay = `${Math.random() * 12}s`;
      bootParticles.appendChild(particle);
    }
  }
  function setBootText(element, text) {
    if (!element) return;
    element.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = text;
    element.appendChild(span);
  }
  function setBootStage(index, message = '') {
    if (!bootEl) return;
    let bounded = Math.max(0, Math.min(bootStages.length - 1, Number(index) || 0));
    if (bootEl.dataset.bootState !== 'failed') {
      bounded = Math.max(bootHighestStage, bounded);
      bootHighestStage = bounded;
    }
    const label = message || window.TradutorI18n?.t(bootStages[bounded]) || bootStages[bounded];
    const pct = ((bounded + 1) / bootStages.length) * 100;
    const pctLabel = `${pct.toLocaleString('pt-BR', {maximumFractionDigits: 1})}%`;
    setBootText($('#bootStatusLine'), label);
    $('#bootStatusMini') && ($('#bootStatusMini').textContent = bootStageMeta[bounded]?.sub || `${bounded + 1}/${bootStages.length} etapas`);
    $('#ringPct') && ($('#ringPct').textContent = pctLabel);
    $('#ringFill') && ($('#ringFill').style.strokeDashoffset = String(175.9 - (pct / 100) * 175.9));
    $('#bootProgressBar') && ($('#bootProgressBar').style.width = `${pct}%`);
    $('#pageCount') && ($('#pageCount').textContent = `${bounded + 1}/${bootStages.length} etapas`);
    $('#bootFooterLabel') && ($('#bootFooterLabel').textContent = pct < 100 ? 'preparando interface' : 'pronto');
    setBootText($('#tickerText'), bootTips[bounded % bootTips.length]);
    $$('#bootNodes [data-boot-node]').forEach((node, nodeIndex) => {
      node.classList.toggle('done', nodeIndex < bounded);
      node.classList.toggle('active', nodeIndex === bounded);
      node.classList.toggle('failed', bootEl.dataset.bootState === 'failed' && nodeIndex === bounded);
    });
    $$('#bootNodes .app-loading-connector').forEach((connector, connectorIndex) => {
      connector.classList.toggle('done', connectorIndex < bounded);
    });
  }
  function setBootFailed(message) {
    if (!bootEl) return;
    bootEl.dataset.bootState = 'failed';
    document.documentElement.dataset.shellState = 'boot_failed';
    setBootStage(1, message || 'Não foi possível carregar a interface local.');
    $('#bootActions') && ($('#bootActions').hidden = false);
    $('#bootFooterLabel') && ($('#bootFooterLabel').textContent = 'ação necessária');
  }
  function closeBoot() {
    if (bootEl) bootEl.classList.add('hide');
  }
  setBootStage(0);
  if (bootVisualTest?.kind === 'stage') {
    bootEl.dataset.visualBootTest = '1';
    document.documentElement.dataset.visualBootTest = '1';
    if (bootVisualTest.reducedMotion) document.documentElement.dataset.visualReducedMotion = '1';
    document.documentElement.dataset.shellState = 'booting';
    $('#authSurface') && ($('#authSurface').hidden = true);
    setBootStage(bootVisualTest.index);
  } else if (bootVisualTest?.kind === 'error') {
    bootEl.dataset.visualBootTest = '1';
    document.documentElement.dataset.visualBootTest = '1';
    if (bootVisualTest.reducedMotion) document.documentElement.dataset.visualReducedMotion = '1';
    document.documentElement.dataset.shellState = 'boot_failed';
    $('#authSurface') && ($('#authSurface').hidden = true);
    setBootFailed(bootVisualTest.code === 'disk_full'
      ? 'Disco cheio ao preparar o painel local.'
      : 'Não foi possível carregar a interface local.');
  }
  const bootTimer = bootVisualTest ? 0 : window.setTimeout(() => setBootFailed('O carregamento demorou para responder.'), 15000);
  $('#bootRetry')?.addEventListener('click', () => window.location.reload());
  $('#bootDiagnostics')?.addEventListener('click', () => activateTab('logs'));

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
    $$('.panel, .hist-item', view).forEach((item, index) => {
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
    if (name === 'nova' && !inFlightStatuses.has(appState.status)) {
      appState.newTranslationDraft = true;
      clearNewTranslationDraftPanels();
    }
    if (name === 'hist') renderHistory();
    if (name === 'inicio') renderDashboard();
    if (name === 'community') loadCommunityFeed();
  }
  $$('.rail-tab').forEach(tab => tab.addEventListener('click', () => {
    // Choosing "Nova tradução" from the rail means starting a fresh chapter, so
    // review_mode must release the form instead of pinning the reviewed chapter.
    if (tab.dataset.tab === 'nova' && appState.reviewMode) exitReviewMode();
    activateTab(tab.dataset.tab);
  }));
  $$('[data-goto]').forEach(button => button.addEventListener('click', () => activateTab(button.dataset.goto)));
  $('#railProfile')?.addEventListener('click', () => activateTab('profile'));
  window.setTimeout(() => moveIndicator($('.rail-tab.active')), 60);

  /* ---------- visual feedback ---------- */
  const reasonMessages = {
    source_not_ready: 'Não foi possível analisar a fonte.',
    source_analysis_failed: 'A análise da fonte falhou antes de reconhecer sua estrutura.',
    download_authorization_required: 'É necessária uma autorização de conteúdo antes de iniciar o download.',
    source_transport_failed: 'Não foi possível conectar à fonte após tentativas controladas.',
    source_unavailable: 'A página da fonte não está disponível.',
    source_redirect_blocked: 'A fonte tentou redirecionar para um endereço não permitido.',
    source_content_type_unsupported: 'A fonte não respondeu com uma página web compatível.',
    browser_runtime_unavailable: 'Nenhum navegador compatível foi encontrado no computador.',
    browser_executable_not_found: 'O navegador configurado não foi encontrado.',
    browser_driver_unavailable: 'O navegador foi encontrado, mas seu driver não está disponível.',
    browser_driver_incompatible: 'A versão do driver não é compatível com o navegador instalado.',
    browser_launch_failed: 'O navegador foi encontrado, mas não conseguiu iniciar.',
    browser_startup_timeout: 'O navegador ultrapassou o tempo limite de inicialização.',
    browser_profile_locked: 'O perfil temporário do navegador não pôde ser aberto.',
    browser_process_exited: 'O navegador encerrou antes de concluir a análise.',
    source_navigation_timeout: 'A página ultrapassou o tempo limite de navegação.',
    chromedriver_unavailable: 'O navegador foi encontrado, mas seu driver não está disponível.',
    disk_full: 'Disco cheio ao gravar as páginas baixadas. Libere espaço e tente novamente.',
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
    idle: 'Pronto para iniciar',
    prepare: 'Pronto para iniciar',
    validating_source: 'Validando a fonte...',
    source_validation: 'Validando a fonte...',
    source_verified: 'Fonte encontrada',
    creating_job: 'Criando o processamento...',
    queued: 'Na fila...',
    starting_worker: 'Iniciando o worker...',
    worker_starting: 'Iniciando o worker...',
    starting: 'Iniciando o worker...',
    source_analysis: 'Analisando o capítulo...',
    source_analysis_ready: 'Fonte analisada',
    browser_loading: 'Analisando o capítulo...',
    collecting_candidates: 'Analisando o capítulo...',
    clustering_candidates: 'Analisando o capítulo...',
    source_lazy_resolution: 'carregando páginas do leitor',
    source_selection: 'preparando a ordem das páginas',
    awaiting_source_review: 'Preparando as páginas...',
    reviewing_pages: 'Preparando as páginas...',
    downloading: 'Baixando as páginas...',
    downloading_pages: 'Baixando as páginas...',
    validating_pages: 'Baixando as páginas...',
    download: 'Baixando as páginas...',
    detecting_balloons: 'Detectando os balões...',
    validation: 'Detectando os balões...',
    reading_text: 'Lendo o texto...',
    ocr: 'Lendo o texto...',
    classification: 'Lendo o texto...',
    translating: 'Traduzindo o capítulo...',
    translate: 'Traduzindo o capítulo...',
    redrawing: 'Reconstruindo a arte...',
    render: 'Reconstruindo a arte...',
    generating_pdf: 'Gerando o PDF...',
    pdf: 'Gerando o PDF...',
    reports: 'Gerando o PDF...',
    quality_review: 'Revisando a tradução...',
    quality_gate: 'Revisando a tradução...',
    review_required: 'Revisão necessária',
    finished: 'Capítulo concluído',
    review_completed: 'Capítulo concluído',
    failed: 'Não foi possível concluir',
    cancelled: 'Processamento cancelado',
    final: 'Capítulo concluído',
  };
  const sfxWords = {download: '…', downloading: '…', downloading_pages: '…', validation: '!!', detecting_balloons: '!!', ocr: 'スキャン', reading_text: 'スキャン', classification: 'CHK', translate: 'ZAP', translating: 'ZAP', render: 'SHK', redrawing: 'SHK', pdf: 'PDF', generating_pdf: 'PDF', quality_review: 'OK', reports: 'OK'};
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
  function rememberBounded(set, key, limit = TOAST_DEDUP_LIMIT) {
    if (!key) return;
    if (set.has(key)) set.delete(key);
    set.add(key);
    while (set.size > limit) set.delete(set.values().next().value);
  }
  function persistConsumedTerminalNotifications() {
    try {
      sessionStorage.setItem(
        TERMINAL_NOTIFICATION_STORAGE_KEY,
        JSON.stringify(Array.from(consumedTerminalNotifications).slice(-TOAST_DEDUP_LIMIT)),
      );
    } catch (_) { /* best effort only */ }
  }
  function removeToastByKey(key) {
    const existing = toastRegistry.get(key);
    if (!existing) return;
    window.clearTimeout(existing.timeout);
    window.clearTimeout(existing.removeTimeout);
    existing.node.remove();
    toastRegistry.delete(key);
  }
  function scheduleToastRemoval(key, toast) {
    const timeout = window.setTimeout(() => {
      toast.classList.add('leaving');
      const removeTimeout = window.setTimeout(() => {
        toast.remove();
        toastRegistry.delete(key);
      }, 280);
      const entry = toastRegistry.get(key);
      if (entry) entry.removeTimeout = removeTimeout;
    }, TOAST_DISMISS_MS);
    return timeout;
  }
  showToast = function deduplicatedToast(message, type = 'ok', options = {}) {
    if (type && typeof type === 'object') {
      options = type;
      type = options.type || 'ok';
    }
    const stack = $('#toastStack');
    if (!stack) return null;
    const key = String(options.key || `${type}:${message}`);
    const icon = type === 'error' ? '!' : type === 'warn' ? '△' : '✓';
    const existing = toastRegistry.get(key);
    if (existing?.node?.isConnected) {
      existing.node.className = `toast ${type}`;
      existing.node.innerHTML = `<span class="t-icon">${icon}</span><span>${escapeHtml(message)}</span>`;
      window.clearTimeout(existing.timeout);
      window.clearTimeout(existing.removeTimeout);
      existing.timeout = scheduleToastRemoval(key, existing.node);
      uiTrace('TERMINAL_NOTIFICATION_DEDUPED', {kind: options.kind || 'toast'});
      return existing.node;
    }
    while (toastRegistry.size >= MAX_VISIBLE_TOASTS) removeToastByKey(toastRegistry.keys().next().value);
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.dataset.toastKey = key;
    toast.innerHTML = `<span class="t-icon">${icon}</span><span>${escapeHtml(message)}</span>`;
    stack.appendChild(toast);
    toastRegistry.set(key, {node: toast, timeout: scheduleToastRemoval(key, toast), removeTimeout: 0});
    return toast;
  };
  // Bridge so ES modules (e.g. the social community UI) reuse the same toast component.
  setGlobal('__tradutorToast', (message, kind) => showToast(message, kind === 'err' ? 'error' : kind || 'ok'));
  function humanCommunityError(errorValue, fallback = 'Não foi possível concluir a ação.') {
    const code = String(errorValue?.code || errorValue?.message || '').trim();
    const status = Number(errorValue?.status || 0);
    if (status === 401 || code === 'authentication_required') return 'Sua sessão expirou. Entre novamente.';
    if (status === 403 || code === 'csrf_rejected' || code === 'forbidden') return 'Você não tem permissão para esta ação.';
    if (status === 404) return 'Este conteúdo não está disponível.';
    if (code === 'timeout') return 'O serviço demorou para responder. Tente novamente.';
    if (code === 'connection_error') return 'Não foi possível conectar ao serviço local.';
    return fallback;
  }
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
      mode: appState.selectedMode === 'download_only' ? 'fast' : appState.selectedMode,
      download_only: appState.selectedMode === 'download_only',
      full,
      max_images: maxImages,
      use_cache: $('#cacheToggle').checked,
      force: $('#forceToggle').checked,
      use_context: $('#ctxToggle').checked,
      open_output: $('#openToggle').checked,
      create_source_profile: !local && $('#sourceProfileToggle').checked,
      pipeline_intent: {
        requested: true,
        mode: appState.selectedMode === 'download_only' ? 'download_only' : appState.selectedMode,
        scope: full ? 'full' : String(maxImages),
      },
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
    appState.sourceReady = null;
    $('#sourceReviewPanel') && ($('#sourceReviewPanel').hidden = true);
    $('#sourceReadyPanel') && ($('#sourceReadyPanel').hidden = true);
  }
  function clearNewTranslationDraftPanels() {
    resetRunPreview();
    appState.qualityReview = null;
    appState.qualityReviewSelection = new Set();
    $('#qualityReviewPanel') && ($('#qualityReviewPanel').hidden = true);
    const reviewedPdf = $('#reviewedPdfAction');
    if (reviewedPdf) { reviewedPdf.hidden = true; reviewedPdf.innerHTML = ''; }
    $('#runStatusCard') && ($('#runStatusCard').hidden = true);
    $('#artifactActions') && ($('#artifactActions').innerHTML = '');
    $('#balloonText') && ($('#balloonText').textContent = window.TradutorI18n?.t('pipeline.idle') || 'Pronto para iniciar');
    $$('.stage-item').forEach(item => {
      item.classList.remove('active', 'done', 'indeterminate');
      const pct = $('.stage-pct', item);
      const fill = $('.stage-fill', item);
      if (pct) pct.textContent = '—';
      if (fill) fill.style.width = '0%';
    });
  }

  function resetActivePipelineIdentity(sourceUrl = '') {
    // A new operation owns every pipeline/result panel immediately. Clear terminal
    // artifacts before adopting its identity so a polling tick cannot leave the last
    // chapter's failure or quality review under the new active stage.
    clearNewTranslationDraftPanels();
    appState.currentRequestId = correlationId();
    appState.currentJobId = '';
    appState.currentRunId = '';
    appState.currentSourceUrl = String(sourceUrl || '');
    appState.currentChapterName = $('#nameInput')?.value?.trim() || '';
    appState.currentStageVersion += 1;
    appState.newTranslationDraft = false;
    $('#balloonText') && ($('#balloonText').textContent = window.TradutorI18n?.t('pipeline.validating_source') || 'Validando fonte');
    persistPipelineIdentity('validating_source');
    uiTrace('pipeline_request_created', {request_id: appState.currentRequestId});
  }

  function persistPipelineIdentity(stage = '') {
    setGlobal('__tradutorCurrentRequestId', appState.currentRequestId);
    setGlobal('__tradutorCurrentJobId', appState.currentJobId);
    setGlobal('__tradutorCurrentRunId', appState.currentRunId);
    setGlobal('__tradutorCurrentStage', stage || appState.activeStage || '');
    try {
      sessionStorage.setItem('tradutor:activePipeline', JSON.stringify({
        request_id: appState.currentRequestId,
        job_id: appState.currentJobId,
        run_id: appState.currentRunId,
        source_url: appState.currentSourceUrl,
        chapter_name: appState.currentChapterName,
        stage: stage || appState.activeStage || '',
        updated_at: Date.now(),
      }));
    } catch (_) { /* session storage is a convenience only */ }
  }

  function pipelineRecord(status = 'staging', stage = 'source_validation', extra = {}) {
    return {
      id: appState.currentJobId,
      job_id: appState.currentJobId,
      run_id: appState.currentRunId,
      status,
      stage,
      reason_code: extra.reason_code || '',
      chapter_name: appState.currentChapterName || $('#nameInput')?.value?.trim() || 'Capítulo atual',
      slug: $('#outputInput')?.value || '',
      url: appState.currentSourceUrl,
      error_message: extra.message || '',
    };
  }

  function renderLocalPipelineState(stage, {status = 'staging', message = '', reason_code = ''} = {}) {
    appState.status = status;
    appState.activeStage = '';
    persistPipelineIdentity(stage);
    const record = pipelineRecord(status, stage, {reason_code, message});
    const runtime = {
      status,
      active: terminalRunStatuses.has(status) ? null : record,
      latest: terminalRunStatuses.has(status) ? record : null,
      progress: {
        stage_key: stage,
        stage: stageMessages[stage] || stage,
        current: 0,
        total: 0,
        fraction: null,
        indeterminate: !terminalRunStatuses.has(status),
        last_message: message,
        updated_at: Date.now() / 1000,
      },
    };
    renderProgress(runtime.progress);
    renderRunStatus(runtime);
    const identity = terminalIdentity(record);
    if (identity) appState.terminalStatusByIdentity.set(identity, String(status || '').toLowerCase());
  }

  function showStartError(error) {
    const box = $('#startError');
    renderLocalPipelineState('source_analysis', {
      status: 'failed',
      reason_code: error.code || '',
      message: error.message || '',
    });
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
    const payload = formPayload();
    resetActivePipelineIdentity(payload.url || payload.local_folder || '');
    renderLocalPipelineState('validating_source', {
      status: 'staging',
      message: 'Validando fonte',
    });

    try {
      const result = await api('/api/ui/run', {method: 'POST', body: JSON.stringify(payload)});
      if (!result || result.ok === false) {
        const error = new Error((result && (result.message || result.reason_code)) || 'Não foi possível analisar esta fonte com segurança.');
        error.code = result && result.reason_code;
        error.stage = (result && result.stage) || 'análise da fonte';
        error.action = (result && result.action) || 'Revise a URL e tente novamente.';
        throw error;
      }
      appState.lastFinishedId = '';
      resetRunPreview();                            // never carry the previous job's card over
      appState.currentJobId = String(result.job_id || '');
      appState.currentRunId = String(result.run_id || '');
      persistPipelineIdentity(result.stage || 'queued');
      uiTrace('pipeline_job_created', {
        request_id: appState.currentRequestId,
        job_id: appState.currentJobId,
        run_id: appState.currentRunId,
        stage: result.stage || 'queued',
      });
      const awaitingReview = Boolean(result.awaiting_source_review);
      renderLocalPipelineState(awaitingReview ? 'awaiting_source_review' : (result.stage || 'queued'), {
        status: awaitingReview ? 'awaiting_source_review' : (result.status || 'queued'),
        message: awaitingReview ? 'Aguardando revisão das páginas' : 'Na fila',
      });
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
      const endpoint = jobId ? `/api/ui/jobs/${encodeURIComponent(jobId)}/cancel` : '/api/ui/cancel';
      const result = await api(endpoint, {method: 'POST', body: JSON.stringify(payload), timeoutMs: 10000});
      showToast(result?.message || 'Cancelamento solicitado. Os arquivos parciais serao preservados.', 'warn');
      pollState();
    } catch (error) {
      showToast(error.message || 'Nao foi possivel cancelar.', 'error');
      if (button) { button.disabled = false; button.textContent = 'Cancelar processamento'; }
    } finally {
      appState.cancelBusy = false;
      if (button && !button.hidden) { button.disabled = false; button.textContent = 'Cancelar processamento'; }
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
  $('#urlInput')?.addEventListener('input', () => {
    if (!appState.currentSourceUrl || $('#urlInput').value.trim() !== appState.currentSourceUrl) {
      appState.newTranslationDraft = true;
      clearNewTranslationDraftPanels();
    }
  });
  $('#localFolderInput')?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); startTranslation(); }
  });

  /* ---------- real pipeline presentation ---------- */
  const stageOrder = ['source_analysis', 'awaiting_source_review', 'download', 'validation', 'ocr', 'translate', 'render', 'pdf', 'quality_review'];
  const stageAliases = {
    idle: 'idle',
    prepare: 'idle',
    creating_job: 'source_analysis',
    queued: 'source_analysis',
    starting: 'source_analysis',
    starting_worker: 'source_analysis',
    worker_starting: 'source_analysis',
    validating_source: 'source_analysis',
    source_validation: 'source_analysis',
    source_verified: 'source_analysis',
    source_analysis: 'source_analysis',
    source_analysis_ready: 'source_analysis',
    browser_loading: 'source_analysis',
    collecting_candidates: 'source_analysis',
    clustering_candidates: 'source_analysis',
    source_lazy_resolution: 'source_analysis',
    source_selection: 'awaiting_source_review',
    awaiting_source_review: 'awaiting_source_review',
    reviewing_pages: 'awaiting_source_review',
    downloading: 'download',
    downloading_pages: 'download',
    validating_pages: 'download',
    download: 'download',
    detecting_balloons: 'validation',
    validation: 'validation',
    reading_text: 'ocr',
    ocr: 'ocr',
    classification: 'ocr',
    translating: 'translate',
    translate: 'translate',
    redrawing: 'render',
    render: 'render',
    generating_pdf: 'pdf',
    pdf: 'pdf',
    reports: 'pdf',
    quality_review: 'quality_review',
    quality_gate: 'quality_review',
    review_required: 'quality_review',
    review_completed: 'quality_review',
    finished: 'quality_review',
    failed: 'failed',
    cancelled: 'cancelled',
    final: 'quality_review',
  };
  const visualStageKey = key => stageAliases[String(key || '').toLowerCase()] || String(key || 'idle').toLowerCase();
  function terminalIdentity(record) {
    const jobId = String(record?.id || record?.job_id || '');
    const runId = String(record?.run_id || '');
    return jobId || runId ? `${jobId}:${runId}` : '';
  }
  function terminalNotificationKey(record) {
    const identity = terminalIdentity(record);
    if (!identity) return '';
    const status = String(record?.status || '').toLowerCase();
    const stage = String(record?.stage || '');
    return `${identity}:${status}:${stage}`;
  }
  function terminalNotificationMessage(record) {
    const status = String(record?.status || '').toLowerCase();
    if (status === 'review_required') {
      return {message: 'PDF gerado, mas requer revisão de qualidade.', type: 'warn'};
    }
    if (['finished', 'review_completed'].includes(status) && (record?.pdf_path || record?.quality_report_path || record?.output_folder)) {
      return {message: 'PDF finalizado e registrado no histórico.', type: 'ok'};
    }
    return null;
  }
  function releaseStaleInterfaceBusy() {
    if (document.body) {
      document.body.removeAttribute('aria-busy');
      document.body.inert = false;
    }
    $$('.modal-overlay:not(.show)').forEach(overlay => {
      overlay.setAttribute('aria-hidden', 'true');
    });
    const startButton = $('#startBtn');
    if (startButton && !inFlightStatuses.has(appState.status)) {
      startButton.dataset.busy = '0';
      startButton.disabled = false;
    }
    const boot = $('#boot');
    if (boot && !document.documentElement.dataset.visualBootTest) boot.classList.add('hide');
  }
  function rememberRuntimeTerminalState(runtime) {
    const record = runtime.active || runtime.source_review || runtime.source_ready || runtime.latest || null;
    const identity = terminalIdentity(record);
    if (!identity) return;
    const status = String(record?.status || runtime.status || '').toLowerCase();
    appState.terminalStatusByIdentity.set(identity, status);
    if (terminalRunStatuses.has(status)) {
      const key = terminalNotificationKey(record);
      rememberBounded(consumedTerminalNotifications, key);
      persistConsumedTerminalNotifications();
    }
  }
  function handleTerminalRuntimeTransition(runtime) {
    const record = runtime.active || runtime.source_review || runtime.source_ready || runtime.latest || null;
    const identity = terminalIdentity(record);
    if (!identity) return;
    const status = String(record?.status || runtime.status || '').toLowerCase();
    const previous = appState.terminalStatusByIdentity.get(identity) || '';
    appState.terminalStatusByIdentity.set(identity, status);
    if (!terminalRunStatuses.has(status)) return;
    releaseStaleInterfaceBusy();
    const key = terminalNotificationKey(record);
    const notification = terminalNotificationMessage(record);
    if (!previous || terminalRunStatuses.has(previous) || !notification) {
      rememberBounded(consumedTerminalNotifications, key);
      persistConsumedTerminalNotifications();
      uiTrace('TERMINAL_NOTIFICATION_DEDUPED', {job_id: record?.id || '', run_id: record?.run_id || '', stage: record?.stage || '', status});
      return;
    }
    uiTrace('TERMINAL_EVENT_RECEIVED', {job_id: record?.id || '', run_id: record?.run_id || '', stage: record?.stage || '', status});
    if (consumedTerminalNotifications.has(key)) {
      uiTrace('TERMINAL_NOTIFICATION_DEDUPED', {job_id: record?.id || '', run_id: record?.run_id || '', stage: record?.stage || '', status});
      return;
    }
    rememberBounded(consumedTerminalNotifications, key);
    persistConsumedTerminalNotifications();
    flashFrame();
    showToast(notification.message, notification.type, {key, kind: 'terminal'});
    uiTrace('TERMINAL_NOTIFICATION_EMITTED', {job_id: record?.id || '', run_id: record?.run_id || '', stage: record?.stage || '', status});
  }
  function canonicalProgressStage(runtime, record, progress) {
    const raw = String(progress?.stage_key || record?.stage || runtime?.stage || runtime?.status || 'idle').toLowerCase();
    if (String(record?.status || runtime?.status || '').toLowerCase() === 'review_required') return 'quality_review';
    if (String(record?.status || runtime?.status || '').toLowerCase() === 'finished') return 'finished';
    return raw || 'idle';
  }
  function metricFromProgress(progress, fallback = '') {
    if (progress?.total) return `${Number(progress.current || 0)}/${Number(progress.total || 0)}`;
    return fallback;
  }
  function buildPipelineState(runtime, progress = {}) {
    const record = runtime.active || runtime.source_review || runtime.source_ready || runtime.latest || runtime.latest_result || null;
    const status = String(record?.status || runtime.status || 'ready').toLowerCase();
    const rawStage = canonicalProgressStage(runtime, record, progress);
    const visualStage = visualStageKey(rawStage);
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    const fraction = Number.isFinite(Number(progress.fraction)) ? Number(progress.fraction) : (total ? current / total : null);
    const isTerminal = terminalRunStatuses.has(status) || ['failed', 'cancelled'].includes(status);
    const totalPages = Number(record?.page_count || record?.pages || progress.pages || progress.total_pages || 0);
    const pendingReview = Number(runtime.quality_review?.pending_count || record?.manual_review_count || record?.rejected_count || 0);
    return {
      requestId: appState.currentRequestId,
      jobId: String(record?.id || record?.job_id || appState.currentJobId || ''),
      runId: String(record?.run_id || appState.currentRunId || ''),
      sourceUrl: String(record?.url || appState.currentSourceUrl || ''),
      title: String(record?.chapter_name || appState.currentChapterName || 'Capítulo atual'),
      status,
      stage: rawStage,
      visualStage,
      stageStartedAt: record?.stage_started_at || '',
      createdAt: record?.created_at || record?.started_at || '',
      updatedAt: progress.updated_at || record?.updated_at || record?.finished_at || '',
      heartbeatAt: progress.updated_at || record?.updated_at || '',
      totalPages,
      completedPages: total ? current : Number(progress.pages || 0),
      currentPage: total ? current : 0,
      totalGroups: Number(progress.groups || record?.group_count || 0),
      completedGroups: Number(progress.completed_groups || 0),
      totalTranslations: Number(progress.total_translations || 0),
      completedTranslations: Number(progress.completed_translations || 0),
      progressPercent: fraction == null ? null : Math.max(0, Math.min(99, Math.round(fraction * 100))),
      elapsedSeconds: progress.elapsed_seconds ?? null,
      etaSeconds: progress.eta_seconds ?? null,
      message: String(progress.last_message || progress.stage || stageMessages[rawStage] || ''),
      reasonCode: String(record?.reason_code || ''),
      pendingReview,
      isTerminal,
      progress,
      record,
    };
  }
  function canonicalStageMetric(stage, state, progress) {
    const activeMetric = metricFromProgress(progress, 'em andamento');
    if (stage === state.visualStage && activeMetric !== '0/0') return activeMetric || 'em andamento';
    if (stage === 'awaiting_source_review' && state.totalPages) return `${state.totalPages} páginas`;
    if (stage === 'download' && state.totalPages) return `${state.totalPages}/${state.totalPages}`;
    if (stage === 'validation' && state.totalGroups) return `${state.totalGroups} grupos`;
    if (stage === 'ocr' && state.totalPages) return `${state.totalPages}/${state.totalPages}`;
    if (stage === 'translate' && state.totalTranslations) return `${state.completedTranslations || state.totalTranslations}/${state.totalTranslations}`;
    if (stage === 'render' && state.totalPages) return `${state.totalPages}/${state.totalPages}`;
    if (stage === 'pdf' && state.totalPages) return `${state.totalPages} páginas`;
    if (stage === 'quality_review' && state.pendingReview) return `${state.pendingReview} pendências`;
    return '';
  }
  function renderPipelinePreview(state) {
    const balloon = $('#balloonText');
    if (!balloon) return;
    const messageKey = state.status === 'ready' && !state.jobId ? 'idle' : (state.status === 'review_required' ? 'review_required' : state.stage);
    const message = stageMessages[messageKey] || stageMessages[state.visualStage] || 'Processando...';
    const detail = state.progress?.total
      ? `Página ${Number(state.progress.current || 0)} de ${Number(state.progress.total || 0)}`
      : state.pendingReview
        ? `Revisão: ${state.pendingReview} pendências`
        : '';
    balloon.innerHTML = detail ? `${escapeHtml(message)}<br><small>${escapeHtml(detail)}</small>` : escapeHtml(message);
  }
  function renderRuntime(runtime) {
    appState.status = runtime.status || 'ready';
    appState.queue = runtime.queue || [];
    const awaitingReview = appState.status === 'awaiting_source_review';
    const running = inFlightStatuses.has(appState.status);
    const analyzing = appState.status === 'staging';
    const visibleProgress = {...(runtime.progress || {})};
    const terminalLatest = runtime.latest || null;
    let presentationStatus = appState.status;
    if (!runtime.active && !runtime.source_review && !runtime.source_ready && terminalLatest
        && ['failed', 'cancelled'].includes(String(terminalLatest.status || '').toLowerCase())) {
      presentationStatus = String(terminalLatest.status || appState.status);
      visibleProgress.stage_key = terminalLatest.stage || visibleProgress.stage_key || 'source_analysis';
      visibleProgress.stage = stageMessages[visibleProgress.stage_key] || visibleProgress.stage || visibleProgress.stage_key;
    }
    const pipelineState = buildPipelineState(runtime, visibleProgress);
    appState.currentPipelineState = pipelineState;
    appState.activeJobId = runtime.active?.id || runtime.source_review?.id || runtime.source_ready?.id || '';
    appState.latestJobId = runtime.latest?.id || '';
    const incoming = runtime.active || runtime.source_review || runtime.source_ready || runtime.latest || null;
    const incomingJobId = String(incoming?.id || incoming?.job_id || '');
    const incomingRunId = String(incoming?.run_id || '');
    if (incomingJobId && (!appState.currentJobId || incomingJobId === appState.currentJobId)) {
      if (!appState.currentJobId) appState.currentJobId = incomingJobId;
      if (!appState.currentRunId && incomingRunId) {
        appState.currentRunId = incomingRunId;
        uiTrace('PIPELINE_RUN_ADOPTED', {
          request_id: appState.currentRequestId,
          job_id: incomingJobId,
          run_id: incomingRunId,
          stage: visibleProgress.stage_key || incoming.stage || '',
        });
      }
      persistPipelineIdentity(visibleProgress.stage_key || incoming.stage || appState.status);
    }
    setRunControls(running || analyzing, awaitingReview);
    const status = $('#appStatus');
    status.textContent = runStatusLabels[appState.status] || appState.status;
    status.dataset.state = appState.status;
    renderProgress(visibleProgress, pipelineState);
    const draftOnly = appState.newTranslationDraft && !runtime.active && !runtime.source_review;
    if (draftOnly) clearNewTranslationDraftPanels();
    else if (runtime.source_ready) {
      $('#runStatusCard') && ($('#runStatusCard').hidden = true);
    } else renderRunStatus({...runtime, status: presentationStatus, progress: visibleProgress});
    renderQueue();
    appendLogs(runtime.logs || []);
    if (awaitingReview && runtime.source_review && shouldRenderSourceReview(runtime.source_review)) {
      renderSourceReview(runtime.source_review);
    }
    else if (!awaitingReview) $('#sourceReviewPanel') && ($('#sourceReviewPanel').hidden = true);
    if (appState.status === 'source_analysis_ready' && runtime.source_ready) {
      renderSourceAnalysisReady(runtime.source_ready);
    } else if ($('#sourceReadyPanel')) {
      $('#sourceReadyPanel').hidden = true;
    }
    // After the user leaves review_mode the form belongs to a new chapter, so an
    // idle runtime's leftover review must not reappear over it.
    const reviewDismissed = appState.reviewPanelDismissed && !inFlightStatuses.has(appState.status);
    const qualityReviewJobId = String(runtime.quality_review?.job_id || '');
    const reviewOwnerJobId = String(
      appState.reviewMode?.jobId || appState.currentJobId || incomingJobId || '');
    const qualityReviewBelongsToOperation = Boolean(
      qualityReviewJobId && reviewOwnerJobId && qualityReviewJobId === reviewOwnerJobId);
    if (!runtime.source_ready && !draftOnly && !reviewDismissed
        && qualityReviewBelongsToOperation) renderQualityReview(runtime.quality_review);
    // In explicit review_mode the panel is owned by the selected finished chapter,
    // so a background poll of the (idle) active runtime must not hide it.
    else if (!appState.reviewMode && $('#qualityReviewPanel')) $('#qualityReviewPanel').hidden = true;
    const resultRecord = runtime.latest_result || runtime.latest;
    if (!runtime.source_ready && resultRecord && !awaitingReview && !draftOnly) {
      const latestId = String(resultRecord.id || resultRecord.job_id || '');
      const operationLatest = runtime.latest || null;
      const operationLatestId = String(operationLatest?.id || operationLatest?.job_id || '');
      const operationFailedWithoutResult = operationLatestId && operationLatestId !== latestId
        && ['failed', 'cancelled'].includes(String(operationLatest?.status || '').toLowerCase());
      if (operationFailedWithoutResult) {
        $('#artifactActions') && ($('#artifactActions').innerHTML = '');
        $('#runSummary') && ($('#runSummary').hidden = true);
      } else if (!appState.currentJobId || latestId === appState.currentJobId || terminalRunStatuses.has(presentationStatus)) {
        renderResult(resultRecord);
      } else {
        uiTrace('old_pipeline_event_discarded', {request_id: appState.currentRequestId});
      }
    }
    if (runtime.history_revision !== appState.historyRevision) refreshBootstrap();
  }
  function renderRunStatus(runtime) {
    const card = $('#runStatusCard');
    if (!card) return;
    const active = inFlightStatuses.has(runtime.status);
    const record = runtime.active || runtime.latest || null;
    const status = runtime.status === 'ready' && record ? record.status : runtime.status;
    const progress = runtime.progress || {};
    const pipelineState = appState.currentPipelineState || buildPipelineState(runtime, progress);
    card.hidden = !active && !record;
    if (card.hidden) return;
    const recordJobId = String(record?.id || record?.job_id || '');
    const recordRunId = String(record?.run_id || '');
    if (recordJobId && (!appState.currentJobId || appState.currentJobId === recordJobId)) {
      if (!appState.currentJobId) appState.currentJobId = recordJobId;
      if (!appState.currentRunId && recordRunId) appState.currentRunId = recordRunId;
      persistPipelineIdentity(progress.stage_key || record.stage || status);
    }
    card.dataset.currentJobId = appState.currentJobId || recordJobId;
    card.dataset.currentRunId = appState.currentRunId || recordRunId;
    card.dataset.currentStage = pipelineState.stage || progress.stage_key || record.stage || status;
    const failed = status === 'failed';
    $('#runStatusHuman').textContent = failed ? 'Não foi possível iniciar o processamento' : (stageMessages[pipelineState.stage] || stageMessages[pipelineState.visualStage] || runStatusLabels[status] || 'Processamento');
    const count = progress.total ? `${progress.current || 0} de ${progress.total}` : 'progresso sendo calculado';
    const terminalCount = pipelineState.pendingReview ? `${pipelineState.pendingReview} itens aguardando revisão` : (pipelineState.totalPages ? `${pipelineState.totalPages} páginas` : '');
    $('#runProgressHuman').textContent = active ? count : (status === 'finished' ? (terminalCount || 'PDF pronto') : status === 'review_required' ? (terminalCount || 'Alguns itens precisam de revisão') : failed ? (reasonText(record?.reason_code) || record?.error_message || 'O processamento não foi concluído.') : runStatusLabels[status] || '');
    $('#runEtaHuman').textContent = active ? (progress.eta_label || 'Tempo variavel nesta etapa') : '';
    const updated = progress.updated_at ? new Date(Number(progress.updated_at) * 1000) : null;
    $('#runUpdatedHuman').textContent = updated && !Number.isNaN(updated.getTime()) ? `Atualizado ha ${Math.max(0, Math.floor((Date.now() - updated.getTime()) / 1000))}s` : '';
    const canonicalIdentity = record?.source_analysis?.canonical_identity || {};
    const canonicalNotice = canonicalIdentity.validation_status
      ? 'Link oficial do episódio confirmado.' : '';
    $('#runNextHuman').textContent = progress.stale
      ? 'O processamento esta demorando mais que o esperado.'
      : [canonicalNotice, active ? 'Processamento em andamento' : failed ? 'Voce pode tentar novamente.' : '']
          .filter(Boolean).join(' ');
    const retry = $('#runRetryAction');
    if (retry) retry.hidden = !record || !['failed', 'cancelled'].includes(status);
    const cancel = $('#runCancelAction');
    if (cancel && !appState.cancelBusy) cancel.hidden = !active;
  }

  // Short labels for the summary line and filter chips.
  const VISUAL_STATE_LABELS = {
    applied: 'aplicadas',
    rejected_visual_regression: 'rejeitadas',
    manual_review: 'revisão manual',
    unchanged: 'sem alteração',
    pending: 'pendentes',
    report_only: 'somente relatório',
  };

  // Full sentence shown as each item's status, per the review spec.
  const VISUAL_STATE_STATUS = {
    applied: 'Alteração aplicada',
    rejected_visual_regression: 'Alteração rejeitada por segurança visual',
    manual_review: 'Revisão humana necessária',
    unchanged: 'Sem alteração',
    pending: 'Aguardando o gate visual',
    report_only: 'Não vinculado à revisão',
  };

  // Why the visual gate refused a region, in plain pt-BR. Unknown codes fall
  // back to the raw code so a new gate is never silently invisible.
  const VISUAL_REASON_LABELS = {
    translated_render_base_unavailable: 'Página traduzida-base indisponível',
    unsafe_incremental_mask: 'Máscara de alteração insegura',
    unexpected_pixels_outside_changed_regions: 'Pixels externos à região foram alterados',
    text_overlap_regression_detected: 'Possível texto sobreposto detectado',
    clean_region_background_unavailable: 'Não foi possível reconstruir o fundo com segurança',
    excessive_cleanup_mask: 'A máscara removeria uma área excessiva',
    empty_previous_text_mask: 'Não foi possível localizar o texto anterior',
    cleanup_mask_crosses_artwork: 'A limpeza pode atingir a arte',
    ambiguous_text_polarity: 'Não foi possível distinguir o texto do fundo com segurança',
    unsafe_inverted_cleanup_mask: 'A máscara invertida do balão escuro não era segura',
    dark_region_background_reconstruction_failed: 'Não foi possível reconstruir o fundo escuro',
    light_text_components_not_isolated: 'O que seria apagado parece arte, não letras',
    render_dimension_mismatch: 'O redesenho saiu com dimensões diferentes da página',
    reviewed_page_write_failed: 'A página revisada não pôde ser gravada',
    source_image_missing: 'A imagem de origem da página não foi encontrada',
  };

  function visualReasonLabel(code) {
    const key = String(code || '');
    if (!key) return '';
    return VISUAL_REASON_LABELS[key] || key;
  }

  function renderQualityReview(review) {
    const panel = $('#qualityReviewPanel');
    const list = $('#qualityReviewList');
    if (!panel || !list) return;
    appState.qualityReview = review;
    panel.hidden = false;
    updateQualityReviewDeveloperActions();
    const items = Array.isArray(review.items) ? review.items : [];
    const filter = appState.qualityReviewFilter || 'pending';
    const visible = items.filter(item => {
      if (VISUAL_STATE_LABELS[filter]) return String(item.visual_state || '') === filter;
      return filter === 'all' || (filter === 'pending' ? item.state === 'pending' : item.state !== 'pending');
    });
    const counts = items.reduce((acc, item) => {
      const risk = String(item.risk || 'LOW').toUpperCase();
      acc[risk] = (acc[risk] || 0) + 1;
      return acc;
    }, {LOW: 0, MEDIUM: 0, HIGH: 0});
    const visualSummary = review.visual_state_summary || {};
    // The chapter-wide gate summary counts the reviewed regions only; report-only
    // items are a panel bucket, appended separately so the region total stays 108.
    const visualParts = Object.keys(VISUAL_STATE_LABELS)
      .filter(state => state !== 'report_only' && Number(visualSummary[state] || 0) > 0)
      .map(state => `${VISUAL_STATE_LABELS[state]} ${Number(visualSummary[state])}`);
    const reportOnly = Number(review.report_only_count || 0);
    if (reportOnly > 0) visualParts.push(`somente relatório ${reportOnly}`);
    $('#qualityReviewMeta').textContent = `${review.pending_count || 0} pendentes · ${items.length} itens · LOW ${counts.LOW || 0} · MEDIUM ${counts.MEDIUM || 0} · HIGH ${counts.HIGH || 0}${visualParts.length ? ` · gate visual: ${visualParts.join(' · ')}` : ''} · ${review.confirmed ? 'Revisão concluída' : 'confirmação necessária'}`;
    renderReviewPreviewAccess();
    // Separate, clearly labelled action for the reviewed PDF; shown only when the
    // revision manifest points to a real reviewed file (never a glob).
    const reviewedPdf = $('#reviewedPdfAction');
    if (reviewedPdf) {
      const rp = review.reviewed_pdf;
      if (rp && rp.path) {
        reviewedPdf.hidden = false;
        reviewedPdf.innerHTML = `<button type="button" class="btn-primary" data-open-reviewed-pdf>ABRIR PDF REVISADO</button>`
          + `<span class="reviewed-pdf-name">${escapeHtml(rp.name || '')}${rp.sha256 ? ` · SHA ${escapeHtml(String(rp.sha256).slice(0, 16))}…` : ''}</span>`;
      } else {
        reviewedPdf.hidden = true;
        reviewedPdf.innerHTML = '';
      }
    }
    const visibleKeys = new Set(visible.map(item => String(item.key || '')));
    appState.qualityReviewSelection = new Set([...appState.qualityReviewSelection].filter(key => visibleKeys.has(key)));
    list.innerHTML = visible.length ? visible.map(item => {
      const actionClass = item.state === 'pending' ? ' show' : '';
      const checked = appState.qualityReviewSelection.has(String(item.key || '')) ? ' checked' : '';
      const risk = String(item.risk || 'LOW').toUpperCase();
      const visualState = String(item.visual_state || '');
      const visualBadge = visualState
        ? `<span class="quality-review-visual" data-visual-state="${escapeAttr(visualState)}">${escapeHtml(VISUAL_STATE_STATUS[visualState] || visualState)}</span>`
        : '';
      const visualReason = visualReasonLabel(item.visual_reason_code);
      const visualNote = visualState === 'rejected_visual_regression' && visualReason
        ? `<div class="quality-review-visual-reason">${escapeHtml(visualReason)}</div>` : '';
      const compare = visualState && item.page_url
        ? `<button type="button" class="btn-ghost review-compare show" data-review-compare="${escapeAttr(item.page)}">ABRIR COMPARAÇÃO</button>`
          + `<button type="button" class="btn-ghost review-compare show" data-revise-page="${escapeAttr(item.page)}">REVISAR ESTA PÁGINA</button>` : '';
      // Report-only items are not part of the revision: no checkbox, no
      // mark/preserve actions, so they can never enter a bulk operation.
      const isReportOnly = visualState === 'report_only';
      const selectBox = isReportOnly ? '' : `<input type="checkbox" class="quality-review-select" data-review-select="${escapeAttr(item.key)}"${checked}> `;
      const reviewActions = isReportOnly ? '' : `<button type="button" class="btn-ghost review-mark${actionClass}" data-review-action="reviewed">Marcar como revisado</button><button type="button" class="btn-ghost review-preserve${actionClass}" data-review-action="preserved_original">Manter original</button>`;
      return `<article class="quality-review-item" data-state="${escapeAttr(item.state)}" data-risk="${escapeAttr(risk)}" data-visual-state="${escapeAttr(visualState)}" data-review-key="${escapeAttr(item.key)}"><div class="quality-review-item-head"><label>${selectBox}<strong>Pagina ${escapeHtml(item.page)} · ${escapeHtml(item.label)}</strong></label><span class="quality-review-risk">${escapeHtml(risk)}</span><span class="quality-review-state">${escapeHtml(item.state === 'pending' ? 'pendente' : item.state === 'preserved_original' ? 'original mantido' : 'revisado')}</span>${visualBadge}</div><div class="quality-review-reason">${escapeHtml(item.reason)}</div>${visualNote}<div class="quality-review-text"><div><small>Original</small>${escapeHtml(item.original || '—')}</div><div><small>Traducao atual</small>${escapeHtml(item.translation || '—')}</div>${item.proposed_translation ? `<div><small>Proposta</small>${escapeHtml(item.proposed_translation)}</div>` : ''}</div>${item.page_url ? `<img class="quality-review-thumb" src="${escapeAttr(item.page_url)}" alt="Miniatura da pagina ${escapeAttr(item.page)}" loading="lazy">` : ''}<div class="cta-row">${reviewActions}${compare}</div></article>`;
    }).join('') : '<div class="muted">Nenhum item neste filtro.</div>';
    const confirm = $('#confirmQualityReview');
    if (confirm) {
      confirm.hidden = Boolean(review.confirmed);
      confirm.disabled = Boolean(review.confirmed) || Number(review.pending_count || 0) > 0;
      confirm.title = confirm.disabled && !review.confirmed ? 'Revise cada item ou mantenha o original antes de confirmar.' : '';
    }
    updateQualityReviewSelectionUi();
    pollQualityRevisionStatus(review.job_id, {once: true});
  }

  function visibleQualityReviewKeys({risk = ''} = {}) {
    const root = $('#qualityReviewList');
    if (!root) return [];
    return $$('.quality-review-item', root)
      // Report-only items are not part of the revision, so bulk actions and
      // select-all must never include them.
      .filter(item => item.dataset.visualState !== 'report_only')
      .filter(item => !risk || String(item.dataset.risk || '').toUpperCase() === String(risk).toUpperCase())
      .map(item => String(item.dataset.reviewKey || ''))
      .filter(Boolean);
  }

  function updateQualityReviewSelectionUi() {
    const count = appState.qualityReviewSelection.size;
    const label = $('#qualityReviewSelectedCount');
    if (label) label.textContent = `${count} selecionado${count === 1 ? '' : 's'}`;
    const all = $('#qualityReviewSelectAll');
    const visible = visibleQualityReviewKeys();
    if (all) {
      all.checked = visible.length > 0 && visible.every(key => appState.qualityReviewSelection.has(key));
      all.indeterminate = !all.checked && visible.some(key => appState.qualityReviewSelection.has(key));
    }
    const undo = $('#undoBulkReview');
    if (undo) undo.disabled = appState.qualityReviewUndo.length === 0 || appState.qualityReviewBulkBusy;
  }

  function setQualityReviewBulkMessage(message, type = '') {
    const node = $('#qualityReviewBulkMessage');
    if (!node) return;
    node.textContent = message || '';
    node.dataset.state = type || '';
  }

  function qualityReviewDeveloperMode() {
    try {
      const params = new URLSearchParams(window.location.search || '');
      return params.get('dev') === '1' || params.get('review_dev') === '1' || localStorage.getItem('tradutorDeveloperMode') === '1';
    } catch (_) {
      return false;
    }
  }

  function updateQualityReviewDeveloperActions() {
    const dev = qualityReviewDeveloperMode();
    const canary = $('#nvidiaContractCanary');
    if (canary) canary.hidden = !dev;
    const diagnostics = $('#runtimeDiagnostics');
    if (!diagnostics) return;
    diagnostics.hidden = !dev;
    // Which build is actually serving this page — developer mode only, so a
    // regular user never sees process internals.
    if (dev && !diagnostics.dataset.loaded) {
      diagnostics.dataset.loaded = '1';
      api('/api/ui/diagnostics').then(info => {
        diagnostics.textContent = `build ${String(info.git_head || '').slice(0, 7)} · pid ${info.pid}`
          + ` · worker ${info.worker_online ? `online (pid ${info.worker_pid ?? '?'})` : 'offline'}`
          + ` · taxonomia v${info.taxonomy_version} · gate v${info.gate_version ?? '?'}`
          + ` · detector OCR v${info.ocr_plausibility_version ?? '?'}`
          + ` · schema ${info.review_schema_version} · desde ${String(info.server_started_at || '').slice(0, 19)}`;
      }).catch(() => { diagnostics.textContent = 'diagnóstico indisponível'; delete diagnostics.dataset.loaded; });
    }
  }

  const REVISION_CANCELLABLE_STATES = new Set(['queued', 'running', 'cancelling']);
  const REVISION_RESUMABLE_STATES = new Set(['cancelled', 'interrupted', 'failed']);

  function renderQualityRevisionStatus(status = null) {
    const panel = $('#qualityRevisionStatus');
    if (!panel) return;
    if (!status || status.status === 'not_started') {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const phase = $('#qualityRevisionPhase');
    const meta = $('#qualityRevisionMeta');
    const label = status.phase_label || status.phase || 'Revisão em andamento';
    const pages = status.total_pages ? `${status.total_pages} páginas` : 'páginas sendo calculadas';
    const regions = status.total_regions ? `${status.total_regions} regiões` : 'regiões sendo calculadas';
    const requests = Number(status.requests || 0);
    const responseStats = [
      status.valid_response_batches ? `${status.valid_response_batches} válidas` : '',
      status.repaired_batches ? `${status.repaired_batches} reparadas` : '',
      status.fallback_individual_requests ? `${status.fallback_individual_requests} fallback` : '',
      status.invalid_response_batches ? `${status.invalid_response_batches} inválidas` : '',
      status.manual_review ? `${status.manual_review} manual` : '',
      status.validity_rate != null ? `${Math.round(Number(status.validity_rate || 0) * 100)}%` : '',
    ].filter(Boolean).join(' · ');
    if (phase) phase.textContent = label;
    if (meta) {
      // Live per-region counters come from the running loop's checkpoint, so a
      // revision in flight shows real progress instead of a stale zero.
      const done = Number(status.regions_completed || 0);
      const pending = Number(status.regions_pending || 0);
      const progressBits = [
        status.suspicious_regions != null ? `${status.suspicious_regions} suspeitas` : '',
        status.skipped_unchanged_regions != null ? `${status.skipped_unchanged_regions} não selecionadas` : '',
        (done || pending) ? `${done} concluídas · ${pending} pendentes` : '',
        status.resumed_regions ? `${status.resumed_regions} reaproveitadas` : '',
        status.cache_hits ? `${status.cache_hits} cache` : '',
        status.retries ? `${status.retries} retry` : '',
        status.elapsed_ms ? `${Math.round(Number(status.elapsed_ms) / 1000)}s` : '',
      ].filter(Boolean).join(' · ');
      meta.textContent = `revision_id ${String(status.revision_id || '—').slice(0, 8)} · ${pages} · ${regions} · ${requests} requisições · ${status.status || 'running'}${progressBits ? ' · ' + progressBits : ''}${responseStats ? ' · ' + responseStats : ''}`;
    }
    // A revision must always offer the next honest action: stop it while it runs,
    // or continue it from the checkpoint once it stopped.
    const state = String(status.status || '');
    const cancelBtn = $('#cancelRevisionAction');
    const resumeBtn = $('#resumeRevisionAction');
    if (cancelBtn) {
      cancelBtn.hidden = !REVISION_CANCELLABLE_STATES.has(state);
      cancelBtn.disabled = state === 'cancelling';
      cancelBtn.textContent = state === 'cancelling' ? 'CANCELANDO…' : 'CANCELAR REVISÃO';
    }
    if (resumeBtn) resumeBtn.hidden = !REVISION_RESUMABLE_STATES.has(state);
  }

  async function pollQualityRevisionStatus(jobId, {once = false} = {}) {
    if (!jobId) return null;
    try {
      const status = await api(`/api/ui/quality-review/revision/${encodeURIComponent(jobId)}`);
      renderQualityRevisionStatus(status);
      if (!once && ['running', 'starting'].includes(String(status.status || ''))) {
        clearTimeout(appState.qualityRevisionPoll);
        appState.qualityRevisionPoll = setTimeout(() => pollQualityRevisionStatus(jobId), 2500);
      } else if (!once && status.status) {
        clearTimeout(appState.qualityRevisionPoll);
        appState.qualityRevisionPoll = null;
        pollState();
      }
      return status;
    } catch (error) {
      renderQualityRevisionStatus(null);
      return null;
    }
  }

  async function qualityReviewBulkAction({action = 'reviewed', keys = [], riskFilter = '', confirmation = false} = {}) {
    if (!appState.qualityReview?.job_id || appState.qualityReviewBulkBusy) return;
    const selected = keys.length ? keys : [...appState.qualityReviewSelection];
    if (!selected.length) { setQualityReviewBulkMessage('Selecione ao menos um item.', 'warn'); return; }
    if (confirmation && selected.length) {
      const highCount = (appState.qualityReview.items || []).filter(item => selected.includes(String(item.key)) && String(item.risk || '').toUpperCase() === 'HIGH').length;
      const pages = Array.from(new Set((appState.qualityReview.items || []).filter(item => selected.includes(String(item.key))).map(item => item.page))).sort((a, b) => Number(a) - Number(b));
      const ok = window.confirm(`Você está prestes a aceitar ${selected.length} itens pendentes em ${pages.length} páginas. Alto risco: ${highCount}. Esta ação não publica o capítulo.`);
      if (!ok) return;
      if (highCount > 0 && !window.confirm('Esta seleção inclui itens de alto risco. Confirme novamente para aceitar TODOS; caso contrário use Aceitar baixo risco.')) {
        setQualityReviewBulkMessage('Ação cancelada: itens de alto risco não foram aceitos.', 'warn');
        return;
      }
    }
    appState.qualityReviewBulkBusy = true;
    setQualityReviewBulkMessage('Aplicando ação em massa...', 'busy');
    try {
      appState.qualityReviewUndo.push({keys: selected, previous: (appState.qualityReview.items || []).filter(item => selected.includes(String(item.key))).map(item => [String(item.key), String(item.state || 'pending')])});
      const review = await api('/api/ui/quality-review/bulk-action', {method: 'POST', body: JSON.stringify({job_id: appState.qualityReview.job_id, item_keys: selected, action, risk_filter: riskFilter})});
      appState.qualityReviewSelection = new Set();
      renderQualityReview(review);
      setQualityReviewBulkMessage(`${review.bulk?.count || selected.length} itens atualizados.`, 'ok');
      showToast('Ação em massa aplicada.', 'ok');
    } catch (error) {
      setQualityReviewBulkMessage(error.message || 'Não foi possível aplicar a ação em massa.', 'error');
      showToast(error.message || 'Não foi possível aplicar a ação em massa.', 'error');
    } finally {
      appState.qualityReviewBulkBusy = false;
      updateQualityReviewSelectionUi();
    }
  }

  // Side-by-side of the page as published versus the page the revision produced,
  // so a rejected region can be judged instead of just read about.
  function openVisualComparison(page) {
    const jobId = appState.qualityReview?.job_id;
    if (!jobId || !page) return;
    const dialog = $('#visualComparisonDialog');
    const body = $('#visualComparisonBody');
    if (!dialog || !body) return;
    const base = `/api/ui/quality-review/${encodeURIComponent(jobId)}/page/${encodeURIComponent(page)}`;
    body.innerHTML = `<figure><figcaption>PDF base (publicado)</figcaption><img src="${escapeAttr(base)}" alt="Página ${escapeAttr(page)} publicada"></figure>`
      + `<figure><figcaption>PDF revisado</figcaption><img id="visualComparisonRevised" src="${escapeAttr(base)}?revision=latest" alt="Página ${escapeAttr(page)} revisada"></figure>`
      + `<div class="visual-comparison-cta"><button type="button" class="btn-ghost" data-revise-page="${escapeAttr(page)}">REVISAR ESTA PÁGINA</button></div>`;
    // A region that was not applied (manual review, or a report-only item) has
    // no revised page: show a clear note instead of a broken/404 image.
    const revised = $('#visualComparisonRevised');
    if (revised) revised.addEventListener('error', () => {
      const fig = revised.closest('figure');
      if (fig) fig.innerHTML = '<figcaption>PDF revisado</figcaption><div class="visual-comparison-empty">Não há versão revisada — alteração não aplicada</div>';
    }, {once: true});
    dialog.hidden = false;
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
  }

  function openHumanPreviewComparison(item) {
    if (!item) return;
    const dialog = $('#visualComparisonDialog');
    const body = $('#visualComparisonBody');
    if (!dialog || !body) return;
    const page = item.page_display_number || item.page_number || item.page_id || '';
    const ready = item.approval_enabled === true && item.blocked !== true;
    const status = ready ? 'PRONTA PARA REVISÃO HUMANA' : 'BLOQUEADA — REQUER RECONSTRUÇÃO DE ARTE';
    const before = item.region_crop_url || '';
    const after = item.preview_image_url || '';
    const gates = [
      `gate visual: ${item.visual_gate?.status || 'indisponível'}`,
      `gate linguístico: ${item.linguistic_gate?.status || 'indisponível'}`,
      `fora da máscara: ${Number(item.visual_gate?.isolation?.changed_pixels_outside_mask || 0)}`,
      `tinta residual: ${Number(item.visual_gate?.residual_source_pixels || 0)}`,
      item.font_selection?.selected_font ? `fonte: ${item.font_selection.selected_font}` : '',
      item.font_runtime_validation?.actual_font_identity ? `fonte real: ${item.font_runtime_validation.actual_font_identity}` : '',
      item.font_runtime_validation?.fallback_used ? 'fallback de fonte detectado' : '',
      item.font_gate?.status ? `gate fonte: ${item.font_gate.status}` : '',
    ].filter(Boolean).join(' · ');
    body.innerHTML = `<section class="human-preview-comparison">`
      + `<div class="human-preview-comparison-head"><strong>PRÉVIA DA PÁGINA ${escapeHtml(page)}</strong>`
      + `<span class="${ready ? 'ok' : 'warn'}">${escapeHtml(status)}</span>`
      + `<small>${escapeHtml(item.region_id || '')} · ${escapeHtml(gates)}</small></div>`
      + `<div class="preview-compare">`
      + `<figure><figcaption>ANTES</figcaption>${before ? `<img data-human-preview-url="${escapeAttr(before)}" alt="Trecho original antes da prévia">` : '<div class="visual-comparison-empty">Imagem original indisponível</div>'}</figure>`
      + `<figure><figcaption>DEPOIS</figcaption>${after ? `<img data-human-preview-url="${escapeAttr(after)}" alt="Rascunho isolado depois da prévia">` : '<div class="visual-comparison-empty">Prévia não renderizada</div>'}</figure>`
      + `</div><div class="preview-approval-note">Aprovar, pedir ajuste, rejeitar ou descartar fica no painel de revisão aberto atrás desta comparação. Nenhum clique foi executado automaticamente.</div>`
      + `</section>`;
    dialog.hidden = false;
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
    void loadHumanComparisonImages(body);
  }

  async function loadHumanComparisonImages(root) {
    for (const img of $$('img[data-human-preview-url]', root || document)) {
      const path = String(img.dataset.humanPreviewUrl || '');
      if (!path || img.dataset.loaded === '1') continue;
      img.dataset.loaded = '1';
      try {
        const response = await api(path, {rawResponse: true});
        const url = URL.createObjectURL(await response.blob());
        const key = `dialog:${path}`;
        if (previewCropUrls.get(key)) URL.revokeObjectURL(previewCropUrls.get(key));
        previewCropUrls.set(key, url);
        img.src = url;
      } catch (error) {
        img.replaceWith(Object.assign(document.createElement('div'), {
          className: 'visual-comparison-empty',
          textContent: `Imagem indisponível: ${error.message || ''}`,
        }));
      }
    }
  }

  async function qualityReviewAction(event) {
    const revise = event.target.closest('[data-revise-page]');
    if (revise) { openPageRevision(Number(revise.dataset.revisePage)); return; }
    const compare = event.target.closest('[data-review-compare]');
    if (compare) { openVisualComparison(compare.dataset.reviewCompare); return; }
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
  $('#reviewedPdfAction')?.addEventListener('click', event => {
    if (!event.target.closest('[data-open-reviewed-pdf]')) return;
    const path = appState.qualityReview?.reviewed_pdf?.path;
    if (path) openArtifact(path);
  });
  $('#visualComparisonClose')?.addEventListener('click', () => {
    const dialog = $('#visualComparisonDialog');
    if (!dialog) return;
    if (typeof dialog.close === 'function' && dialog.open) dialog.close();
    dialog.hidden = true;
  });

  /* ---------- targeted page revision (BLOCO 1) ---------- */
  const pageRevisionState = {page: null, pageRevisionId: null, regions: [], manifest: null};

  function pageRevisionIdentity() {
    let runId = '';
    try { runId = String(new URLSearchParams(window.location.search || '').get('run_id') || ''); } catch (_) {}
    return {job_id: String(appState.qualityReview?.job_id || ''), run_id: runId};
  }

  function pageRevisionMessage(message, type = '') {
    const node = $('#pageRevisionMessage');
    if (node) { node.textContent = message || ''; node.dataset.state = type || ''; }
  }

  function pageRevisionSyncUrl() {
    try {
      const url = new URL(window.location.href);
      if (pageRevisionState.pageRevisionId) {
        url.searchParams.set('page_rev', pageRevisionState.pageRevisionId);
        url.searchParams.set('page_rev_page', String(pageRevisionState.page || ''));
      } else {
        url.searchParams.delete('page_rev'); url.searchParams.delete('page_rev_page');
      }
      window.history.replaceState({}, '', url);
    } catch (_) {}
  }

  async function openPageRevision(page, {restore = false, pageRevisionId = '', focusRegion = ''} = {}) {
    const id = pageRevisionIdentity();
    const pageNo = Number(page);
    if (!id.job_id || !pageNo) { showToast('Abra um capítulo em revisão primeiro.', 'error'); return; }
    pageRevisionState.page = pageNo;
    pageRevisionState.pageRevisionId = pageRevisionId || null;
    pageRevisionState.focusRegion = focusRegion || '';
    const dialog = $('#pageRevisionPanel');
    if (dialog) { dialog.hidden = false; if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal(); }
    $('#pageRevisionTitle').textContent = `Revisão da página ${pageNo}`;
    pageRevisionMessage('Carregando regiões detectadas…');
    try {
      const listing = await api('/api/ui/page-revision/regions', {method: 'POST', body: JSON.stringify({...id, page: pageNo})});
      pageRevisionState.regions = listing.regions || [];
      renderPageRevisionRegions(listing);
    } catch (error) { pageRevisionMessage(error.message || 'Falha ao listar regiões.', 'error'); }
    if (pageRevisionId) await refreshPageRevisionStatus();
    pageRevisionSyncUrl();
  }

  function renderPageRevisionRegions(listing) {
    const root = $('#pageRevisionRegions');
    if (!root) return;
    const focus = String(pageRevisionState.focusRegion || '');
    const rows = (listing.regions || []).map(r => {
      // Badges render the backend's resolved policy; the UI never infers one
      // from a classification string.
      const badge = !r.reviewable ? '<span class="pr-badge pr-preserved">preservada</span>'
        : r.cache_hit ? '<span class="pr-badge pr-cache">cache</span>'
        : r.translatable ? '<span class="pr-badge pr-auth">exige NVIDIA</span>'
        : '<span class="pr-badge pr-review">revisão humana</span>';
      const checked = focus && focus === String(r.region_id) ? ' checked' : '';
      const box = r.reviewable ? `<input type="checkbox" class="pr-select" data-pr-region="${escapeAttr(r.region_id)}"${checked}>` : '';
      const normalized = r.classification_normalized
        ? `<em class="pr-normalized">${escapeHtml(r.classification_normalized)}</em>` : '';
      return `<label class="pr-region"${focus === String(r.region_id) ? ' data-focus="1"' : ''}>`
        + `<span class="pr-region-head">${box}<strong>${escapeHtml(r.region_id)}</strong> <em>${escapeHtml(r.classification)}</em> ${normalized} ${badge}</span>`
        + `<span class="pr-region-text"><small>fonte</small> ${escapeHtml(r.source_text || '—')}</span>`
        + `<span class="pr-region-text"><small>tradução</small> ${escapeHtml(r.current_translation || '—')}</span></label>`;
    }).join('');
    root.innerHTML = `<div class="pr-summary">${listing.reviewable_regions || 0} revisáveis · ${listing.requests_needed || 0} exigiriam NVIDIA (não serão chamadas)</div>${rows || '<div class="muted">Nenhuma região.</div>'}`;
  }

  function selectedPageRegions() {
    return $$('.pr-select', $('#pageRevisionRegions')).filter(c => c.checked).map(c => String(c.dataset.prRegion || ''));
  }

  async function startPageRevisionDraft(regionIds) {
    const id = pageRevisionIdentity();
    pageRevisionMessage('Criando prévia a partir do cache…');
    try {
      const manifest = await api('/api/ui/page-revision/start', {method: 'POST',
        body: JSON.stringify({...id, page: pageRevisionState.page, region_ids: regionIds || null})});
      pageRevisionState.pageRevisionId = manifest.page_revision_id;
      renderPageRevisionStatus(manifest);
      pageRevisionSyncUrl();
    } catch (error) { pageRevisionMessage(error.message || 'Falha ao criar a prévia.', 'error'); }
  }

  async function refreshPageRevisionStatus() {
    if (!pageRevisionState.pageRevisionId) return;
    const id = pageRevisionIdentity();
    try {
      const manifest = await api('/api/ui/page-revision/status', {method: 'POST',
        body: JSON.stringify({...id, page_revision_id: pageRevisionState.pageRevisionId})});
      renderPageRevisionStatus(manifest);
    } catch (error) { pageRevisionMessage(error.message || 'Falha ao consultar estado.', 'error'); }
  }

  function renderPageRevisionStatus(manifest) {
    pageRevisionState.manifest = manifest;
    pageRevisionState.pageRevisionId = manifest.page_revision_id || pageRevisionState.pageRevisionId;
    const auth = Number(manifest.requests_needed || 0);
    $('#pageRevisionMeta').textContent = `id ${String(manifest.page_revision_id || '').slice(0, 8)} · ${manifest.status}`
      + ` · ${manifest.safe_changes_applied || 0} aplicadas` + (auth ? ` · ${auth} exigem autorização NVIDIA` : '');
    const preview = $('#pageRevisionPreview');
    const id = pageRevisionIdentity();
    const draft = manifest.draft_page_path
      ? `/api/ui/page-revision/${encodeURIComponent(id.job_id)}/${encodeURIComponent(manifest.page_revision_id)}/draft?run_id=${encodeURIComponent(id.run_id)}`
      : '';
    if (preview) preview.innerHTML = draft
      ? `<figure><figcaption>Prévia (rascunho)</figcaption><img src="${escapeAttr(draft)}" alt="Prévia da página ${escapeAttr(manifest.page)}"></figure>`
      : (auth ? '<div class="muted">Sem prévia: regiões selecionadas exigem autorização do provedor.</div>' : '');
    const active = ['running', 'cancelling'].includes(String(manifest.status));
    const resumable = ['cancelled', 'awaiting_provider_authorization'].includes(String(manifest.status));
    $('#pageRevisionCancel').hidden = !active;
    $('#pageRevisionResume').hidden = !resumable;
    // A rejected draft still has files on disk, so it must stay discardable;
    // only approve is limited to a ready draft.
    const status = String(manifest.status);
    $('#pageRevisionDecision').hidden = !['draft_ready', 'rejected'].includes(status);
    const approve = $('[data-page-decision="approved"]');
    if (approve) approve.disabled = status !== 'draft_ready';
    if (manifest.status === 'awaiting_provider_authorization')
      pageRevisionMessage(`${auth} região(ões) exigem autorização NVIDIA. Nenhuma chamada foi feita.`, 'warn');
    else if (manifest.status === 'draft_ready') pageRevisionMessage('Prévia pronta. Aprove, rejeite ou descarte.', 'ok');
    else pageRevisionMessage(`Estado: ${manifest.status}.`);
  }

  async function pageRevisionDecision(outcome) {
    if (!pageRevisionState.pageRevisionId) return;
    const id = pageRevisionIdentity();
    try {
      const manifest = await api('/api/ui/page-revision/decision', {method: 'POST',
        body: JSON.stringify({...id, page_revision_id: pageRevisionState.pageRevisionId, outcome})});
      renderPageRevisionStatus(manifest);
      showToast(`Rascunho: ${manifest.status}. O v8 permanece intacto.`, 'ok');
    } catch (error) { pageRevisionMessage(error.message || 'Falha na decisão.', 'error'); }
  }

  async function pageRevisionForgotten() {
    const id = pageRevisionIdentity();
    pageRevisionMessage('Procurando texto esquecido nesta página…');
    try {
      const result = await api('/api/ui/page-revision/forgotten-text', {method: 'POST',
        body: JSON.stringify({...id, page: pageRevisionState.page})});
      const rows = (result.candidates || []).map(c => `<div class="pr-candidate">${escapeHtml(c.region_id)} · ${escapeHtml(c.classification)}`
        + `${c.do_not_translate ? ' · <em>não traduzir</em>' : ' · decisão humana'} — ${escapeHtml(c.source_text || c.current_translation || '')}</div>`).join('');
      $('#pageRevisionPreview').innerHTML = `<div class="pr-candidates"><strong>${result.candidate_count} candidato(s)</strong>${rows}</div>`;
      pageRevisionMessage('Candidatos exibidos para decisão humana. Nada foi traduzido.', 'ok');
    } catch (error) { pageRevisionMessage(error.message || 'Falha na busca.', 'error'); }
  }

  function pageRevisionManual() {
    if (!pageRevisionState.pageRevisionId) { pageRevisionMessage('Crie uma prévia antes de adicionar região manual.', 'warn'); return; }
    const form = $('#pageRevisionManualForm');
    if (form) form.hidden = !form.hidden;
  }

  async function pageRevisionManualSubmit(event) {
    event.preventDefault();
    const box = ['prManualX', 'prManualY', 'prManualW', 'prManualH'].map(id => parseInt($('#' + id)?.value, 10));
    if (box.some(n => Number.isNaN(n))) { pageRevisionMessage('Caixa inválida. Preencha x, y, largura e altura.', 'error'); return; }
    const source = String($('#prManualSource')?.value || '');
    const id = pageRevisionIdentity();
    try {
      const entry = await api('/api/ui/page-revision/manual-region', {method: 'POST',
        body: JSON.stringify({...id, page_revision_id: pageRevisionState.pageRevisionId, box, source_text: source, region_type: 'speech'})});
      pageRevisionMessage(`Região manual ${entry.region_id} registrada (imagem não alterada).`, 'ok');
      const form = $('#pageRevisionManualForm'); if (form) form.hidden = true;
    } catch (error) { pageRevisionMessage(error.message || 'Falha ao adicionar região.', 'error'); }
  }

  async function pageRevisionLifecycle(kind) {
    if (!pageRevisionState.pageRevisionId) return;
    const id = pageRevisionIdentity();
    try {
      const manifest = await api(`/api/ui/page-revision/${kind}`, {method: 'POST',
        body: JSON.stringify({...id, page_revision_id: pageRevisionState.pageRevisionId})});
      renderPageRevisionStatus(manifest);
    } catch (error) { pageRevisionMessage(error.message || 'Falha na ação.', 'error'); }
  }

  $('#openPageRevision')?.addEventListener('click', () => openPageRevision(Number($('#pageRevisionPageInput')?.value || 0)));
  $('#pageRevisionClose')?.addEventListener('click', () => {
    const d = $('#pageRevisionPanel'); if (d) { if (typeof d.close === 'function' && d.open) d.close(); d.hidden = true; }
    pageRevisionState.pageRevisionId = null; pageRevisionSyncUrl();
  });
  $('#pageRevisionStart')?.addEventListener('click', () => startPageRevisionDraft(selectedPageRegions().length ? selectedPageRegions() : null));
  $('#pageRevisionBalloon')?.addEventListener('click', () => {
    const sel = selectedPageRegions();
    if (sel.length !== 1) { pageRevisionMessage('Selecione exatamente um balão para revisar.', 'warn'); return; }
    startPageRevisionDraft(sel);
  });
  $('#pageRevisionForgotten')?.addEventListener('click', pageRevisionForgotten);
  $('#pageRevisionManual')?.addEventListener('click', pageRevisionManual);
  $('#pageRevisionManualForm')?.addEventListener('submit', pageRevisionManualSubmit);
  $('#pageRevisionManualCancel')?.addEventListener('click', () => { const f = $('#pageRevisionManualForm'); if (f) f.hidden = true; });
  $('#pageRevisionCancel')?.addEventListener('click', () => pageRevisionLifecycle('cancel'));
  $('#pageRevisionResume')?.addEventListener('click', () => pageRevisionLifecycle('resume'));
  $('#pageRevisionDecision')?.addEventListener('click', event => {
    const btn = event.target.closest('[data-page-decision]');
    if (btn) pageRevisionDecision(btn.dataset.pageDecision);
  });
  $('#visualComparisonBody')?.addEventListener('click', event => {
    const btn = event.target.closest('[data-revise-page]');
    if (!btn) return;
    const cmp = $('#visualComparisonDialog');
    if (cmp) { if (typeof cmp.close === 'function' && cmp.open) cmp.close(); cmp.hidden = true; }
    openPageRevision(Number(btn.dataset.revisePage));
  });

  // F5 restore: reopen a page revision from the URL once the chapter review is up.
  function restorePageRevisionFromUrl() {
    let params;
    try { params = new URLSearchParams(window.location.search || ''); } catch (_) { return; }
    const prid = String(params.get('page_rev') || '');
    const page = Number(params.get('page_rev_page') || 0);
    if (prid && page) openPageRevision(page, {restore: true, pageRevisionId: prid});
  }

  /* ---------- linguistic audit review (BLOCO 3) ---------- */
  const auditState = {review: null, mode: 'list', triage: null, providerSet: null,
    ocrCandidates: null, editorial: null, previews: null, selection: new Set()};

  function auditMessage(text, type = '') {
    const node = $('#auditMessage');
    if (node) { node.textContent = text || ''; node.dataset.state = type || ''; }
  }

  function auditSyncUrl(open) {
    try {
      const url = new URL(window.location.href);
      if (open) {
        url.searchParams.set('audit', '1');
        // The audit lives inside a chapter review, so its URL has to carry the
        // chapter's identity too — 'audit=1' alone cannot be reopened after F5.
        const id = pageRevisionIdentity();
        const jobId = id.job_id || String(appState.reviewMode?.jobId || '');
        const runId = id.run_id || String(appState.reviewMode?.runId || '');
        if (jobId) {
          url.searchParams.set('view', 'review');
          url.searchParams.set('job_id', jobId);
          if (runId) url.searchParams.set('run_id', runId);
        }
      } else {
        url.searchParams.delete('audit');
      }
      window.history.replaceState({}, '', url);
    } catch (_) {}
  }

  async function openLinguisticAudit({restore = false} = {}) {
    const id = pageRevisionIdentity();
    if (!id.job_id) { showToast('Abra um capítulo em revisão primeiro.', 'error'); return; }
    const dialog = $('#linguisticAuditPanel');
    if (dialog) { dialog.hidden = false; if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal(); }
    auditMessage('Carregando auditoria…');
    try {
      // On an F5 restore the auth token can still be initialising; retry a few
      // times (condition, not a fixed sleep) so the panel populates once ready.
      const attempts = restore ? 6 : 1;
      let review, lastError;
      for (let i = 0; i < attempts; i++) {
        try { review = await api('/api/ui/audit/review', {method: 'POST', body: JSON.stringify(pageRevisionIdentity())}); break; }
        catch (err) {
          lastError = err;
          if (!/authentication|401|forbidden|expired/i.test(String(err.message || ''))) throw err;
          await new Promise(r => setTimeout(r, 400));
        }
      }
      if (!review) throw lastError;
      auditState.review = review;
      renderAuditSummary(review);
      populateAuditCategoryFilter(review);
      // Restoring a non-default mode must re-render that mode, not the list.
      if (auditState.mode && auditState.mode !== 'list') await setAuditMode(auditState.mode);
      else renderAuditList();
      auditMessage('');
    } catch (error) { auditMessage(error.message || 'Falha ao carregar a auditoria.', 'error'); }
    auditSyncUrl(true);
  }

  function renderAuditSummary(review) {
    const s = review.summary || {};
    $('#auditMeta').textContent = `revisão ${String(review.revision_id || '').slice(0, 8)} · taxonomia v${review.taxonomy_version || '?'}`;
    const cats = Object.entries(s.by_normalized_category || {}).map(([k, v]) => `${escapeHtml(k)} ${Number(v)}`).join(' · ');
    $('#auditSummary').innerHTML = `<div><strong>${Number(s.total_regions_audited || 0)}</strong> regiões · `
      + `report_only ${Number(s.report_only_total || 0)} (traduzíveis ${Number(s.report_only_now_translatable || 0)}) · `
      + `revisão humana ${Number(s.needs_human_review_total || 0)} · exigem provider ${Number(s.provider_required_total || 0)}</div>`
      + (cats ? `<div class="audit-cats">${cats}</div>` : '');
  }

  function populateAuditCategoryFilter(review) {
    const select = $('#auditFilterCategory');
    if (!select) return;
    const current = select.value;
    const cats = Object.keys(review.summary?.by_normalized_category || {}).sort();
    select.innerHTML = '<option value="">todas</option>' + cats.map(c => `<option value="${escapeAttr(c)}">${escapeHtml(c)}</option>`).join('');
    select.value = current;
  }

  function auditVisibleRecords() {
    const review = auditState.review;
    if (!review) return [];
    const cat = $('#auditFilterCategory')?.value || '';
    const action = $('#auditFilterAction')?.value || '';
    const hr = $('#auditFilterHumanReview')?.checked;
    const ro = $('#auditFilterReportOnly')?.checked;
    const prov = $('#auditFilterProvider')?.checked;
    const cacheHit = $('#auditFilterCacheHit')?.checked;
    const page = Number($('#auditFilterPage')?.value || 0);
    return (review.records || []).filter(r =>
      (!cat || r.classification_normalized === cat)
      && (!action || r.suggested_action === action)
      && (!hr || r.needs_human_review)
      && (!ro || r.report_only)
      && (!prov || r.provider_required)
      && (!cacheHit || r.cache_status === 'answered')
      && (!page || Number(r.page_number) === page));
  }

  const AUDIT_DECISIONS = [['translate', 'TRADUZÍVEL'], ['preserve', 'PRESERVAR'],
    ['ocr_invalid', 'OCR INVÁLIDO'], ['needs_review', 'REVISÃO HUMANA'], ['dismissed', 'DESCARTAR DECISÃO']];

  function renderAuditList() {
    const list = $('#auditList');
    if (!list) return;
    const records = auditVisibleRecords();
    if (!records.length) { list.innerHTML = '<div class="muted">Nenhum item neste filtro.</div>'; return; }
    list.innerHTML = records.map(r => {
      const decided = r.human_decision ? String(r.human_decision.decision) : '';
      const decisionBadge = decided ? `<span class="audit-decided" data-decision="${escapeAttr(decided)}">decisão: ${escapeHtml(decided)}</span>` : '';
      const buttons = AUDIT_DECISIONS.map(([value, label]) =>
        `<button type="button" class="btn-ghost audit-decide${decided === value ? ' selected' : ''}" data-audit-decision="${escapeAttr(value)}" data-region="${escapeAttr(r.region_id)}">${label}</button>`).join('');
      const remove = r.human_decision ? `<button type="button" class="btn-ghost" data-audit-remove="${escapeAttr(r.human_decision.audit_decision_id)}">Remover decisão</button>` : '';
      return `<article class="audit-item" data-region="${escapeAttr(r.region_id)}" data-page="${escapeAttr(r.page_number)}">`
        + `<div class="audit-item-head"><strong>${escapeHtml(r.region_id)}</strong> `
        + `<span class="audit-cls">${escapeHtml(r.classification_original)} → ${escapeHtml(r.classification_normalized)}</span>`
        + `<span class="audit-flags">${r.report_only ? ' report_only' : ''}${r.revision_linked ? ' vinculada' : ''}${r.provider_required ? ' provider' : ''} · cache:${escapeHtml(r.cache_status)}</span>${decisionBadge}</div>`
        + `<div class="audit-texts"><small>fonte</small> ${escapeHtml(r.source_text || '—')}<br><small>tradução</small> ${escapeHtml(r.current_translation || '—')}</div>`
        + `<div class="audit-reasons">ação sugerida: <strong>${escapeHtml(r.suggested_action)}</strong> · reasons: ${escapeHtml((r.reason_codes || []).join(', '))} · confiança: ${escapeHtml(String(r.confidence ?? '—'))}</div>`
        + `<div class="audit-actions"><button type="button" class="btn-ghost" data-audit-open-page="${escapeAttr(r.page_number)}">ABRIR PÁGINA</button>`
        + `<button type="button" class="btn-ghost" data-audit-revise-page="${escapeAttr(r.page_number)}">REVISAR ESTA PÁGINA</button>`
        // Region-scoped review is only offered when the backend policy says the
        // region is reviewable; a preserved class shows a disabled control.
        + `<button type="button" class="btn-ghost" data-audit-revise-region="${escapeAttr(r.region_id)}" data-region-page="${escapeAttr(r.page_number)}"${r.reviewable ? '' : ' disabled aria-disabled="true" title="Região preservada pela política atual"'}>REVISAR ESTA REGIÃO</button>`
        + `${buttons}${remove}</div></article>`;
    }).join('');
  }

  // A preserved region can still have been read wrong. The corrected reading
  // travels with the verdict as audit metadata; it never rewrites the page.
  function auditCorrectedReading(regionId) {
    const field = $(`#auditList [data-corrected-reading="${CSS.escape(String(regionId))}"]`);
    return field ? String(field.value || '').trim() : '';
  }

  async function auditDecide(regionId, decision) {
    const id = pageRevisionIdentity();
    const corrected = auditCorrectedReading(regionId);
    try {
      await api('/api/ui/audit/decision', {method: 'POST', body: JSON.stringify({
        ...id, region_id: regionId, decision,
        notes: corrected ? `leitura_correta: ${corrected}` : ''})});
      await refreshAuditReview();
      auditMessage('Decisão registrada (não altera o PDF nem a revisão histórica).', 'ok');
    } catch (error) { auditMessage(error.message || 'Falha ao registrar decisão.', 'error'); }
  }

  async function auditRemoveDecision(decisionId) {
    const id = pageRevisionIdentity();
    try {
      await api('/api/ui/audit/decision/delete', {method: 'POST', body: JSON.stringify({...id, decision_id: decisionId})});
      await refreshAuditReview();
      auditMessage('Decisão removida.', 'ok');
    } catch (error) { auditMessage(error.message || 'Falha ao remover decisão.', 'error'); }
  }

  async function refreshAuditReview() {
    const id = pageRevisionIdentity();
    const review = await api('/api/ui/audit/review', {method: 'POST', body: JSON.stringify(id)});
    auditState.review = review;
    renderAuditSummary(review);
    // A decision taken from a queue must refresh that queue, not drop the
    // operator back into the flat list.
    if (auditState.mode && auditState.mode !== 'list') await setAuditMode(auditState.mode);
    else renderAuditList();
  }

  function closeLinguisticAudit() {
    const d = $('#linguisticAuditPanel');
    if (d) { if (typeof d.close === 'function' && d.open) d.close(); d.hidden = true; }
    auditSyncUrl(false);
  }

  $('#openLinguisticAudit')?.addEventListener('click', () => openLinguisticAudit());
  $('#linguisticAuditClose')?.addEventListener('click', closeLinguisticAudit);
  // A native <dialog> closes itself on Escape; mirror that into our own state so
  // the panel really hides and the URL stops advertising an open audit. The
  // explicit keydown guarantees Escape works even when the native path does not
  // fire (focus inside a select, embedded browsers).
  $('#linguisticAuditPanel')?.addEventListener('close', () => {
    const d = $('#linguisticAuditPanel');
    if (d) d.hidden = true;
    auditSyncUrl(false);
  });
  $('#linguisticAuditPanel')?.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); closeLinguisticAudit(); }
  });
  $('#pageRevisionPanel')?.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    const d = $('#pageRevisionPanel');
    if (d) { if (typeof d.close === 'function' && d.open) d.close(); d.hidden = true; }
    pageRevisionState.pageRevisionId = null;
    pageRevisionSyncUrl();
  });
  $('#pageRevisionPanel')?.addEventListener('close', () => {
    const d = $('#pageRevisionPanel');
    if (d) d.hidden = true;
    pageRevisionState.pageRevisionId = null;
    pageRevisionSyncUrl();
  });
  $('#auditFilters')?.addEventListener('input', renderAuditList);
  $('#auditFilters')?.addEventListener('change', renderAuditList);
  $('#auditList')?.addEventListener('click', event => {
    const preview = event.target.closest('[data-preview-action]');
    if (preview) {
      if (preview.disabled) return;
      void previewAction(preview.dataset.previewAction, String(preview.dataset.region || ''), preview);
      return;
    }
    const decide = event.target.closest('[data-audit-decision]');
    if (decide) { auditDecide(decide.dataset.region, decide.dataset.auditDecision); return; }
    const remove = event.target.closest('[data-audit-remove]');
    if (remove) { auditRemoveDecision(remove.dataset.auditRemove); return; }
    const reviseRegion = event.target.closest('[data-audit-revise-region]');
    if (reviseRegion) {
      if (reviseRegion.disabled) return;
      closeLinguisticAudit();
      openPageRevision(Number(reviseRegion.dataset.regionPage), {focusRegion: reviseRegion.dataset.auditReviseRegion});
      return;
    }
    const revise = event.target.closest('[data-audit-revise-page]');
    if (revise) { closeLinguisticAudit(); openPageRevision(Number(revise.dataset.auditRevisePage)); return; }
    const openPage = event.target.closest('[data-audit-open-page]');
    if (openPage) { openVisualComparison(Number(openPage.dataset.auditOpenPage)); }
  });
  function restoreAuditFromUrl() {
    try {
      const params = new URLSearchParams(window.location.search || '');
      if (params.get('audit') !== '1') return;
      const mode = String(params.get('audit_mode') || 'list');
      if (AUDIT_MODES.includes(mode)) auditState.mode = mode;
      openLinguisticAudit({restore: true});
    } catch (_) {}
  }

  /* ---------- triage, bulk decisions and provider set (BLOCO 5) ---------- */
  function auditModeSyncUrl() {
    try {
      const url = new URL(window.location.href);
      if (auditState.mode && auditState.mode !== 'list') url.searchParams.set('audit_mode', auditState.mode);
      else url.searchParams.delete('audit_mode');
      window.history.replaceState({}, '', url);
    } catch (_) {}
  }

  const AUDIT_MODES = ['list', 'triage', 'ocr', 'editorial', 'provider', 'previews'];
  // Bulk selection is only meaningful where every row is individually pickable.
  const AUDIT_BULK_MODES = ['triage', 'ocr'];

  async function setAuditMode(mode) {
    auditState.mode = mode;
    $$('[data-audit-mode]').forEach(b => b.classList.toggle('selected', b.dataset.auditMode === mode));
    const bulk = $('#auditBulk');
    if (bulk) bulk.hidden = !AUDIT_BULK_MODES.includes(mode);
    auditModeSyncUrl();
    const id = pageRevisionIdentity();
    try {
      if (mode === 'triage') {
        auditState.triage = await api('/api/ui/audit/triage', {method: 'POST', body: JSON.stringify(id)});
        renderTriageQueue();
      } else if (mode === 'ocr') {
        auditState.ocrCandidates = await api('/api/ui/audit/ocr-invalid-candidates', {method: 'POST', body: JSON.stringify(id)});
        renderOcrCandidates();
      } else if (mode === 'editorial') {
        auditState.editorial = await api('/api/ui/audit/editorial-pending', {method: 'POST', body: JSON.stringify(id)});
        renderEditorialPending();
      } else if (mode === 'provider') {
        auditState.providerSet = await api('/api/ui/audit/provider-set', {method: 'POST', body: JSON.stringify(id)});
        renderProviderSet();
      } else if (mode === 'previews') {
        auditState.previews = await api('/api/ui/human-translation/review', {method: 'POST', body: JSON.stringify(id)});
        renderHumanPreviews();
        void loadPreviewGates();
      } else {
        renderAuditCounters(null);
        renderAuditList();
      }
      auditMessage('');
    } catch (error) { auditMessage(error.message || 'Falha ao carregar.', 'error'); }
  }

  // FASE 12 — three separate totals. Merging them would hide which step is
  // actually blocking the chapter.
  function renderAuditCounters(counters) {
    const box = $('#auditCounters');
    if (!box) return;
    if (!counters) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    const cells = [
      ['OCR DIRECIONADO FUTURO', counters.targeted_ocr_pending, 'ocr'],
      ['DECISÃO EDITORIAL PENDENTE', counters.awaiting_editorial_decision, 'editorial'],
      ['PRONTO PARA REVISÃO COM IA', counters.ready_for_ai_review, 'ready'],
    ].map(([label, value, kind]) =>
      `<span class="audit-counter" data-counter="${escapeAttr(kind)}">`
      + `<strong>${Number(value || 0)}</strong> ${escapeHtml(label)}</span>`).join('');
    box.innerHTML = cells + (counters.authorization_blocked
      ? '<span class="audit-counter" data-counter="blocked">autorização bloqueada</span>' : '');
  }

  function auditTextsBlock(item) {
    return `<div class="audit-texts"><small>fonte</small> ${escapeHtml(item.source_text || '—')}`
      + `<br><small>tradução</small> ${escapeHtml(item.current_translation || '—')}</div>`;
  }

  // The region's own pixels. A transcription can only be judged against them.
  // The <img> is filled in by loadAuditCrops: a plain src cannot carry the
  // authorization header, so the bytes are fetched through the same helper as
  // every other authenticated call.
  function auditCropBlock(item) {
    if (!item.bounding_box) return '<div class="audit-crop-missing">recorte indisponível: região sem geometria</div>';
    return `<figure class="audit-crop"><img data-crop-region="${escapeAttr(item.region_id)}" alt=""`
      + ` aria-label="Recorte da região ${escapeAttr(item.region_id)} na página ${escapeAttr(item.page_number)}">`
      + `<figcaption>página ${escapeHtml(item.page_number)} · caixa ${escapeHtml((item.bounding_box || []).join(', '))}</figcaption></figure>`;
  }

  const auditCropUrls = new Map();

  async function loadAuditCrops() {
    const id = pageRevisionIdentity();
    if (!id.job_id) return;
    for (const img of $$('#auditList img[data-crop-region]')) {
      const region = String(img.dataset.cropRegion || '');
      if (!region || img.dataset.cropLoaded === '1') continue;
      img.dataset.cropLoaded = '1';
      try {
        const response = await api('/api/ui/audit/region-crop?' + new URLSearchParams({
          job_id: id.job_id, run_id: id.run_id || '', region_id: region}), {rawResponse: true});
        const url = URL.createObjectURL(await response.blob());
        const previous = auditCropUrls.get(region);
        if (previous) URL.revokeObjectURL(previous);
        auditCropUrls.set(region, url);
        img.src = url;
      } catch (error) {
        img.replaceWith(Object.assign(document.createElement('div'), {
          className: 'audit-crop-missing',
          textContent: `recorte indisponível: ${error.message || 'falha ao carregar'}`,
        }));
      }
    }
  }

  // Every label the panel offers, including the reclassifications. Kept apart
  // from AUDIT_DECISIONS so the flat list keeps its five original buttons.
  const AUDIT_DECISION_LABELS = Object.assign(
    Object.fromEntries(AUDIT_DECISIONS),
    {needs_review: 'EXIGIR REVISÃO HUMANA',
     classify_credit: 'CLASSIFICAR COMO CRÉDITO',
     classify_title_name: 'CLASSIFICAR COMO TÍTULO/NOME',
     classify_editorial: 'CLASSIFICAR COMO TEXTO EDITORIAL',
     classify_sfx: 'CLASSIFICAR COMO SFX',
     classify_watermark: 'CLASSIFICAR COMO WATERMARK/URL'});

  function auditDecisionButtons(region, decided, values, overrides = {}) {
    return values.map(value => {
      const label = overrides[value] || AUDIT_DECISION_LABELS[value] || value;
      return `<button type="button" class="btn-ghost audit-decide${decided === value ? ' selected' : ''}"`
        + ` data-audit-decision="${escapeAttr(value)}" data-region="${escapeAttr(region)}">${escapeHtml(label)}</button>`;
    }).join('');
  }

  function auditRemoveButton(item) {
    if (!item.human_decision_id) return '';
    return `<button type="button" class="btn-ghost" data-audit-remove="${escapeAttr(item.human_decision_id)}">REMOVER DECISÃO</button>`;
  }

  // What actually happens to this region once it is ruled on. Derived from the
  // backend's own booleans, never from the text.
  function auditImpact(item) {
    if (item.human_decision === 'ocr_invalid') return 'sai da fila de tradução, entra em OCR direcionado futuro';
    if (item.translatable && item.provider_required) return 'se TRADUZIR: entra no conjunto do provider (1 request estimada)';
    if (item.translatable) return 'traduzível, já respondido pelo cache: sem request nova';
    if (item.preservable) return 'preservada: nenhuma request, texto original mantido';
    return 'sem política definida: permanece fora do conjunto do provider';
  }

  // FASE 6 — proposed candidates only. Nothing here is marked without a human.
  function renderOcrCandidates() {
    const list = $('#auditList');
    const data = auditState.ocrCandidates;
    if (!list || !data) return;
    renderAuditCounters(data.editorial_counters);
    const card = (item, group) => {
      const a = item.ocr_assessment || {};
      const evidence = (a.strong_evidence || []).concat(a.weak_evidence || []);
      // Only unambiguous evidence may be acted on in bulk; an ambiguous read
      // has to be judged one region at a time.
      const pickable = group === 'auto';
      const checked = auditState.selection.has(String(item.region_id)) ? ' checked' : '';
      const pick = pickable
        ? `<label class="triage-pick"><input type="checkbox" data-triage-select="${escapeAttr(item.region_id)}"${checked}> `
        : '<label class="triage-pick">';
      return `<article class="audit-item" data-region="${escapeAttr(item.region_id)}" data-page="${escapeAttr(item.page_number)}" data-ocr-group="${escapeAttr(group)}">`
        + `<div class="audit-item-head">${pick}<strong>${escapeHtml(item.region_id)}</strong></label> `
        + `<span class="audit-cls">${escapeHtml(item.classification_original)} → ${escapeHtml(item.classification_normalized)}</span>`
        + `<span class="triage-gate" data-gate="${escapeAttr(a.status || '')}">${escapeHtml(a.status || '—')}</span>`
        + `<span class="audit-flags">OCR ${escapeHtml(String(item.confidence ?? '—'))}`
        + ` · cache:${escapeHtml(item.cache_status)}${item.cache_correction_available ? ' (correção)' : ''}`
        + ` · provider:${item.provider_required ? 'sim' : 'não'}</span>`
        + (item.human_decision ? `<span class="audit-decided">decisão: ${escapeHtml(item.human_decision)}</span>` : '')
        + `</div><div class="audit-body">${auditCropBlock(item)}<div class="audit-body-text">`
        + auditTextsBlock(item)
        + `<div class="audit-reasons">evidência contra a leitura: ${escapeHtml(evidence.join(', ') || 'nenhuma')}`
        + `<br>evidência protetora: ${escapeHtml((a.positive_evidence || []).join(', ') || 'nenhuma')}`
        + `<br>confiança do detector: ${escapeHtml(String(a.confidence ?? '—'))}`
        + ` · vogais ${escapeHtml(String(a.vowel_ratio ?? '—'))} · alfabético ${escapeHtml(String(a.alphabetic_ratio ?? '—'))}`
        + `<br>impacto: ${escapeHtml(auditImpact(item))}</div></div></div>`
        + `<div class="audit-corrected"><label>leitura correta (opcional, vai para os metadados)`
        + ` <input type="text" data-corrected-reading="${escapeAttr(item.region_id)}"`
        + ` placeholder="o que a imagem realmente diz"></label></div>`
        + `<div class="audit-actions">`
        + auditDecisionButtons(item.region_id, item.human_decision,
            ['ocr_invalid', 'translate', 'needs_review', 'preserve',
             'classify_sfx', 'classify_watermark'],
            {ocr_invalid: 'CONFIRMAR OCR INVÁLIDO', translate: 'MANTER COMO TEXTO',
             preserve: 'MARCAR COMO PRESERVÁVEL'})
        + auditRemoveButton(item)
        + `<button type="button" class="btn-ghost" data-audit-revise-region="${escapeAttr(item.region_id)}" data-region-page="${escapeAttr(item.page_number)}">REVISAR ESTA REGIÃO</button>`
        + `</div></article>`;
    };
    const section = (title, note, items, group) => !items.length ? '' :
      `<div class="audit-group"><h4>${escapeHtml(title)} <span class="muted">${items.length}</span></h4>`
      + `<p class="muted">${escapeHtml(note)}</p>${items.map(i => card(i, group)).join('')}</div>`;
    list.innerHTML = section('EVIDÊNCIA INEQUÍVOCA', 'Podem ser confirmadas em massa. A confirmação continua sendo sua.',
        data.auto_markable || [], 'auto')
      + section('EXIGEM ANÁLISE INDIVIDUAL', 'A leitura é duvidosa, mas pode ser um nome, uma sigla ou um termo legítimo.',
        data.review_required || [], 'review')
      + section('JÁ CONFIRMADAS', 'Retiradas da fila de tradução, aguardando OCR direcionado futuro.',
        data.confirmed || [], 'confirmed')
      || '<div class="muted">Nenhuma leitura suspeita.</div>';
    updateBulkCount();
    void loadAuditCrops();
  }

  // FASE 9 — the questions a human has to answer before any provider call.
  function renderEditorialPending() {
    const list = $('#auditList');
    const data = auditState.editorial;
    if (!list || !data) return;
    renderAuditCounters(data.editorial_counters);
    const rows = (data.items || []).map(item => {
      const a = item.ocr_assessment || {};
      const gate = (item.linguistic_gate || {}).status || '—';
      return `<article class="audit-item" data-region="${escapeAttr(item.region_id)}" data-page="${escapeAttr(item.page_number)}">`
        + `<div class="audit-item-head"><strong>${escapeHtml(item.region_id)}</strong> `
        + `<span class="audit-cls">${escapeHtml(item.classification_original)} → ${escapeHtml(item.classification_normalized)}</span>`
        + `<span class="audit-flags">papel:${escapeHtml(item.semantic_role || '—')}`
        + ` · cache:${escapeHtml(item.cache_status)}${item.cache_correction_available ? ' (correção)' : ''}`
        + ` · provider:${item.provider_required ? 'sim' : 'não'}</span>`
        + `<span class="triage-gate" data-gate="${escapeAttr(gate)}">gate ${escapeHtml(gate)}</span>`
        + (item.human_decision ? `<span class="audit-decided">decisão: ${escapeHtml(item.human_decision)}</span>` : '')
        + `</div><div class="audit-body">${auditCropBlock(item)}<div class="audit-body-text">`
        + auditTextsBlock(item)
        + `<div class="audit-context"><small>antes</small> ${escapeHtml(item.context_before || '—')}`
        + `<br><small>depois</small> ${escapeHtml(item.context_after || '—')}</div>`
        + `<div class="audit-reasons">pendente porque: ${escapeHtml((item.editorial_reasons || []).join(', '))}`
        + `<br>leitura: ${escapeHtml(a.status || '—')} · evidência: `
        + escapeHtml(((a.strong_evidence || []).concat(a.weak_evidence || [])).join(', ') || 'nenhuma')
        + `<br>gate: ${escapeHtml(((item.linguistic_gate || {}).reason_codes || []).join(', ') || 'sem alertas')}`
        + `<br>recomendação da taxonomia: <strong>${escapeHtml(item.suggested_action || '—')}</strong>`
        + ` (motivos: ${escapeHtml((item.reason_codes || []).join(', ') || 'nenhum')})`
        + `<br>impacto: ${escapeHtml(auditImpact(item))}</div></div></div>`
        + `<div class="audit-corrected"><label>leitura correta (opcional, vai para os metadados)`
        + ` <input type="text" data-corrected-reading="${escapeAttr(item.region_id)}"`
        + ` placeholder="o que a imagem realmente diz"></label></div>`
        + `<div class="audit-actions">`
        + auditDecisionButtons(item.region_id, item.human_decision,
            ['translate', 'preserve', 'needs_review', 'classify_credit',
             'classify_title_name', 'classify_editorial', 'classify_sfx',
             'classify_watermark'],
            {translate: 'TRADUZIR', preserve: 'PRESERVAR', needs_review: 'MANTER PENDENTE'})
        + auditRemoveButton(item)
        + `<button type="button" class="btn-ghost" data-audit-revise-region="${escapeAttr(item.region_id)}" data-region-page="${escapeAttr(item.page_number)}">REVISAR ESTA REGIÃO</button>`
        + `<button type="button" class="btn-ghost" data-audit-open-page="${escapeAttr(item.page_number)}">ABRIR PÁGINA</button>`
        + `</div></article>`;
    }).join('');
    list.innerHTML = `<div class="pr-summary"><strong>${Number(data.item_count || 0)}</strong> região(ões) aguardando decisão editorial`
      + ` em ${Number(data.page_count || 0)} página(s). Enquanto houver pendências, a autorização fica bloqueada.</div>`
      + (rows || '<div class="muted">Nenhuma decisão editorial pendente.</div>');
    void loadAuditCrops();
  }

  function renderTriageQueue() {
    const list = $('#auditList');
    const data = auditState.triage;
    if (!list || !data) return;
    renderAuditCounters(null);
    const counters = Object.entries(data.counters || {}).filter(([k]) => !k.startsWith('class_'))
      .map(([k, v]) => `${escapeHtml(k)} ${Number(v)}`).join(' · ');
    const rows = (data.queue || []).map(item => {
      const gate = (item.linguistic_gate || {}).status || '—';
      const decided = item.human_decision ? String(item.human_decision.decision) : '';
      const checked = auditState.selection.has(String(item.region_id)) ? ' checked' : '';
      return `<article class="audit-item" data-region="${escapeAttr(item.region_id)}" data-page="${escapeAttr(item.page_number)}">`
        + `<div class="audit-item-head"><label class="triage-pick"><input type="checkbox" data-triage-select="${escapeAttr(item.region_id)}"${checked}> `
        + `<strong>${escapeHtml(item.region_id)}</strong></label> `
        + `<span class="audit-cls">${escapeHtml(item.classification_original)} → ${escapeHtml(item.classification_normalized)}</span>`
        + `<span class="triage-gate" data-gate="${escapeAttr(gate)}">gate ${escapeHtml(gate)}</span>`
        + `<span class="triage-score">prioridade ${Number(item.triage_score)}</span>`
        + (decided ? `<span class="audit-decided">decisão: ${escapeHtml(decided)}</span>` : '') + `</div>`
        + `<div class="audit-texts"><small>fonte</small> ${escapeHtml(item.source_text || '—')}<br><small>tradução</small> ${escapeHtml(item.current_translation || '—')}</div>`
        + `<div class="audit-reasons">motivo da prioridade: ${escapeHtml((item.triage_reasons || []).join(', '))}`
        + ` · gate: ${escapeHtml(((item.linguistic_gate || {}).reason_codes || []).join(', ') || 'sem alertas')}</div>`
        + `<div class="audit-actions"><button type="button" class="btn-ghost" data-audit-revise-region="${escapeAttr(item.region_id)}" data-region-page="${escapeAttr(item.page_number)}"${item.reviewable ? '' : ' disabled aria-disabled="true" title="Região preservada pela política atual"'}>REVISAR ESTA REGIÃO</button></div></article>`;
    }).join('');
    list.innerHTML = `<div class="pr-summary">${Number(data.total || 0)} na fila · ${escapeHtml(counters)}</div>`
      + (rows || '<div class="muted">Fila vazia.</div>');
    updateBulkCount();
  }

  function renderProviderSet() {
    const list = $('#auditList');
    const data = auditState.providerSet;
    if (!list || !data) return;
    renderAuditCounters(data.editorial_counters);
    const blocked = Number(data.awaiting_editorial_count || 0);
    const rows = (data.items || []).map(item =>
      `<article class="audit-item" data-region="${escapeAttr(item.region_id)}">`
      + `<div class="audit-item-head"><strong>${escapeHtml(item.region_id)}</strong>`
      + `<span class="audit-cls">${escapeHtml(item.classification_normalized)}</span>`
      + `<span class="triage-gate" data-gate="${escapeAttr(item.risk)}">risco ${escapeHtml(item.risk)}</span></div>`
      + `<div class="audit-texts"><small>fonte</small> ${escapeHtml(item.source_text || '—')}<br><small>tradução</small> ${escapeHtml(item.current_translation || '—')}</div></article>`).join('');
    const excluded = (data.excluded || []).reduce((acc, e) => {
      acc[e.excluded_reason] = (acc[e.excluded_reason] || 0) + 1; return acc;
    }, {});
    const excludedText = Object.entries(excluded).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(' · ');
    // Regions held back for a human ruling are shown, never billed.
    const held = (data.awaiting_editorial || []).map(item =>
      `<article class="audit-item" data-region="${escapeAttr(item.region_id)}" data-held="1">`
      + `<div class="audit-item-head"><strong>${escapeHtml(item.region_id)}</strong>`
      + `<span class="audit-cls">${escapeHtml(item.classification_normalized)}</span>`
      + `<span class="triage-gate" data-gate="${escapeAttr(item.ocr_status || '')}">${escapeHtml(item.ocr_status || '')}</span></div>`
      + auditTextsBlock(item)
      + `<div class="audit-reasons">retida porque: ${escapeHtml((item.editorial_reasons || []).join(', '))}</div></article>`).join('');
    const auth = data.authorization || {};
    const AUTH_TEXT = {
      ready_for_human_authorization: 'Pronto para autorização humana. Cria apenas um pedido pendente; nenhuma chamada externa é feita agora.',
      blocked_pending_editorial_decisions: 'Bloqueado: há regiões aguardando decisão editorial.',
      blocked_by_ocr_review: 'Bloqueado: há leituras de OCR ainda não resolvidas.',
    };
    const authText = AUTH_TEXT[auth.status] || 'Estado da autorização indisponível.';
    list.innerHTML = `<div class="pr-summary"><strong>${Number(data.estimated_requests || 0)}</strong> regiões exigiriam a IA`
      + ` · ${Number(data.page_count || 0)} páginas · excluídas: ${escapeHtml(excludedText || 'nenhuma')}`
      + (blocked ? ` · <strong>${blocked}</strong> retida(s) aguardando decisão editorial` : '') + `</div>`
      + `<div class="audit-counter" data-counter="${escapeAttr(auth.status === 'ready_for_human_authorization' ? 'ready' : 'blocked')}">`
      + `autorização: ${escapeHtml(auth.status || '—')}</div>`
      + `<div class="provider-cta"><button type="button" class="btn-primary" id="requestProviderAuth"`
      + (blocked ? ' disabled aria-disabled="true" title="Decida as regiões pendentes antes de autorizar"' : '')
      + `>SOLICITAR AUTORIZAÇÃO PARA REVISÃO COM IA</button>`
      + `<span class="muted">${escapeHtml(authText)} provider_executed: ${String(auth.provider_executed === true)}</span></div>`
      + (rows || '<div class="muted">Nenhuma região exige a IA.</div>')
      + (held ? `<div class="audit-group"><h4>RETIDAS PARA DECISÃO EDITORIAL <span class="muted">${blocked}</span></h4>${held}</div>` : '');
  }

  // FASE 13 — every state a human needs to judge a preview, side by side.
  function renderHumanPreviews() {
    const list = $('#auditList');
    const data = auditState.previews;
    if (!list || !data) return;
    renderAuditCounters(null);
    const rows = (data.items || []).map(item => {
      const region = String(item.region_id || '');
      const human = item.human_candidate || '';
      const decided = !!item.human_decision;
      const pending = pendingPreviewItems().find(candidate =>
        String(candidate.region_id || '') === region &&
        String(candidate.job_id || '').toLowerCase() === String(appState.qualityReview?.job_id || '').toLowerCase());
      const blocked = pending?.blocked === true || pending?.approval_enabled === false;
      const ready = pending?.approval_enabled === true && pending?.blocked !== true;
      const previewStatus = pending ? (ready ? 'PRONTA PARA REVISÃO HUMANA' : 'BLOQUEADA — REQUER RECONSTRUÇÃO DE ARTE') : '';
      const fontButton = `<button type="button" class="btn-ghost" data-preview-action="font-options" data-region="${escapeAttr(region)}"${decided ? '' : ' disabled aria-disabled="true" title="Aprove um texto antes de escolher tipografia"'}>ESCOLHER TIPOGRAFIA</button>`;
      const refineButton = `<button type="button" class="btn-ghost" data-preview-action="refine-ptbr" data-region="${escapeAttr(region)}">REFINAR PT-BR</button>`;
      const actionButtons = blocked
        ? `<button type="button" class="btn-ghost" data-preview-action="compare" data-region="${escapeAttr(region)}">VER DETALHES</button>`
          + refineButton + fontButton
          + `<button type="button" class="btn-ghost" data-preview-action="mask-editor" data-region="${escapeAttr(region)}">REFINAR MÁSCARA</button>`
        : `<button type="button" class="btn-ghost" data-preview-action="approve" data-region="${escapeAttr(region)}">APROVAR TEXTO PARA PRÉVIA</button>`
          + `<button type="button" class="btn-ghost" data-preview-action="edit" data-region="${escapeAttr(region)}">EDITAR TRADUÇÃO HUMANA</button>`
          + refineButton + fontButton
          + `<button type="button" class="btn-ghost" data-preview-action="render" data-region="${escapeAttr(region)}"${decided ? '' : ' disabled aria-disabled="true" title="Aprove um texto antes de renderizar"'}>RENDERIZAR NOVA TENTATIVA</button>`
          + `<button type="button" class="btn-primary" data-preview-action="compare" data-region="${escapeAttr(region)}">${ready ? 'ABRIR PRÉVIA DA PÁGINA' : 'ABRIR COMPARAÇÃO'}</button>`
          + `<button type="button" class="btn-ghost" data-preview-action="mask-editor" data-region="${escapeAttr(region)}">REFINAR MÁSCARA</button>`
          + `<button type="button" class="btn-ghost" data-preview-action="residual" data-region="${escapeAttr(region)}">VER TINTA RESIDUAL</button>`
          + `<button type="button" class="btn-ghost" data-preview-action="reject" data-region="${escapeAttr(region)}"${decided ? '' : ' disabled aria-disabled="true"'}>PEDIR AJUSTE / REJEITAR PRÉVIA</button>`
          + `<button type="button" class="btn-ghost" data-preview-action="discard" data-region="${escapeAttr(region)}"${decided ? '' : ' disabled aria-disabled="true"'}>DESCARTAR RASCUNHO</button>`;
      return `<article class="audit-item preview-item" data-region="${escapeAttr(region)}" data-page="${escapeAttr(item.page_number)}">`
        + `<div class="audit-item-head"><strong>${escapeHtml(region)}</strong>`
        + `<span class="audit-cls">${escapeHtml(item.classification_normalized || '')}</span>`
        + `<span class="audit-flags">página ${escapeHtml(item.page_number)}</span>`
        + (previewStatus ? `<span class="triage-gate" data-gate="${ready ? 'passed' : 'failed'}">${escapeHtml(previewStatus)}</span>` : '')
        + `<span class="triage-gate" data-gate="" data-preview-visual="${escapeAttr(region)}">gate visual …</span>`
        + `<span class="triage-gate" data-gate="" data-preview-linguistic="${escapeAttr(region)}">gate linguístico …</span>`
        + `</div>`
        + `<dl class="preview-states">`
        + `<dt>SOURCE OCR</dt><dd>${escapeHtml(item.ocr_source_text || '—')}</dd>`
        + `<dt>SOURCE CORRIGIDO</dt><dd>${escapeHtml(item.sent_text || '—')}`
        + `<small> (${escapeHtml(item.text_origin || '')})</small></dd>`
        + `<dt>TRADUÇÃO ATUAL</dt><dd>${escapeHtml(item.current_translation || '—')}</dd>`
        + `<dt>RESPOSTA DO PROVIDER</dt><dd>${escapeHtml(item.provider_candidate || '—')}</dd>`
        + `<dt>DECISÃO HUMANA</dt><dd>${escapeHtml(human || '— ainda não decidida —')}</dd>`
        + `<dt>MÁSCARAS</dt><dd data-preview-mask-summary="${escapeAttr(region)}">aguardando gate visual</dd>`
        + `<dt>FONTE</dt><dd data-preview-font-summary="${escapeAttr(region)}">aguardando perfil visual</dd>`
        + `</dl>`
        + `<div class="preview-compare">`
        + `<figure><figcaption>ATUAL</figcaption>`
        + `<img data-preview-crop="${escapeAttr(region)}" data-preview-kind="base" alt=""`
        + ` aria-label="Região ${escapeAttr(region)} como está hoje"></figure>`
        + `<figure><figcaption>PRÉVIA</figcaption>`
        + `<img data-preview-crop="${escapeAttr(region)}" data-preview-kind="draft" alt=""`
        + ` aria-label="Região ${escapeAttr(region)} na prévia"></figure>`
        + `</div>`
        + `<div class="preview-reasons" data-preview-reasons="${escapeAttr(region)}"></div>`
        + (pending ? `<div class="preview-approval-note">${ready ? 'A prévia isolada está pronta para comparação. Aprovar a página, pedir ajuste, rejeitar ou descartar exige clique humano; nada é aplicado automaticamente.' : 'Este rascunho não passou pelos critérios seguros. Não há botão de aplicar ou nova renderização aqui; requer reconstrução de arte.'}</div>` : '')
        + `<div class="audit-corrected"><label>tradução humana`
        + ` <input type="text" data-human-candidate="${escapeAttr(region)}"`
        + ` value="${escapeAttr(human)}" placeholder="escreva a linha aprovada"${blocked ? ' disabled aria-disabled="true"' : ''}></label></div>`
        + `<div class="audit-actions">${actionButtons}</div></article>`;
    }).join('');
    list.innerHTML = `<div class="pr-summary">${Number(data.item_count || 0)} região(ões) do pedido`
      + ` <strong>${escapeHtml(String(data.authorization_request_id || '').slice(0, 8))}</strong>`
      + ` · modelo ${escapeHtml(data.provider_model || '—')} · ${Number(data.api_requests || 0)} request(s) já feita(s)`
      + ` · nenhuma nova chamada é feita nesta tela.</div>`
      + (rows || '<div class="muted">Nenhuma execução de provider para revisar.</div>');
    bindPreviewActionButtons(list);
    void loadPreviewCrops();
  }

  const previewCropUrls = new Map();

  function bindPreviewActionButtons(root = $('#auditList')) {
    if (!root) return;
    $$('[data-preview-action]', root).forEach(button => {
      if (button.dataset.previewBound === '1') return;
      button.dataset.previewBound = '1';
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        const target = event.currentTarget;
        if (target.disabled) return;
        void previewAction(target.dataset.previewAction, String(target.dataset.region || ''), target);
      });
    });
  }

  async function loadPreviewCrops() {
    const id = pageRevisionIdentity();
    if (!id.job_id) return;
    for (const img of $$('#auditList img[data-preview-crop]')) {
      if (img.dataset.cropLoaded === '1') continue;
      img.dataset.cropLoaded = '1';
      const region = String(img.dataset.previewCrop || '');
      const kind = String(img.dataset.previewKind || 'draft');
      try {
        const response = await api('/api/ui/human-translation/preview-crop?' + new URLSearchParams({
          job_id: id.job_id, run_id: id.run_id || '', region_id: region, kind}), {rawResponse: true});
        const url = URL.createObjectURL(await response.blob());
        const key = `${region}:${kind}`;
        if (previewCropUrls.get(key)) URL.revokeObjectURL(previewCropUrls.get(key));
        previewCropUrls.set(key, url);
        img.src = url;
      } catch (error) {
        img.replaceWith(Object.assign(document.createElement('div'), {
          className: 'audit-crop-missing',
          textContent: kind === 'draft' ? `prévia indisponível: ${error.message || 'não renderizada'}`
                                        : `imagem indisponível: ${error.message || ''}`,
        }));
      }
    }
  }

  // Gates are fetched per region: a measured verdict, never inferred from the
  // presence of a file.
  async function loadPreviewGates() {
    const id = pageRevisionIdentity();
    if (!id.job_id) return;
    for (const item of (auditState.previews?.items || [])) {
      const region = String(item.region_id || '');
      if (!item.human_candidate) continue;
      try {
        const gates = await api('/api/ui/human-translation/gates', {method: 'POST',
          body: JSON.stringify({...id, region_id: region})});
        const visual = $(`#auditList [data-preview-visual="${CSS.escape(region)}"]`);
        const ling = $(`#auditList [data-preview-linguistic="${CSS.escape(region)}"]`);
        if (visual) {
          visual.textContent = `gate visual ${gates.visual_gate.status}`;
          visual.dataset.gate = gates.visual_gate.status === 'passed' ? 'passed'
            : (gates.visual_gate.status === 'failed' ? 'failed' : 'needs_review');
        }
        if (ling) {
          ling.textContent = `gate linguístico ${gates.linguistic_gate.status}`;
          ling.dataset.gate = gates.linguistic_gate.status;
        }
        const box = $(`#auditList [data-preview-reasons="${CSS.escape(region)}"]`);
        if (box) {
          const iso = gates.visual_gate.isolation || {};
          box.innerHTML = `<div class="audit-reasons">reason codes: `
            + escapeHtml((gates.visual_gate.reason_codes || []).join(', ') || 'nenhum')
            + (iso.changed_pixels_total !== undefined && iso.changed_pixels_total !== null
              ? `<br>pixels alterados: ${Number(iso.changed_pixels_total)} · dentro da máscara `
                + `${Number(iso.changed_pixels_inside_mask)} · <strong>fora da máscara `
                + `${Number(iso.changed_pixels_outside_mask)}</strong>` : '')
            + `<br>tinta residual: ${Number(gates.visual_gate.residual_source_pixels || 0)}`
            + ` · componentes ${Number(gates.visual_gate.residual_component_count || 0)}`
            + `<br>estado: ${escapeHtml(gates.state || '')}</div>`;
        }
        const maskSummary = $(`#auditList [data-preview-mask-summary="${CSS.escape(region)}"]`);
        if (maskSummary) {
          const refinement = gates.mask_refinement || {};
          const expansion = refinement.expansion || {};
          const halo = gates.visual_gate.validation_halo || {};
          maskSummary.textContent = `original ${JSON.stringify(refinement.original_box || gates.visual_gate.original_mask_bounds || [])}`
            + ` · expandida ${JSON.stringify(refinement.expanded_box || [])}`
            + ` · halo ${JSON.stringify((halo.boxes || [])[0] || [])}`
            + ` · expansão L${Number(expansion.left || 0)} R${Number(expansion.right || 0)}`
            + ` T${Number(expansion.top || 0)} B${Number(expansion.bottom || 0)}`;
        }
        const fontSummary = $(`#auditList [data-preview-font-summary="${CSS.escape(region)}"]`);
        if (fontSummary) {
          const selection = gates.font_selection || {};
          fontSummary.textContent = `${selection.selected_font || '—'}`
            + ` · score ${Number(selection.font_match_score || 0).toFixed(3)}`
            + ((selection.font_reason_codes || []).length
              ? ` · ${selection.font_reason_codes.join(', ')}` : '');
        }
      } catch (error) { /* a region with no decision yet has no gate */ }
    }
  }

  function bindBoundaryEditor(panel, data, region) {
    if (!panel) return;
    const boundary = data.boundary_review || {};
    const storageKey = `tradutor-boundary-review:${boundary.conflict_artifact_id || region}`;
    let state = {};
    try { state = JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch (_) { /* fail closed */ }
    state.decisions ||= {};
    state.history ||= [];
    state.redo ||= [];
    state.current ||= boundary.first_pending_segment_id || '';
    const persist = () => {
      state.opacity = Number(panel.querySelector('[data-boundary-opacity]')?.value || 50);
      localStorage.setItem(storageKey, JSON.stringify(state));
      const pending = (boundary.segments || []).filter(segment =>
        !state.decisions[segment.segment_id] || state.decisions[segment.segment_id] === 'uncertain').length;
      const status = panel.querySelector('[data-boundary-status]');
      if (status) status.textContent = pending
        ? `${pending} segmento(s) pendente(s). Rascunho restaurável após F5; confirmar continua bloqueado.`
        : 'Todos os segmentos foram classificados. Salve o rascunho antes de confirmar.';
    };
    const select = id => {
      state.current = id;
      panel.querySelectorAll('[data-boundary-segment]').forEach(button =>
        button.classList.toggle('is-current', button.dataset.boundarySegment === id));
      persist();
    };
    panel.querySelectorAll('[data-boundary-segment]').forEach(button =>
      button.addEventListener('click', () => select(button.dataset.boundarySegment)));
    panel.querySelectorAll('[data-boundary-class]').forEach(button =>
      button.addEventListener('click', () => {
        if (!state.current) return;
        state.history.push(JSON.stringify(state.decisions));
        state.redo.length = 0;
        state.decisions[state.current] = button.dataset.boundaryClass;
        persist();
      }));
    panel.querySelectorAll('[data-boundary-tool]').forEach(button =>
      button.addEventListener('click', () => {
        const tool = button.dataset.boundaryTool;
        if (tool === 'undo' && state.history.length) {
          state.redo.push(JSON.stringify(state.decisions));
          state.decisions = JSON.parse(state.history.pop());
        } else if (tool === 'redo' && state.redo.length) {
          state.history.push(JSON.stringify(state.decisions));
          state.decisions = JSON.parse(state.redo.pop());
        } else if (tool === 'restore') {
          state.history.push(JSON.stringify(state.decisions));
          state.decisions = {};
          state.redo.length = 0;
        }
        persist();
      }));
    const opacity = panel.querySelector('[data-boundary-opacity]');
    if (opacity) {
      opacity.value = String(state.opacity ?? 50);
      const apply = () => {
        const value = Number(opacity.value);
        const overlay = panel.querySelector('.boundary-overlay');
        if (overlay) overlay.style.opacity = String(value / 100);
        const output = panel.querySelector('[data-boundary-opacity-value]');
        if (output) output.textContent = `${value}%`;
        persist();
      };
      opacity.addEventListener('input', apply);
      apply();
    }
    select(state.current);
  }

  async function previewAction(action, region, sourceElement = null) {
    const id = pageRevisionIdentity();
    const field = $(`#auditList [data-human-candidate="${CSS.escape(region)}"]`);
    const item = (auditState.previews?.items || []).find(i => String(i.region_id) === region) || {};
    try {
      if (action === 'edit') { field?.focus(); field?.select(); return; }
      if (action === 'refine-ptbr') {
        const box = $(`#auditList [data-preview-reasons="${CSS.escape(region)}"]`);
        const refinementPayload = {
          ...id, revision_id: item.revision_id || '', page_id: item.page_id || String(item.page_number || ''),
          region_id: region, source_text: item.sent_text || item.ocr_source_text || '',
          current_translation: item.current_translation || item.human_candidate || field?.value || '',
          context_before: item.context_before || '', context_after: item.context_after || '',
          region_type: item.region_type || item.classification || '', speaker: item.speaker || '',
          tone: item.tone || '', emotion: item.emotion || '', register: item.register || '',
          visual_character_limit: item.visual_character_limit || 0, glossary: item.glossary || {},
          previous_decision_id: item.human_translation_decision_id || ''
        };
        if (box) box.innerHTML = `<section class="natural-refinement-panel" aria-live="polite"><h4>REFINAR PT-BR</h4>`
          + `<p>Original: <strong>${escapeHtml(item.sent_text || item.ocr_source_text || '—')}</strong></p>`
          + `<p>Tradução atual: <strong>${escapeHtml(item.current_translation || item.human_candidate || '—')}</strong></p>`
          + `<p class="muted">A NVIDIA exige autorização explícita. Uma sugestão nunca substitui uma decisão humana sem confirmação.</p>`
          + `<div data-refinement-result class="muted">Nenhuma sugestão solicitada.</div>`
          + `<div class="audit-actions"><button type="button" class="btn-primary" data-refinement-action="request">GERAR SUGESTÕES</button>`
          + `<button type="button" class="btn-ghost" data-refinement-action="natural">USAR NATURAL</button><button type="button" class="btn-ghost" data-refinement-action="compact">USAR COMPACTA</button>`
          + `<button type="button" class="btn-ghost" data-refinement-action="neutral">USAR NEUTRA</button><button type="button" class="btn-ghost" data-refinement-action="manual">EDITAR MANUALMENTE</button>`
          + `<button type="button" class="btn-ghost" data-refinement-action="keep_current">MANTER ATUAL</button><button type="button" class="btn-ghost" data-refinement-action="request">PEDIR NOVA SUGESTÃO</button>`
          + `<button type="button" class="btn-ghost" data-refinement-action="discard">DESCARTAR</button></div>`
          + `<p class="muted">Warnings, confiança e limite visual aparecerão após uma resposta estruturada válida.</p></section>`;
        let refinement = null;
        const resultBox = box?.querySelector('[data-refinement-result]');
        const showResult = value => {
          refinement = value || null;
          const result = refinement?.result || {};
          const trace = refinement?.provider_trace || {};
          const repairCount = (trace.repair_attempts || []).length;
          if (resultBox) resultBox.innerHTML = refinement
            ? `<p>Status: ${escapeHtml(refinement.status || '—')} · confiança: ${escapeHtml(String(result.confidence ?? '—'))}</p>`
              + `<p>Formato: ${escapeHtml(trace.status || 'validando tradução')} · correções JSON: ${repairCount}</p>`
              + `<p>Natural: ${escapeHtml(result.natural_ptbr || '—')}</p><p>Compacta: ${escapeHtml(result.compact_ptbr || '—')}</p>`
              + `<p>Neutra: ${escapeHtml(result.neutral_ptbr || '—')}</p><p>Warnings: ${escapeHtml((result.warnings || refinement.reason_codes || []).join(', ') || 'nenhum')}</p>`
            : 'Nenhuma sugestão solicitada.';
        };
        const showSelection = selection => {
          if (!selection || !resultBox) return;
          const label = selection.selected_action === 'keep_current' ? 'MANTER ATUAL' : 'USAR NATURAL';
          resultBox.insertAdjacentHTML('beforeend',
            `<p><strong>Decisão humana confirmada:</strong> ${escapeHtml(label)}</p>`
            + `<p>Tradução linguística efetiva: ${escapeHtml(selection.effective_translation_after || '—')}</p>`
            + '<p class="muted">Aplicação visual ainda não realizada.</p>');
        };
        try {
          const restored = await api('/api/ui/human-translation/refinement', {method: 'POST',
            body: JSON.stringify({...refinementPayload, operation: 'restore'})});
          if (restored.restored) showResult(restored.refinement);
          showSelection(restored.selection);
        } catch (_) { /* no persisted suggestion is expected on first use */ }
        box?.querySelectorAll('[data-refinement-action]').forEach(button => button.addEventListener('click', async () => {
          const operation = button.dataset.refinementAction;
          if (operation === 'discard') { box.innerHTML = ''; return; }
          if (operation === 'request') {
            const authorized = await auditConfirm({
              title: 'AUTORIZAR REFINAMENTO LINGUÍSTICO',
              summary: '<p>Solicita três opções contextuais em PT-BR ao provider configurado.</p>',
              effect: 'Nenhuma sugestão substitui uma decisão humana automaticamente.'
            });
            if (!authorized) return;
            if (resultBox) resultBox.textContent = 'Gerando sugestão e validando estrutura...';
            const response = await api('/api/ui/human-translation/refinement', {method: 'POST',
              body: JSON.stringify({...refinementPayload, authorized: true})});
            showResult(response.refinement); return;
          }
          if (!refinement) { auditMessage('Gere ou restaure sugestões antes de escolher.', 'warn'); return; }
          const manualText = operation === 'manual' ? String(field?.value || '').trim() : '';
          if (operation === 'manual' && !manualText) {
            auditMessage('Edite a tradução no campo da região antes de confirmar a opção manual.', 'warn');
            field?.focus(); return;
          }
          const response = await api('/api/ui/human-translation/refinement/decision', {method: 'POST',
            body: JSON.stringify({result: refinement, option: operation, manual_text: manualText,
              authorization: 'delegated_by_user', previous_decision_id: refinementPayload.previous_decision_id})});
          showSelection(response.decision);
          auditMessage(`Decisão linguística registrada: ${response.decision?.status || 'confirmada'}.`, 'ok');
        }));
        return;
      }
      if (action === 'compare') {
        const pending = pendingPreviewItems().find(candidate =>
          String(candidate.region_id || '') === region &&
          String(candidate.job_id || '').toLowerCase() === String(id.job_id || '').toLowerCase());
        if (pending) {
          if (pending.comparison_url) {
            try { window.history.replaceState({}, '', pending.comparison_url); } catch (_) { /* url is advisory */ }
          }
          openHumanPreviewComparison(pending);
        }
        else openVisualComparison(Number(item.page_number));
        return;
      }
      if (action === 'mask') { auditMessage('A máscara original, a expandida e o halo estão no bloco de evidências desta região.', 'ok'); return; }
      if (action === 'mask-editor') {
        const data = await api('/api/ui/human-mask/editor-state', {method: 'POST',
          body: JSON.stringify({...id, region_id: region})});
        const box = $(`#auditList [data-preview-reasons="${CSS.escape(region)}"]`);
        if (box) {
          const assets = data.assets || {};
          const assetImg = (key, label) => assets[key]
            ? `<figure><figcaption>${escapeHtml(label)}</figcaption><img src="/api/ui/human-mask/asset?asset=${encodeURIComponent(assets[key])}" alt="${escapeAttr(label)} da região ${region}"></figure>`
            : `<figure><figcaption>${escapeHtml(label)}</figcaption><div class="visual-comparison-empty">camada indisponível</div></figure>`;
          const auto = data.automatic_segmentation || {};
          const boundary = data.boundary_review || {};
          const segments = (boundary.segments || []).map((segment, index) =>
            `<button type="button" class="btn-ghost boundary-segment${index === 0 ? ' is-current' : ''}" data-boundary-segment="${escapeAttr(segment.segment_id || '')}">SEGMENTO ${index + 1} · ${Number(segment.pixel_count || 0)} px</button>`).join('');
          box.innerHTML = `<section class="hm-refine-panel" aria-live="polite">`
            + `<p>${escapeHtml(data.message || 'Refine a máscara com segurança antes de qualquer reconstrução local.')}</p>`
            + `<dl><dt>estado</dt><dd>${escapeHtml(auto.status || 'blocked_pending_human_mask')}</dd>`
            + `<dt>ratio</dt><dd>${Number(auto.mask_ratio || 0).toFixed(4)}</dd>`
            + `<dt>precisão</dt><dd>${Number(auto.mask_precision || 0).toFixed(4)}</dd>`
            + `<dt>guards</dt><dd>${escapeHtml((auto.reason_codes || []).join(', ') || 'sem bloqueio automático')}</dd></dl>`
            + `<div class="hm-refine-tools" role="toolbar" aria-label="Ferramentas de refinamento de máscara">`
            + `<button type="button" class="btn-ghost" data-mask-tool="include_text">INCLUIR TEXTO</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="exclude_art">EXCLUIR ARTE</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="protect_lines">PROTEGER LINHAS</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="mark_uncertain">MARCAR INCERTO</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="eraser">BORRACHA</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="undo">DESFAZER</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="redo">REFAZER</button>`
            + `<button type="button" class="btn-ghost" data-mask-tool="restore_automatic">RESTAURAR AUTOMÁTICA</button>`
            + `<button type="button" class="btn-ghost" data-preview-action="mask-save" data-region="${escapeAttr(region)}">SALVAR RASCUNHO</button>`
            + `<button type="button" class="btn-primary" data-preview-action="mask-confirm" data-region="${escapeAttr(region)}">CONFIRMAR MÁSCARA</button></div>`
            + `<section class="boundary-editor" data-boundary-editor="${escapeAttr(region)}"><h4>REVISAR TEXTO × ARTE</h4>`
            + `<p><strong>${Number(boundary.conflict_pixel_count || 0)}</strong> pixels em conflito. Sugestões não são decisões.</p>`
            + `<label>OPACIDADE <input type="range" min="0" max="100" value="50" data-boundary-opacity> <output data-boundary-opacity-value>50%</output></label>`
            + `<div class="boundary-stage" tabindex="0" aria-label="Original com camadas de revisão">`
            + (assets.auto_segmentation ? `<img class="boundary-base" src="/api/ui/human-mask/asset?asset=${encodeURIComponent(assets.auto_segmentation)}" alt="Imagem original">` : '')
            + (assets.glyph_art_conflict ? `<img class="boundary-overlay" src="/api/ui/human-mask/asset?asset=${encodeURIComponent(assets.glyph_art_conflict)}" alt="Pixels em conflito">` : '')
            + `</div><div class="boundary-segments" aria-label="Segmentos pendentes">${segments || '<span>Sem segmentos</span>'}</div>`
            + `<div class="hm-refine-tools"><button type="button" class="btn-ghost" data-boundary-class="glyph_outline">MARCAR COMO TEXTO</button>`
            + `<button type="button" class="btn-ghost" data-boundary-class="protected_art">MARCAR COMO ARTE</button>`
            + `<button type="button" class="btn-ghost" data-boundary-class="uncertain">MANTER INCERTO</button>`
            + `<button type="button" class="btn-ghost" data-boundary-tool="undo">DESFAZER</button>`
            + `<button type="button" class="btn-ghost" data-boundary-tool="redo">REFAZER</button>`
            + `<button type="button" class="btn-ghost" data-boundary-tool="restore">RESTAURAR</button></div>`
            + `<p class="muted" data-boundary-status>A confirmação permanece bloqueada enquanto houver incerteza.</p></section>`
            + `<div class="hm-layer-grid">${assetImg('auto_segmentation', 'original/recorte')}`
            + assetImg('text_core_mask', 'texto core') + assetImg('outline_mask', 'contorno')
            + assetImg('antialias_mask', 'antialias') + assetImg('combined_text_mask', 'máscara combinada')
            + assetImg('validation_halo', 'halo de validação')
            + assetImg('protected_edge_mask', 'arte protegida') + assetImg('background_art_mask', 'fundo/arte') + `</div>`
            + `<p class="muted">Sem confirmação humana, o inpainting permanece bloqueado: ${escapeHtml(data.inpainting_status || 'blocked_pending_human_mask')}.</p>`
            + `</section>`;
          bindPreviewActionButtons(box);
          bindBoundaryEditor(box.querySelector('[data-boundary-editor]'), data, region);
        }
        return;
      }
      if (action === 'mask-save' || action === 'mask-confirm') {
        auditMessage(action === 'mask-confirm'
          ? 'Confirmação real da máscara exige edição humana na ferramenta; nenhum inpainting foi iniciado.'
          : 'Rascunho visual preparado. Nenhum inpainting ou página definitiva foi alterado.', 'warn');
        return;
      }
      if (action === 'residual') { auditMessage('A contagem de tinta residual está no gate visual medido desta região.', 'ok'); return; }
      if (action === 'font-options') {
        const data = await api('/api/ui/human-translation/font-candidates', {method: 'POST',
          body: JSON.stringify({...id, region_id: region})});
        const box = $(`#auditList [data-preview-reasons="${CSS.escape(region)}"]`);
        if (box) {
          const selected = data.selected_choice || {};
          const cards = (data.candidates || []).map(candidate => {
            const chosen = selected.candidate_id && String(selected.candidate_id) === String(candidate.candidate_id);
            return `<article class="font-choice-card${chosen ? ' is-selected' : ''}">`
              + `<strong>${escapeHtml(candidate.option_label || 'OPÇÃO')}</strong>`
              + `<img src="${escapeAttr(candidate.preview_asset || '')}" alt="Comparação tipográfica ${escapeAttr(candidate.option_label || '')}">`
              + `<dl><dt>Fonte real</dt><dd>${escapeHtml(candidate.actual_font || '—')}</dd>`
              + `<dt>fallback</dt><dd>${candidate.fallback_used ? 'sim' : 'não'}</dd>`
              + `<dt>glyphs</dt><dd>${escapeHtml(candidate.glyph_support?.status || '—')}</dd>`
              + `<dt>score</dt><dd>${Number(candidate.overall_score || 0).toFixed(3)}</dd>`
              + `<dt>estilo</dt><dd>${Number(candidate.style_score || 0).toFixed(3)}</dd>`
              + `<dt>encaixe</dt><dd>${Number(candidate.fit_score || 0).toFixed(3)}</dd></dl>`
              + `<button type="button" class="btn-primary" data-preview-action="font-choose" data-region="${escapeAttr(region)}" data-candidate-id="${escapeAttr(candidate.candidate_id || '')}">ESCOLHER ESTA TIPOGRAFIA</button>`
              + `</article>`;
          }).join('');
          box.innerHTML = `<section class="font-choice-panel" aria-live="polite">`
            + `<p>${escapeHtml(data.message || 'Escolher cria uma nova tentativa de prévia; nada é aplicado automaticamente.')}</p>`
            + `<div class="font-choice-grid">${cards || '<div class="visual-comparison-empty">Nenhuma fonte compatível encontrada.</div>'}</div>`
            + `<div class="audit-actions"><button type="button" class="btn-ghost" data-preview-action="compare" data-region="${escapeAttr(region)}">ABRIR AMPLIADO</button>`
            + `<button type="button" class="btn-ghost" data-preview-action="font-options" data-region="${escapeAttr(region)}">PEDIR OUTRAS OPÇÕES</button>`
            + `<button type="button" class="btn-ghost" data-preview-action="compare" data-region="${escapeAttr(region)}">MANTER PENDENTE</button></div></section>`;
          bindPreviewActionButtons(box);
        }
        return;
      }
      if (action === 'font-choose') {
        const candidateId = String(sourceElement?.dataset?.candidateId || '');
        if (!candidateId) { auditMessage('Candidato de fonte indisponível.', 'error'); return; }
        const result = await api('/api/ui/human-translation/font-choice', {method: 'POST',
          body: JSON.stringify({...id, region_id: region, candidate_id: candidateId})});
        auditMessage(`Tipografia escolhida (${String(result.font_choice?.font_choice_decision_id || '').slice(0, 8)}). Renderize uma nova tentativa quando quiser; nada foi aplicado automaticamente.`, 'ok');
        await previewAction('font-options', region);
        return;
      }
      if (action === 'approve') {
        const text = String(field?.value || '').trim();
        if (!text) { auditMessage('Escreva a tradução humana antes de aprovar.', 'warn'); return; }
        await api('/api/ui/human-translation/record', {method: 'POST',
          body: JSON.stringify({...id, region_id: region, human_candidate: text})});
        auditMessage('Texto aprovado para prévia. Nenhum PDF ou página foi alterado.', 'ok');
      } else if (action === 'render') {
        const ok = await auditConfirm({
          title: `RENDERIZAR PRÉVIA — ${region}`,
          summary: `<p>Cria um rascunho isolado da página, alterando somente esta região.</p>`,
          effect: 'Nenhum PDF, nenhuma página final e nenhuma publicação são alterados. '
            + 'Nenhuma chamada ao provider é feita.',
        });
        if (!ok) { auditMessage('Operação cancelada.', 'warn'); return; }
        let fontChoiceId = '';
        try {
          const fontData = await api('/api/ui/human-translation/font-candidates', {method: 'POST',
            body: JSON.stringify({...id, region_id: region})});
          fontChoiceId = String(fontData.selected_choice?.font_choice_decision_id || '');
        } catch (_) { /* draft can still use the generic font selection */ }
        const result = await api('/api/ui/human-translation/draft', {method: 'POST',
          body: JSON.stringify({...id, region_id: region, font_choice_decision_id: fontChoiceId})});
        auditMessage(`Rascunho ${String(result.manifest?.page_revision_id || '').slice(0, 8)} criado `
          + '(aguardando sua aprovação visual).', 'ok');
      } else if (action === 'reject' || action === 'discard') {
        const decision = item.human_decision;
        if (!decision) { auditMessage('Nada a remover nesta região.', 'warn'); return; }
        const ok = await auditConfirm({
          title: `${action === 'reject' ? 'REJEITAR PRÉVIA' : 'DESCARTAR RASCUNHO'} — ${region}`,
          summary: `<p>Remove a decisão humana registrada para esta região.</p>`,
          effect: 'Nenhum PDF ou tradução aplicada é alterado.',
        });
        if (!ok) { auditMessage('Operação cancelada.', 'warn'); return; }
        await api('/api/ui/human-translation/delete', {method: 'POST',
          body: JSON.stringify({...id, decision_id: decision.human_translation_decision_id})});
        auditMessage('Decisão removida.', 'ok');
      }
      await setAuditMode('previews');
    } catch (error) { auditMessage(error.message || 'Falha na operação.', 'error'); }
  }

  function selectedTriageRegions() {
    return [...auditState.selection];
  }

  function updateBulkCount() {
    const label = $('#auditBulkCount');
    if (label) label.textContent = `${auditState.selection.size} selecionado${auditState.selection.size === 1 ? '' : 's'}`;
  }

  // An in-page dialog, not window.confirm: the operator has to see exactly what
  // is about to change, and what is not.
  function auditConfirm({title, summary, effect}) {
    const dialog = $('#auditConfirmDialog');
    if (!dialog) return Promise.resolve(true);
    $('#auditConfirmTitle').textContent = title;
    $('#auditConfirmSummary').innerHTML = summary;
    $('#auditConfirmEffect').textContent = effect;
    dialog.hidden = false;
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
    return new Promise(resolve => {
      const finish = value => {
        $('#auditConfirmApply').removeEventListener('click', onApply);
        $('#auditConfirmCancel').removeEventListener('click', onCancel);
        dialog.removeEventListener('keydown', onKey);
        if (typeof dialog.close === 'function' && dialog.open) dialog.close();
        dialog.hidden = true;
        resolve(value);
      };
      const onApply = () => finish(true);
      const onCancel = () => finish(false);
      const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); finish(false); } };
      $('#auditConfirmApply').addEventListener('click', onApply);
      $('#auditConfirmCancel').addEventListener('click', onCancel);
      dialog.addEventListener('keydown', onKey);
    });
  }

  // FASE 13 — the operator sees the whole shape of the operation before it runs,
  // including what is deliberately left out of it.
  function bulkBreakdown(regions) {
    const data = auditState.mode === 'ocr' ? auditState.ocrCandidates : auditState.triage;
    if (!data) return '';
    const pool = (data.auto_markable || []).concat(data.review_required || [], data.queue || []);
    const picked = pool.filter(i => regions.includes(String(i.region_id)));
    if (!picked.length) return '';
    const tally = (values) => Object.entries(values.reduce((acc, v) => {
      acc[v] = (acc[v] || 0) + 1; return acc;
    }, {})).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(' · ') || '—';
    const pages = [...new Set(picked.map(i => String(i.page_id || '')))].sort();
    const reasons = picked.flatMap(i => ((i.ocr_assessment || {}).strong_evidence || [])
      .concat((i.ocr_assessment || {}).weak_evidence || []));
    const confidences = picked.map(i => Number(i.confidence)).filter(n => Number.isFinite(n));
    const ambiguous = (data.review_required || []).length;
    const plausible = (data.auto_markable || []).length
      ? pool.filter(i => (i.ocr_assessment || {}).status === 'plausible_semantic_text').length : 0;
    return '<dl class="audit-confirm-facts">'
      + `<dt>páginas</dt><dd>${pages.length} (${escapeHtml(pages.join(', '))})</dd>`
      + `<dt>classes</dt><dd>${tally(picked.map(i => String(i.classification_normalized || '')))}</dd>`
      + `<dt>reason codes</dt><dd>${tally(reasons)}</dd>`
      + `<dt>confiança OCR mínima</dt><dd>${confidences.length ? Math.min(...confidences).toFixed(3) : '—'}</dd>`
      + `<dt>ambíguos não incluídos</dt><dd>${ambiguous}</dd>`
      + `<dt>semanticamente plausíveis não incluídos</dt><dd>${plausible}</dd>`
      + '</dl>';
  }

  const BULK_EFFECTS = {
    ocr_invalid: 'Estas regiões serão retiradas da fila de tradução e encaminhadas para '
      + 'OCR direcionado futuro. Nenhum PDF ou tradução será alterado.',
    translate: 'Estas regiões passam a valer como texto traduzível. Nenhum PDF ou tradução será alterado agora.',
    preserve: 'Estas regiões passam a valer como texto preservado. Nenhum PDF ou tradução será alterado.',
    needs_review: 'Estas regiões ficam marcadas para revisão humana. Nenhum PDF ou tradução será alterado.',
    dismissed: 'Estas regiões saem da fila sem decisão de conteúdo. Nenhum PDF ou tradução será alterado.',
    remove: 'As decisões registradas por você nestas regiões serão removidas. Nenhum PDF ou tradução será alterado.',
  };

  async function applyBulkDecision(decision) {
    const regions = selectedTriageRegions();
    if (!regions.length) { auditMessage('Selecione ao menos uma região.', 'warn'); return; }
    const id = pageRevisionIdentity();
    const source = auditState.mode === 'ocr' ? auditState.ocrCandidates : auditState.triage;
    const hash = source?.source_audit_hash || '';
    const label = decision === 'remove' ? 'REMOVER DECISÃO'
      : (AUDIT_DECISION_LABELS[decision] || decision);
    const preview = regions.slice(0, 8).map(r => `<li>${escapeHtml(r)}</li>`).join('');
    const ok = await auditConfirm({
      title: `${label} — ${regions.length} região(ões)`,
      summary: `<ul class="audit-confirm-list">${preview}</ul>`
        + (regions.length > 8 ? `<p class="muted">e mais ${regions.length - 8}.</p>` : '')
        + bulkBreakdown(regions),
      effect: BULK_EFFECTS[decision] || 'Nenhum PDF ou tradução será alterado.',
    });
    if (!ok) { auditMessage('Operação cancelada.', 'warn'); return; }
    try {
      if (decision === 'remove') {
        // Removing is per-decision; resolve each region's decision id first.
        const review = await api('/api/ui/audit/review', {method: 'POST', body: JSON.stringify(id)});
        const byRegion = new Map((review.records || []).filter(r => r.human_decision)
          .map(r => [String(r.region_id), r.human_decision.audit_decision_id]));
        for (const region of regions) {
          const decisionId = byRegion.get(region);
          if (decisionId) await api('/api/ui/audit/decision/delete', {method: 'POST', body: JSON.stringify({...id, decision_id: decisionId})});
        }
        auditMessage(`Decisões removidas: ${regions.length}.`, 'ok');
      } else {
        const result = await api('/api/ui/audit/decision/bulk', {method: 'POST',
          body: JSON.stringify({...id, region_ids: regions, decision, source_audit_hash: hash})});
        auditMessage(`${result.applied} decisão(ões) aplicadas em ${result.pages.length} página(s).`, 'ok');
      }
      auditState.selection.clear();
      await setAuditMode(auditState.mode);
    } catch (error) { auditMessage(error.message || 'Falha na operação em massa.', 'error'); }
  }

  $$('[data-audit-mode]').forEach(button => button.addEventListener('click', () => setAuditMode(button.dataset.auditMode)));
  $('#auditBulk')?.addEventListener('click', event => {
    const btn = event.target.closest('[data-audit-bulk]');
    if (btn) applyBulkDecision(btn.dataset.auditBulk);
  });
  $('#auditList')?.addEventListener('change', event => {
    const box = event.target.closest('[data-triage-select]');
    if (!box) return;
    const region = String(box.dataset.triageSelect || '');
    if (box.checked) auditState.selection.add(region); else auditState.selection.delete(region);
    updateBulkCount();
  });
  $('#auditList')?.addEventListener('click', async event => {
    if (!event.target.closest('#requestProviderAuth')) return;
    const id = pageRevisionIdentity();
    try {
      const request = await api('/api/ui/audit/provider-authorization', {method: 'POST',
        body: JSON.stringify({...id, confirm: true})});
      auditMessage(`Pedido ${String(request.authorization_request_id).slice(0, 8)} criado: `
        + `${request.estimated_requests} região(ões), ${request.pages.length} página(s). `
        + 'Nenhuma chamada externa foi feita.', 'ok');
    } catch (error) { auditMessage(error.message || 'Falha ao registrar o pedido.', 'error'); }
  });
  $('#qualityReviewList')?.addEventListener('change', event => {
    const checkbox = event.target.closest?.('[data-review-select]');
    if (!checkbox) return;
    const key = String(checkbox.dataset.reviewSelect || '');
    if (!key) return;
    if (checkbox.checked) appState.qualityReviewSelection.add(key);
    else appState.qualityReviewSelection.delete(key);
    updateQualityReviewSelectionUi();
  });
  $$('[data-review-filter]').forEach(button => button.addEventListener('click', () => { appState.qualityReviewFilter = button.dataset.reviewFilter || 'pending'; $$('[data-review-filter]').forEach(item => item.classList.toggle('selected', item === button)); if (appState.qualityReview) renderQualityReview(appState.qualityReview); }));
  $('#confirmQualityReview')?.addEventListener('click', confirmQualityReview);
  $('#qualityReviewSelectAll')?.addEventListener('change', event => {
    const keys = visibleQualityReviewKeys();
    if (event.target.checked) keys.forEach(key => appState.qualityReviewSelection.add(key));
    else keys.forEach(key => appState.qualityReviewSelection.delete(key));
    renderQualityReview(appState.qualityReview);
  });
  $('#acceptLowRiskReview')?.addEventListener('click', () => {
    const lowKeys = visibleQualityReviewKeys({risk: 'LOW'});
    const selectedLow = lowKeys.filter(key => appState.qualityReviewSelection.has(key));
    const keys = selectedLow.length ? selectedLow : lowKeys;
    qualityReviewBulkAction({action: 'reviewed', keys, riskFilter: 'LOW', confirmation: false});
  });
  $('#acceptAllReview')?.addEventListener('click', () => qualityReviewBulkAction({action: 'reviewed', confirmation: true}));
  $('#undoBulkReview')?.addEventListener('click', async () => {
    if (!appState.qualityReview?.job_id || appState.qualityReviewBulkBusy || !appState.qualityReviewUndo.length) return;
    const last = appState.qualityReviewUndo.pop();
    const restore = Object.fromEntries(last.previous || []);
    appState.qualityReviewBulkBusy = true;
    setQualityReviewBulkMessage('Desfazendo última ação em massa...', 'busy');
    try {
      const review = await api('/api/ui/quality-review/bulk-action', {method: 'POST', body: JSON.stringify({job_id: appState.qualityReview.job_id, item_keys: last.keys || [], action: 'pending', undo: true, restore_actions: restore})});
      renderQualityReview(review);
      setQualityReviewBulkMessage('Última ação em massa desfeita.', 'ok');
    } catch (error) {
      setQualityReviewBulkMessage(error.message || 'Não foi possível desfazer.', 'error');
    } finally {
      appState.qualityReviewBulkBusy = false;
      updateQualityReviewSelectionUi();
    }
  });
  $('#nvidiaContractCanary')?.addEventListener('click', async () => {
    if (!qualityReviewDeveloperMode() || !appState.qualityReview?.job_id || appState.qualityReviewBulkBusy) return;
    appState.qualityReviewBulkBusy = true;
    setQualityReviewBulkMessage('Preparando canário NVIDIA escalonado...', 'busy');
    try {
      const current = await pollQualityRevisionStatus(appState.qualityReview.job_id, {once: true});
      const passed = String(current?.status || '') === 'contract_canary_passed';
      const reviewed = Number(current?.reviewed_regions || 0);
      const validity = Number(current?.validity_rate || 0);
      const maxRegions = passed && reviewed >= 3 && validity >= 0.9 ? 10 : (passed && reviewed >= 1 ? 3 : 1);
      setQualityReviewBulkMessage(`Enviando canário NVIDIA de ${maxRegions} região${maxRegions === 1 ? '' : 'ões'}...`, 'busy');
      const status = await api('/api/ui/quality-review/revision/canary/start', {method: 'POST', body: JSON.stringify({job_id: appState.qualityReview.job_id, max_regions: maxRegions})});
      renderQualityRevisionStatus(status);
      setQualityReviewBulkMessage(`Canário iniciado: ${status.phase_label || status.phase || 'em andamento'}.`, 'ok');
      showToast('Canário do contrato NVIDIA iniciado.', 'ok');
      clearTimeout(appState.qualityRevisionPoll);
      appState.qualityRevisionPoll = setTimeout(() => pollQualityRevisionStatus(appState.qualityReview.job_id), 1000);
    } catch (error) {
      setQualityReviewBulkMessage(error.message || 'Não foi possível iniciar o canário NVIDIA.', 'error');
      showToast(error.message || 'Não foi possível iniciar o canário NVIDIA.', 'error');
    } finally {
      appState.qualityReviewBulkBusy = false;
      updateQualityReviewSelectionUi();
    }
  });
  $('#cancelRevisionAction')?.addEventListener('click', async () => {
    const jobId = appState.qualityReview?.job_id;
    if (!jobId || appState.qualityReviewBulkBusy) return;
    if (!window.confirm('Cancelar a revisão em andamento? O progresso já revisado é preservado e você pode retomar depois.')) return;
    appState.qualityReviewBulkBusy = true;
    setQualityReviewBulkMessage('Cancelando revisão...', 'busy');
    try {
      const status = await api('/api/ui/quality-review/revision/cancel', {method: 'POST', body: JSON.stringify({job_id: jobId})});
      renderQualityRevisionStatus(status);
      setQualityReviewBulkMessage(`Revisão ${status.status === 'cancelling' ? 'sendo cancelada' : status.status || 'cancelada'}.`, 'ok');
      clearTimeout(appState.qualityRevisionPoll);
      appState.qualityRevisionPoll = setTimeout(() => pollQualityRevisionStatus(jobId), 1000);
    } catch (error) {
      setQualityReviewBulkMessage(error.message || 'Não foi possível cancelar a revisão.', 'error');
    } finally {
      appState.qualityReviewBulkBusy = false;
      updateQualityReviewSelectionUi();
    }
  });

  $('#resumeRevisionAction')?.addEventListener('click', () => {
    // Resuming continues from the persisted checkpoint; the backend skips the
    // regions that already have a stored answer instead of re-requesting them.
    $('#globalAiReview')?.click();
  });

  $('#globalAiReview')?.addEventListener('click', async () => {
    if (!appState.qualityReview?.job_id || appState.qualityReviewBulkBusy) return;
    appState.qualityReviewBulkBusy = true;
    setQualityReviewBulkMessage('Iniciando revisão completa pela UI...', 'busy');
    try {
      const status = await api('/api/ui/quality-review/revision/start', {method: 'POST', body: JSON.stringify({job_id: appState.qualityReview.job_id})});
      renderQualityRevisionStatus(status);
      setQualityReviewBulkMessage(`Revisão iniciada: ${status.phase_label || status.phase || 'em andamento'}.`, 'ok');
      showToast('Revisão completa iniciada pela UI.', 'ok');
      clearTimeout(appState.qualityRevisionPoll);
      appState.qualityRevisionPoll = setTimeout(() => pollQualityRevisionStatus(appState.qualityReview.job_id), 1000);
    } catch (error) {
      setQualityReviewBulkMessage(error.message || 'Não foi possível iniciar a revisão completa.', 'error');
      showToast(error.message || 'Não foi possível iniciar a revisão completa.', 'error');
    } finally {
      appState.qualityReviewBulkBusy = false;
      updateQualityReviewSelectionUi();
    }
  });
  $('#runRetryAction')?.addEventListener('click', async () => { const id = appState.latestJobId || ''; if (!id) return; try { await api('/api/ui/retry', {method: 'POST', body: JSON.stringify({job_id: id})}); showToast('Nova tentativa iniciada.', 'ok'); pollState(); } catch (error) { showToast(error.message || 'Nao foi possivel tentar novamente.', 'error'); } });

  function renderProgress(progress, pipelineState = null) {
    const state = pipelineState || buildPipelineState({status: appState.status}, progress);
    const runtimeKey = state.stage || progress.stage_key || 'idle';
    const key = state.visualStage || visualStageKey(runtimeKey);
    const activeIndex = stageOrder.indexOf(key);
    const terminalOk = ['finished', 'review_required', 'review_completed'].includes(state.status);
    const sourceReady = appState.status === 'source_analysis_ready';
    $$('.stage-item').forEach(item => {
      const index = stageOrder.indexOf(item.dataset.stage);
      const pct = $('.stage-pct', item);
      const fill = $('.stage-fill', item);
      const failedHere = ['failed', 'cancelled'].includes(state.status) && item.dataset.stage === key;
      item.classList.toggle('done', terminalOk || key === 'final'
        || (sourceReady && item.dataset.stage === 'source_analysis')
        || (activeIndex >= 0 && index < activeIndex));
      item.classList.toggle('active', item.dataset.stage === key && !terminalOk && (appState.status === 'running' || appState.status === 'staging' || appState.status === 'awaiting_source_review' || state.status === 'queued' || state.status === 'starting'));
      item.classList.toggle('failed', failedHere && state.status === 'failed');
      item.classList.toggle('cancelled', failedHere && state.status === 'cancelled');
      item.classList.toggle('indeterminate', item.classList.contains('active') && progress.indeterminate);
      if (sourceReady && item.dataset.stage === 'source_analysis') {
        pct.textContent = 'concluída';
        fill.style.width = '100%';
      }
      else if (item.classList.contains('done')) { pct.textContent = '100%'; fill.style.width = '100%'; }
      else if (item.classList.contains('active') && progress.fraction != null) {
        const percent = Math.max(0, Math.min(100, Math.round(progress.fraction * 100)));
        pct.textContent = progress.total ? `${progress.current}/${progress.total} · ${percent}%` : `${percent}%`;
        fill.style.width = `${percent}%`;
      } else if (item.classList.contains('active')) { pct.textContent = 'em andamento'; fill.style.width = '38%'; }
      else {
        const metric = canonicalStageMetric(item.dataset.stage, state, progress);
        pct.textContent = metric || '—';
        fill.style.width = '0%';
      }
      item.dataset.state = item.classList.contains('done') ? 'completed' : item.classList.contains('active') ? 'active' : item.classList.contains('failed') ? 'failed' : item.classList.contains('cancelled') ? 'cancelled' : 'future';
    });
    if (key !== appState.activeStage) {
      appState.activeStage = key;
      sfxPop(runtimeKey);
    }
    renderPipelinePreview(state);
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
    if (!path && !['reprocess','delete'].includes(action)) return '';
    const icons = {
      pdf: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4"/><path d="M8 16h1.2a1.2 1.2 0 0 0 0-2.4H8V18m5-4.4V18h1.1a2.2 2.2 0 0 0 0-4.4zm5 0h3M18 16h2"/></svg>',
      folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 7h7l2 2h9v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M3 7V5a2 2 0 0 1 2-2h5l2 2h5"/></svg>',
      report: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M6 3h12v18H6z"/><path d="M9 9h6M9 13h3M9 17h6"/><path d="M15 14v3M12 15v2"/></svg>',
      compare: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 5h7v14H4zM13 5h7v14h-7z"/><path d="m9 9-2 2 2 2M15 15l2-2-2-2"/></svg>',
      context: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 5h16v10H8l-4 4z"/><path d="M8 9h8M8 12h5"/></svg>',
      reprocess: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 12a8 8 0 0 1 13.7-5.7L20 8"/><path d="M20 4v4h-4M20 12a8 8 0 0 1-13.7 5.7L4 16"/><path d="M4 20v-4h4"/></svg>',
      delete: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M7 7l1 14h8l1-14"/><path d="M10 11v6M14 11v6"/></svg>',
    };
    const icon = icons[action] || '';
    return `<button class="btn-ghost icon-action" data-action="${action}" data-path="${encodeURIComponent(path || '')}" aria-label="${escapeAttr(label)}" title="${escapeAttr(label)}">${icon}<span class="sr-only">${escapeHtml(label)}</span></button>`;
  }
  function publicationEligibility(record) {
    const terminal = terminalRunStatuses.has(String(record.status || '').toLowerCase());
    const hasPdf = Boolean(record.pdf_path);
    const manifest = record.output_verification === 'manifest_verified' || Boolean(record.manifest_path);
    const authenticated = isCanonicalCommunityAuthenticated();
    const technicalGatePassed = boolish(record.quality_gate) === true;
    const reviewCompleted = record.review_status === 'completed' || record.review_confirmed === true;
    const review = !technicalGatePassed;
    const published = String(record.publication_status || '').toLowerCase() === 'published';
    const changed = published && record.publication_pdf_sha256 && record.pdf_sha256 && record.publication_pdf_sha256 !== record.pdf_sha256;
    const ownership = String(record.community_ownership || '');
    const ownerReady = ownership !== 'legacy' && ownership !== 'unowned_new';
    const baseEligible = hasPdf && manifest && terminal && authenticated;
    return {terminal, hasPdf, manifest, authenticated, review, reviewCompleted,
      technicalGatePassed, published, changed,
      ownership, ownerReady, baseEligible,
      eligible: baseEligible && ownerReady};
  }
  function publicationAction(record) {
    const eligibility = publicationEligibility(record);
    if (!eligibility.hasPdf) return '';
    if (eligibility.published && !eligibility.changed) {
      const postId = escapeAttr(record.publication_id || '');
      return `<button class="btn-ghost" data-action="open-publication" data-publication-id="${postId}" ${postId ? '' : 'disabled'} title="Abrir publicação existente">Publicado</button>`;
    }
    if (!eligibility.authenticated) return '<button class="btn-ghost" data-action="publish" disabled title="Entre para publicar">Publicação indisponível</button>';
    if (!eligibility.manifest || !eligibility.terminal) return '<button class="btn-ghost" data-action="publish" disabled>Publicação indisponível</button>';
    if (!eligibility.ownerReady) return '<button class="btn-ghost" data-action="publish" disabled>Vincule antes de publicar</button>';
    if (eligibility.published && !eligibility.changed) return '<button class="btn-ghost" data-action="publish">Publicado</button>';
    return `<button class="btn-ghost" data-action="publish">${eligibility.changed ? 'Atualizar publicação' : 'Publicar na comunidade'}</button>`;
  }
  function claimEligibility(record) {
    const eligibility = publicationEligibility(record);
    const ownerState = String(record.community_ownership || '');
    const hasIdentity = /^[0-9a-f]{32}$/.test(String(record.job_id || '')) &&
      Boolean(String(record.run_id || '').trim()) && /^[0-9a-f]{64}$/.test(String(record.pdf_sha256 || '').toLowerCase());
    return { ...eligibility,
      eligible: ownerState === 'legacy' && hasIdentity && eligibility.baseEligible && !eligibility.published };
  }
  function claimAction(record) {
    if (!claimEligibility(record).eligible) return '';
    return '<button class="btn-ghost" data-action="claim">Vincular à minha conta</button>';
  }
  // Canonical job/run identity for a finished chapter card. The review entry must
  // adopt exactly this chapter's job_id + run_id, never runtime.latest or the title.
  function reviewIdentity(record) {
    // Only the canonical job id identifies the chapter; a presentation/history
    // row id must never be used as the source job.
    const jobId = String(record.job_id || '').toLowerCase();
    const runId = String(record.run_id || '').trim();
    if (!/^[0-9a-f]{32}$/.test(jobId) || !runId) return null;
    if (!record.quality_report_path) return null;
    return {jobId, runId};
  }
  function reviewActionLabel(record) {
    const status = String(record.status || '').toLowerCase();
    if (record.review_status === 'completed' || status === 'review_completed') return 'Ver revisão';
    if (status === 'review_running' || status === 'running') return 'Continuar revisão';
    return 'Revisar';
  }
  function reviewAction(record) {
    if (!reviewIdentity(record)) return '';
    const label = reviewActionLabel(record);
    return `<button class="btn-ghost hm-review-action" data-action="review" title="Revisar OCR, tradução e qualidade deste capítulo" aria-label="${escapeAttr(`${label}: ${record.chapter_name || record.slug || 'capítulo'}`)}">${escapeHtml(label)}</button>`;
  }
  function renderSourceAnalysisReady(record) {
    const panel = $('#sourceReadyPanel');
    if (!panel) return;
    const result = record?.source_analysis_result || {};
    appState.sourceReady = record || null;
    const policy = appState.settings?.workspace_source_policy || {};
    const safe = value => escapeHtml(String(value || '—'));
    const operation = String(result.operation_id || '').slice(0, 12);
    const completed = result.completed_at
      ? new Date(Number(result.completed_at) * 1000).toLocaleString('pt-BR') : '—';
    $('#sourceReadyMeta').innerHTML = [
      '<strong>A fonte foi reconhecida e a política do workspace está sendo resolvida.</strong>',
      `Fonte: ${safe(record?.source_type || result.source_kind)}`,
      `Adapter: ${safe(result.adapter)}`,
      `Inspeção: ${result.browser_inspection_performed ? 'navegador controlado' : 'preflight'}`,
      `Navegador: ${safe(result.browser_engine)}`,
      `Estrutura: ${result.public_structure_indicators_present ? 'compatível' : 'não confirmada'}`,
      `Concluída: ${safe(completed)}`,
      `Operação: ${safe(operation)}`,
      `Motivo: <code>${safe(result.reason_code)}</code>`,
    ].join('<br>');
    panel.hidden = false;
    const active = policy.status === 'active' && policy.all_submitted_sources_authorized === true;
    $('#sourceReadyPolicyState').textContent = active
      ? 'Autorização: política do workspace. Preparando download.'
      : 'A política de fontes autorizadas está desativada.';
    $('#openSourcePolicySettings').hidden = active;
  }
  $('#openSourcePolicySettings')?.addEventListener('click', () => activateTab('settings'));
  function pendingPreviewItems() {
    return Array.isArray(appState.pendingHumanPreviews?.items) ? appState.pendingHumanPreviews.items : [];
  }
  function pendingPreviewsForRecord(record) {
    const identity = reviewIdentity(record);
    if (!identity) return [];
    return pendingPreviewItems().filter(item =>
      String(item.job_id || '').toLowerCase() === identity.jobId &&
      String(item.run_id || '') === identity.runId);
  }
  function readyPreviewsForRecord(record) {
    return pendingPreviewsForRecord(record).filter(item => item.approval_enabled === true && item.blocked !== true);
  }
  function pendingPreviewLabel(item) {
    const page = item.page_display_number || item.page_number || item.page_id || '—';
    const region = item.region_id || 'região';
    return `página ${page} · ${region}`;
  }
  function firstReadyPreviewForRecord(record) {
    return readyPreviewsForRecord(record)[0] || null;
  }
  function historyPreviewAction(record) {
    const all = pendingPreviewsForRecord(record);
    if (!all.length) return '';
    const ready = all.filter(item => item.approval_enabled === true && item.blocked !== true);
    const blocked = all.length - ready.length;
    const label = ready.length ? `${ready.length} prévia${ready.length === 1 ? '' : 's'} pronta${ready.length === 1 ? '' : 's'}` : `${blocked} prévia${blocked === 1 ? '' : 's'} bloqueada${blocked === 1 ? '' : 's'}`;
    const action = ready.length ? 'open-preview' : 'review';
    return `<span class="badge preview ${ready.length ? 'ready' : 'blocked'}">${escapeHtml(label)}</span>`
      + `<button class="btn-primary hm-preview-action" data-action="${escapeAttr(action)}">${ready.length ? 'Abrir prévia' : 'Ver bloqueio'}</button>`;
  }
  function renderPendingPreviewSummary(target, {compact = false} = {}) {
    const node = typeof target === 'string' ? $(target) : target;
    if (!node) return;
    const items = pendingPreviewItems();
    if (!items.length) {
      node.hidden = true;
      node.innerHTML = '';
      return;
    }
    const ready = items.filter(item => item.approval_enabled === true && item.blocked !== true);
    const blocked = items.filter(item => item.blocked === true);
    const cards = items.slice(0, compact ? 2 : 4).map(item => {
      const enabled = item.approval_enabled === true && item.blocked !== true;
      const status = enabled ? 'PRONTA PARA REVISÃO HUMANA' : 'BLOQUEADA — REQUER RECONSTRUÇÃO DE ARTE';
      return `<button type="button" class="pending-preview-row ${enabled ? 'ready' : 'blocked'}" data-open-pending-preview="${escapeAttr(item.region_id || '')}" data-job-id="${escapeAttr(item.job_id || '')}" data-run-id="${escapeAttr(item.run_id || '')}">`
        + `<span><strong>${escapeHtml(item.chapter_display_name || item.chapter_name || 'Capítulo')}</strong>`
        + `<small>${escapeHtml(pendingPreviewLabel(item))}</small></span>`
        + `<span class="pending-preview-status">${escapeHtml(status)}</span></button>`;
    }).join('');
    node.hidden = false;
    node.innerHTML = `<div class="pending-preview-head"><strong>Prévia humana pendente</strong>`
      + `<span>${ready.length} pronta${ready.length === 1 ? '' : 's'} · ${blocked.length} bloqueada${blocked.length === 1 ? '' : 's'}</span></div>`
      + cards;
  }
  function renderPendingPreviewSurfaces() {
    renderPendingPreviewSummary('#homePendingPreviews');
    renderPendingPreviewSummary('#historyPendingPreviews', {compact: true});
    const count = Number(appState.pendingHumanPreviews?.item_count || pendingPreviewItems().length || 0);
    $$('[data-audit-mode="previews"]').forEach(button => {
      button.textContent = count ? `PRÉVIAS HUMANAS (${count})` : 'PRÉVIAS HUMANAS';
    });
  }
  function renderReviewPreviewAccess() {
    const node = $('#reviewPreviewAccess');
    if (!node) return;
    const jobId = String(appState.reviewMode?.jobId || '').toLowerCase();
    const runId = String(appState.reviewMode?.runId || '');
    const items = pendingPreviewItems().filter(item =>
      String(item.job_id || '').toLowerCase() === jobId && String(item.run_id || '') === runId);
    if (!items.length) {
      node.hidden = true;
      node.innerHTML = '';
      return;
    }
    const ready = items.filter(item => item.approval_enabled === true && item.blocked !== true);
    const first = ready[0] || items[0];
    node.hidden = false;
    node.innerHTML = `<span>${ready.length} prévia${ready.length === 1 ? '' : 's'} pronta${ready.length === 1 ? '' : 's'} para inspeção · ${items.length - ready.length} bloqueada${items.length - ready.length === 1 ? '' : 's'}</span>`
      + `<button type="button" class="btn-primary" data-open-pending-preview="${escapeAttr(first.region_id || '')}" data-job-id="${escapeAttr(first.job_id || '')}" data-run-id="${escapeAttr(first.run_id || '')}">Abrir prévia</button>`;
  }
  async function loadPendingHumanPreviews() {
    try {
      const data = await api('/api/ui/human-previews/pending');
      appState.pendingHumanPreviews = {
        ...data,
        items: Array.isArray(data.items) ? data.items : [],
      };
    } catch (error) {
      appState.pendingHumanPreviews = {items: [], item_count: 0, ready_count: 0, blocked_count: 0, error: error.message || 'preview_unavailable'};
    }
    renderPendingPreviewSurfaces();
    renderReviewPreviewAccess();
    restorePendingPreviewFromUrl();
    return appState.pendingHumanPreviews;
  }
  function applyReviewMode(info) {
    document.documentElement.dataset.reviewMode = '1';
    const banner = $('#reviewModeBanner');
    if (banner) {
      banner.hidden = false;
      const title = $('#reviewModeTitle');
      const metaEl = $('#reviewModeMeta');
      if (title) title.textContent = `Revisando ${info.chapterName}`;
      if (metaEl) metaEl.textContent = `job ${String(info.jobId).slice(0, 8)}… · run ${String(info.runId).slice(0, 8)}…`;
    }
    const start = $('#startBtn');
    if (start) start.hidden = true;
    ['#urlInput', '#localFolderInput', '#nameInput', '#outputInput'].forEach(sel => {
      const el = $(sel);
      if (el) el.readOnly = true;
    });
  }
  // Leave review_mode and hand the Nova tradução form back to the user: the
  // reviewed chapter must not keep the form hostage. Never touches the
  // revision's checkpoints or cancels background work.
  function exitReviewMode({clearForm = true} = {}) {
    const wasActive = Boolean(appState.reviewMode);
    delete document.documentElement.dataset.reviewMode;
    appState.reviewMode = null;
    appState.currentJobId = '';
    setGlobal('__tradutorCurrentJobId', '');
    const banner = $('#reviewModeBanner');
    if (banner) banner.hidden = true;
    const start = $('#startBtn');
    if (start) { start.hidden = false; start.disabled = false; }
    ['#urlInput', '#localFolderInput', '#nameInput', '#outputInput'].forEach(sel => {
      const el = $(sel);
      if (el) el.readOnly = false;
    });
    if (wasActive && clearForm) {
      appState.programmingFields = true;
      ['#urlInput', '#localFolderInput', '#nameInput', '#outputInput'].forEach(sel => {
        const el = $(sel);
        if (el) el.value = '';
      });
      appState.programmingFields = false;
      appState.nameDirty = false;
      appState.outputDirty = false;
      appState.qualityReview = null;
    }
    appState.reviewPanelDismissed = true;
    const panel = $('#qualityReviewPanel');
    if (panel) panel.hidden = true;
    const revisionStatus = $('#qualityRevisionStatus');
    if (revisionStatus) revisionStatus.hidden = true;
    try {
      const url = new URL(window.location.href);
      ['view', 'job_id', 'run_id'].forEach(key => url.searchParams.delete(key));
      window.history.replaceState({}, '', url);
    } catch (_) { /* history is best-effort */ }
    return wasActive;
  }
  // Adopt exactly this chapter's job/run and open the existing review panel in
  // Nova tradução. Never creates a job, starts a run, or calls NVIDIA.
  async function openChapterReview(record, {restore = false} = {}) {
    const identity = reviewIdentity(record);
    if (!identity) { showToast('Revisão indisponível para este capítulo.', 'warn'); return; }
    const {jobId, runId} = identity;
    appState.reviewMode = {jobId, runId, chapterName: record.chapter_name || record.slug || 'Capítulo'};
    appState.currentJobId = jobId;
    appState.reviewPanelDismissed = false;
    if (!restore) {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set('view', 'review');
        url.searchParams.set('job_id', jobId);
        url.searchParams.set('run_id', runId);
        window.history.replaceState({}, '', url);
      } catch (_) { /* history is best-effort; in-memory state still applies */ }
    }
    activateTab('nova');
    applyReviewMode(appState.reviewMode);
    // The user may leave review_mode while these run; a late response must not
    // resurrect the panel over a fresh Nova tradução form.
    const stillReviewing = () => appState.reviewMode?.jobId === jobId;
    try {
      const review = await api(`/api/ui/quality-review/${encodeURIComponent(jobId)}`);
      if (!stillReviewing()) return;
      if (review) renderQualityReview(review);
    } catch (_) {
      if (stillReviewing()) setQualityReviewBulkMessage('Não foi possível carregar a revisão deste capítulo.', 'error');
    }
    if (!stillReviewing()) return;
    try {
      const status = await api(`/api/ui/quality-review/revision/${encodeURIComponent(jobId)}`);
      if (!stillReviewing()) return;
      renderQualityRevisionStatus(status);
    } catch (_) { /* revision status is optional context */ }
    if (!stillReviewing()) return;
    updateQualityReviewDeveloperActions();
    const panel = $('#qualityReviewPanel');
    if (panel) {
      panel.hidden = false;
      panel.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
    if (restore) { restorePageRevisionFromUrl(); restoreAuditFromUrl(); }
  }
  async function openPendingPreview(item, {preserveUrl = false} = {}) {
    if (!item) return;
    const record = appState.history.find(candidate =>
      String(candidate.job_id || '').toLowerCase() === String(item.job_id || '').toLowerCase() &&
      String(candidate.run_id || '') === String(item.run_id || ''));
    if (!record) {
      showToast('Prévia encontrada, mas o capítulo não está no histórico carregado.', 'warn');
      return;
    }
    if (!preserveUrl && item.comparison_url) {
      try { window.history.replaceState({}, '', item.comparison_url); } catch (_) { /* url is advisory */ }
    }
    await openChapterReview(record, {restore: true});
    await setAuditMode('previews');
    if (item.page_number && item.page_revision_id) {
      await openPageRevision(Number(item.page_number), {
        restore: true,
        pageRevisionId: String(item.page_revision_id || ''),
        focusRegion: String(item.region_id || ''),
      });
    }
    openHumanPreviewComparison(item);
    const node = $(`#auditList [data-region="${CSS.escape(String(item.region_id || ''))}"]`);
    if (node) node.scrollIntoView({behavior: 'smooth', block: 'center'});
  }
  function pendingPreviewFromDataset(element) {
    if (!element) return null;
    const region = String(element.dataset.openPendingPreview || '');
    const jobId = String(element.dataset.jobId || '').toLowerCase();
    const runId = String(element.dataset.runId || '');
    return pendingPreviewItems().find(item =>
      String(item.region_id || '') === region &&
      String(item.job_id || '').toLowerCase() === jobId &&
      String(item.run_id || '') === runId) || null;
  }
  function restorePendingPreviewFromUrl() {
    if (appState.previewRestoreAttempted) return;
    let params;
    try { params = new URLSearchParams(window.location.search || ''); } catch (_) { return; }
    if (params.get('preview_compare') !== '1') return;
    const jobId = String(params.get('job_id') || '').toLowerCase();
    const runId = String(params.get('run_id') || '');
    const region = String(params.get('region_id') || '');
    const item = pendingPreviewItems().find(candidate =>
      String(candidate.job_id || '').toLowerCase() === jobId &&
      String(candidate.run_id || '') === runId &&
      String(candidate.region_id || '') === region);
    if (!item) return;
    appState.previewRestoreAttempted = true;
    void openPendingPreview(item, {preserveUrl: true});
  }
  function restoreReviewModeFromUrl() {
    let params;
    try { params = new URLSearchParams(window.location.search || ''); } catch (_) { return; }
    if (params.get('view') !== 'review') return;
    const jobId = String(params.get('job_id') || '').toLowerCase();
    if (!/^[0-9a-f]{32}$/.test(jobId)) return;
    const record = appState.history.find(item => String(item.job_id || '').toLowerCase() === jobId);
    if (record) void openChapterReview(record, {restore: true});
  }
  function renderHistoryCard(record) {
    const title = record.chapter_name || record.slug || 'Capítulo';
    const engine = record.mode === 'fast' ? 'rapid' : 'paddle';
    const statusLabel = record.review_status === 'completed' ? 'revisão concluída' : (runStatusLabels[record.status] || record.status || 'local');
    const gateValue = boolish(record.quality_gate);
    const gate = gateValue === true ? 'gate aprovado' : gateValue === false ? 'gate reprovado' : 'gate pendente';
    const provenance = record.output_verification === 'legacy_unverified' ? 'origem não verificada' : record.output_verification === 'e2e_evidence' ? 'evidência E2E' : record.output_verification === 'manifest_verified' ? 'manifest verificado' : 'origem não informada';
    const meta = `${Number(record.pages_processed || 0)} páginas · ${Number(record.groups_translated || 0)} grupos · ${formatSeconds(record.total_seconds)} · ${gate} · ${provenance}`;
    const previewActionHtml = historyPreviewAction(record);
    return `<div class="hist-item" data-id="${escapeAttr(record.id || '')}">
      <div class="hist-cover" style="background:${engine === 'rapid' ? '#2f7a6b' : '#c9a227'}">${escapeHtml(title.slice(0, 1).toUpperCase())}</div>
      <div class="hist-meta"><div class="hm-title">${escapeHtml(title)}</div><div class="hm-sub">${escapeHtml(meta)}</div>
      <div class="hm-badges"><span class="badge ep">${escapeHtml(statusLabel)}</span><span class="badge ${engine}">${engine === 'rapid' ? 'Rápido' : 'Qualidade'}</span>${previewActionHtml && previewActionHtml.startsWith('<span') ? previewActionHtml.split('</span>')[0] + '</span>' : ''}</div></div>
      <div class="hm-actions">${previewActionHtml ? previewActionHtml.replace(/^<span[^]*?<\/span>/, '') : ''}${reviewAction(record)}${actionButton('Abrir PDF', 'pdf', record.pdf_path)}${actionButton('Abrir pasta', 'folder', record.output_folder)}${actionButton('Relatório', 'report', record.quality_report_path)}${actionButton('Comparar', 'compare', record.compare_sheet_path)}${actionButton('Contexto', 'context', record.session_context_path)}${actionButton('Reprocessar', 'reprocess')}${claimAction(record)}${publicationAction(record)}${actionButton('Excluir capítulo local', 'delete')}</div>
    </div>`;
  }
  function renderHistory() {
    const list = $('#histList');
    if (!list) return;
    renderPendingPreviewSurfaces();
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
      const open = appState.expandedFolders.has(key);
      const folderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h8l2 2"/></svg>';
      const panelId = `series-panel-${slugify(key) || 'series'}`;
      return `<div class="community-folder ${open ? 'open' : ''}" data-folder="${escapeAttr(key)}"><button type="button" class="cf-header" data-folder="${escapeAttr(key)}" aria-expanded="${open ? 'true' : 'false'}" aria-controls="${escapeAttr(panelId)}" aria-label="${escapeAttr(`Expandir ${group.series}`)}"><span class="cf-icon">${folderIcon}</span><span class="cf-name">${escapeHtml(group.series)}</span><span class="cf-count">${group.records.length} ${group.records.length === 1 ? 'capítulo' : 'capítulos'}</span><span class="cf-chevron">⌄</span></button><div class="cf-body" id="${escapeAttr(panelId)}" role="region" aria-hidden="${open ? 'false' : 'true'}">${group.records.map(renderHistoryCard).join('')}</div></div>`;
    }).join('');
  }
  const statusLabels = {online: 'online', away: 'ausente', busy: 'ocupado', offline: 'offline'};
  function applyCanonicalAuthSurface(state) {
    const authenticated = String(state || '') === 'authenticated';
    document.documentElement.dataset.shellState = authenticated ? 'authenticated' : 'unauthenticated';
    const protectedSelectors = ['#railProfile', '.rail-tab[data-tab="community"]', '.rail-tab[data-tab="profile"]', '#view-community', '#view-profile'];
    protectedSelectors.forEach(selector => $$(selector).forEach(element => {
      element.hidden = !authenticated;
      element.setAttribute('aria-hidden', authenticated ? 'false' : 'true');
    }));
    const profileControls = $$('#view-profile input, #view-profile textarea, #view-profile select, #view-profile button');
    profileControls.forEach(control => { control.disabled = !authenticated; });
    if (!authenticated) {
      appState.profile = {};
      renderProfile({});
      if ($('#view-community')?.classList.contains('active') || $('#view-profile')?.classList.contains('active')) activateTab('inicio');
    }
  }
  function toggleHistoryFolder(folder) {
    const key = folder?.dataset?.folder;
    if (!key) return;
    appState.expandedFolders.has(key) ? appState.expandedFolders.delete(key) : appState.expandedFolders.add(key);
    renderHistory();
  }
  $('#histSearch')?.addEventListener('input', renderHistory);
  $('#homePendingPreviews')?.addEventListener('click', event => {
    const target = event.target.closest('[data-open-pending-preview]');
    if (!target) return;
    void openPendingPreview(pendingPreviewFromDataset(target));
  });
  $('#historyPendingPreviews')?.addEventListener('click', event => {
    const target = event.target.closest('[data-open-pending-preview]');
    if (!target) return;
    void openPendingPreview(pendingPreviewFromDataset(target));
  });
  $('#reviewPreviewAccess')?.addEventListener('click', event => {
    const target = event.target.closest('[data-open-pending-preview]');
    if (!target) return;
    void openPendingPreview(pendingPreviewFromDataset(target));
  });
  $('#reviewModeExit')?.addEventListener('click', () => { exitReviewMode(); activateTab('hist'); });
  const developerModeToggle = $('#developerModeToggle');
  if (developerModeToggle) {
    developerModeToggle.checked = qualityReviewDeveloperMode();
    developerModeToggle.addEventListener('change', () => {
      try {
        if (developerModeToggle.checked) localStorage.setItem('tradutorDeveloperMode', '1');
        else localStorage.removeItem('tradutorDeveloperMode');
      } catch (_) { /* private mode: dev toggle stays session-only */ }
      updateQualityReviewDeveloperActions();
    });
  }
  window.addEventListener('tradutor-auth-changed', event => {
    const state = String(event?.detail?.state || getGlobal('__tradutorAuthState') || '');
    if (state !== 'authenticated') clearCommunityObjectUrls();
    applyCanonicalAuthSurface(state);
    renderHistory();
    // The initial bootstrap may race the SDK/backend session check. Refresh the
    // authoritative local records once authentication settles.
    void refreshBootstrap();
  });
  applyCanonicalAuthSurface(getGlobal('__tradutorAuthState') || 'auth_loading');
  $('#histList')?.addEventListener('click', async event => {
    const folder = event.target.closest('.cf-header');
    if (folder) {
      toggleHistoryFolder(folder);
      return;
    }
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const record = appState.history.find(item => String(item.id) === String(button.closest('.hist-item')?.dataset.id));
    if (!record) return;
    if (button.dataset.action === 'open-preview') {
      void openPendingPreview(firstReadyPreviewForRecord(record) || pendingPreviewsForRecord(record)[0]);
      return;
    }
    if (button.dataset.action === 'review') { void openChapterReview(record); return; }
    if (button.dataset.action === 'reprocess') { loadRecordIntoForm(record); return; }
    if (button.dataset.action === 'claim') { openClaimModal(record); return; }
    if (button.dataset.action === 'delete') { openLocalDeleteModal(record); return; }
    if (button.dataset.action === 'open-publication') {
      const postId = button.dataset.publicationId || '';
      if (postId) void openAuthenticatedCommunityPdf(postId, button);
      return;
    }
    if (button.dataset.action === 'publish') { openPublicationModal(record); return; }
    if (['pdf','folder','report','compare','context'].includes(button.dataset.action || '')) {
      await openArtifact(decodeURIComponent(button.dataset.path || ''));
    }
  });
  $('#histList')?.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const folder = event.target.closest('.cf-header');
    if (!folder) return;
    event.preventDefault();
    toggleHistoryFolder(folder);
  });

  function closeLocalDeleteModal() {
    const overlay = $('#localDeleteModalOverlay');
    if (overlay) { overlay.classList.remove('show'); overlay.setAttribute('aria-hidden', 'true'); }
    appState.localDeleteRecord = null;
    appState.localDeleteBusy = false;
  }
  function openLocalDeleteModal(record) {
    const overlay = $('#localDeleteModalOverlay');
    if (!overlay) return;
    appState.localDeleteRecord = record;
    appState.localDeleteBusy = false;
    const published = String(record.publication_status || '').toLowerCase() === 'published';
    $('#localDeleteSummary').innerHTML = [
      ['Obra', record.series_name || record.chapter_name || '—'],
      ['Capítulo', record.chapter_name || record.slug || '—'],
      ['Páginas', Number(record.pages_processed || 0)],
      ['Pasta', record.output_folder ? String(record.output_folder).replace(/^.*\\output\\?/i, 'output\\') : '—'],
      ['Publicação', published ? 'existe e será preservada' : 'nenhuma vinculada'],
    ].map(([label, value]) => `<div class="pub-meta"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`).join('');
    $('#localDeleteNotice').textContent = published
      ? 'Este capítulo já possui publicação na Comunidade. A exclusão local não removerá a publicação, e os arquivos usados por ela não serão apagados por este fluxo.'
      : 'Esta ação remove o item selecionado do histórico. Marque a opção de arquivos apenas para apagar a pasta local deste capítulo.';
    $('#localDeleteFiles').checked = false;
    $('#localDeleteFiles').disabled = published;
    $('#localDeleteConfirm').value = '';
    $('#localDeleteError').hidden = true;
    $('#localDeleteSubmit').disabled = false;
    $('#localDeleteSubmit').textContent = 'Excluir localmente';
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    $('#localDeleteConfirm')?.focus();
  }
  async function deleteLocalArtifact() {
    if (appState.localDeleteBusy || !appState.localDeleteRecord) return;
    const record = appState.localDeleteRecord;
    const submit = $('#localDeleteSubmit');
    const error = $('#localDeleteError');
    if (String($('#localDeleteConfirm')?.value || '') !== 'EXCLUIR') {
      if (error) { error.textContent = 'Digite EXCLUIR para confirmar.'; error.hidden = false; }
      return;
    }
    appState.localDeleteBusy = true;
    uiTrace('local_delete_submit', {reason_code: $('#localDeleteFiles')?.checked === true ? 'delete_files' : 'hide_only'});
    if (submit) { submit.disabled = true; submit.textContent = 'Excluindo...'; }
    try {
      const result = await api('/api/ui/history/delete', {
        method: 'POST',
        body: JSON.stringify({
          local_artifact_id: record.id,
          delete_files: $('#localDeleteFiles')?.checked === true,
          confirmation: 'EXCLUIR',
        }),
      });
      uiTrace('local_delete_completed', {code: result.code});
      closeLocalDeleteModal();
      showToast(result.deleted_files ? 'Capítulo local e arquivos apagados.' : 'Capítulo removido do histórico local.', 'ok');
      await refreshBootstrap();
    } catch (errorValue) {
      appState.localDeleteBusy = false;
      if (submit) { submit.disabled = false; submit.textContent = 'Excluir localmente'; }
      if (error) { error.textContent = humanCommunityError(errorValue, 'Não foi possível excluir este capítulo local.'); error.hidden = false; }
    }
  }
  $('#localDeleteCancel')?.addEventListener('click', closeLocalDeleteModal);
  $('#localDeleteModalClose')?.addEventListener('click', closeLocalDeleteModal);
  $('#localDeleteModalOverlay')?.addEventListener('click', event => {
    if (event.target === $('#localDeleteModalOverlay')) closeLocalDeleteModal();
  });
  $('#localDeleteForm')?.addEventListener('submit', event => {
    event.preventDefault();
    void deleteLocalArtifact();
  });

  /* ---------- authenticated legacy ownership ---------- */
  function closeClaimModal() {
    const overlay = $('#claimModalOverlay');
    if (overlay) { overlay.classList.remove('show'); overlay.setAttribute('aria-hidden', 'true'); }
    appState.claimRecord = null;
    appState.claimBusy = false;
  }
  function openClaimModal(record) {
    if (!claimEligibility(record).eligible) {
      showToast('Este capítulo não está disponível para vinculação.', 'warn');
      return;
    }
    const overlay = $('#claimModalOverlay');
    if (!overlay) return;
    appState.claimRecord = record;
    appState.claimBusy = false;
    const summary = $('#claimSummary');
    if (summary) summary.innerHTML = [
      ['Obra', record.series_name || record.chapter_name || '—'],
      ['Capítulo', record.chapter_name || record.slug || '—'],
      ['Páginas', Number(record.pages_processed || 0)],
      ['SHA-256', String(record.pdf_sha256 || '').slice(0, 16) + '…'],
    ].map(([label, value]) => `<div class="pub-meta"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`).join('');
    $('#claimConfirm').checked = false;
    $('#claimError').hidden = true;
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    $('#claimConfirm')?.focus();
  }
  async function claimArtifact(record) {
    if (appState.claimBusy || !claimEligibility(record).eligible) return;
    uiTrace('claim_started');
    appState.claimBusy = true;
    const submit = $('#claimSubmit');
    if (submit) { submit.disabled = true; submit.textContent = 'Vinculando…'; }
    try {
      await api(`/api/community/artifacts/${encodeURIComponent(record.job_id)}/claim`, {
        method: 'POST',
        body: JSON.stringify({
          expected_run_id: String(record.run_id || ''),
          expected_pdf_sha256: String(record.pdf_sha256 || '').toLowerCase(),
          confirm: true,
        }),
      });
      record.community_ownership = 'owned';
      uiTrace('claim_completed', {status: 200});
      closeClaimModal();
      showToast('Capítulo vinculado à sua conta. Agora você pode publicar.', 'ok');
      renderHistory();
    } catch (errorValue) {
      uiTrace('claim_failed', {code: errorValue.code || 'claim_failed', status: errorValue.status || 0});
      const error = $('#claimError');
      if (error) { error.textContent = errorValue.message || 'Não foi possível vincular este capítulo.'; error.hidden = false; }
      appState.claimBusy = false;
      if (submit) { submit.disabled = false; submit.textContent = 'Vincular à minha conta'; }
    }
  }
  $('#claimCancel')?.addEventListener('click', closeClaimModal);
  $('#claimModalClose')?.addEventListener('click', closeClaimModal);
  $('#claimModalOverlay')?.addEventListener('click', event => {
    if (event.target === $('#claimModalOverlay')) closeClaimModal();
  });
  $('#claimForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    if (!$('#claimConfirm')?.checked || !appState.claimRecord) return;
    await claimArtifact(appState.claimRecord);
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
    appState.publicationCorrelation = correlationId();
    uiTrace('publication_modal_opened', {correlation_id: appState.publicationCorrelation});
    const draftKey = String(record.id || record.slug || '');
    const draft = appState.publicationDrafts[draftKey] || {};
    const review = eligibility.review;
    const pendingCount = Number(record.manual_review_count || record.rejected_count || 0);
    summary.innerHTML = [
      ['Obra', record.series_name || record.chapter_name || '—'],
      ['Capítulo', record.chapter_name || record.slug || '—'],
      ['Páginas', Number(record.pages_processed || 0)],
      ['Qualidade', review
        ? (eligibility.reviewCompleted
          ? 'gate técnico reprovado · revisão humana concluída'
          : 'gate técnico reprovado · revisão necessária')
        : 'gate técnico aprovado'],
    ].map(([label, value]) => `<div class="pub-meta"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`).join('');
    pending.hidden = !review;
    pending.textContent = review
      ? (eligibility.reviewCompleted
        ? 'Este capítulo possui revisão humana concluída, mas o gate técnico automático registrou pendências. Confirme que você revisou as pendências e autoriza a publicação desta versão.'
        : `Este capítulo tem ${pendingCount || 'pendências de'} revisão. Confirme que você conferiu o resultado antes de publicar.`)
      : '';
    $('#publicationTitle').value = draft.title || record.chapter_name || record.slug || '';
    $('#publicationDescription').value = draft.description || '';
    $('#publicationTags').value = draft.tags || (Array.isArray(record.publication_tags || record.tags)
      ? (record.publication_tags || record.tags).join(', ')
      : String(record.publication_tags || record.tags || ''));
    $('#publicationVisibility').value = draft.visibility || 'public';
    if ($('#publicationAllowComments')) $('#publicationAllowComments').checked = draft.allow_comments !== false;
    $('#publicationConfirm').checked = false;
    $('#publicationSubmit').textContent = eligibility.published ? 'Atualizar publicação' : 'Publicar';
    $('#publicationError').hidden = true;
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    $('#publicationTitle')?.focus();
  }
  function publicationError(message, code = 'validation_failed') {
    const error = $('#publicationError');
    if (error) { error.textContent = message; error.hidden = false; }
    uiTrace('publication_failed', {code, correlation_id: appState.publicationCorrelation});
  }
  function validatePublicationForm(record) {
    const eligibility = publicationEligibility(record);
    uiTrace('validation_started', {correlation_id: appState.publicationCorrelation});
    if (!eligibility.authenticated) { publicationError('Sua sessão não está mais válida. Entre novamente.', 'authentication_required'); return false; }
    if (!eligibility.ownerReady) { publicationError('Vincule este capítulo à sua conta antes de publicar.', 'artifact_has_no_owner'); return false; }
    if (!String($('#publicationTitle')?.value || '').trim()) { publicationError('Informe um título.', 'title_required'); return false; }
    if (!$('#publicationConfirm')?.checked) { publicationError('Confirme que revisou as pendências.', 'confirmation_required'); return false; }
    uiTrace('validation_result', {valid: true, correlation_id: appState.publicationCorrelation});
    return true;
  }
  async function publishToCommunity(record) {
    if (appState.publicationBusy) return;
    uiTrace('publish_click_received', {correlation_id: appState.publicationCorrelation});
    const eligibility = publicationEligibility(record);
    if (!validatePublicationForm(record) || !eligibility.eligible) {
      if (eligibility.eligible === false && eligibility.ownerReady && eligibility.authenticated) {
        publicationError('Publicação indisponível para este resultado.', 'publication_ineligible');
      }
      return;
    }
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
      allow_comments: $('#publicationAllowComments')?.checked !== false,
    };
    if (trustedJobId) payload.source_job_id = trustedJobId;
    appState.publicationBusy = true;
    const correlation = appState.publicationCorrelation || correlationId();
    uiTrace('publication_request_started', {correlation_id: correlation});
    const submit = $('#publicationSubmit');
    if (submit) { submit.disabled = true; submit.textContent = 'Publicando…'; }
    try {
      const result = await api('/api/community/publish', {
        method: 'POST',
        headers: {'X-Tradutor-Correlation-ID': correlation},
        body: JSON.stringify(payload),
      });
      uiTrace('publication_response_received', {status: 200, correlation_id: correlation});
      record.publication_status = 'published';
      record.publication_id = result.post_id || result.publication_id || '';
      record.publication_tags = payload.tags;
      const key = String(record.id || record.slug || '');
      if (key) delete appState.publicationDrafts[key];
      closePublicationModal();
      showToast('Publicação enviada à fila. O worker fará o upload.', 'ok');
      renderHistory();
      await loadCommunityFeed();
      uiTrace('publication_completed', {status: 200, correlation_id: correlation});
    } catch (errorValue) {
      const error = $('#publicationError');
      if (error) { error.textContent = errorValue.message || 'Não foi possível publicar.'; error.hidden = false; }
      appState.publicationBusy = false;
      if (submit) { submit.disabled = false; submit.textContent = eligibility.published ? 'Atualizar publicação' : 'Publicar'; }
    } finally {
      appState.publicationBusy = false;
      if (submit) { submit.disabled = false; submit.textContent = eligibility.published ? 'Atualizar publicação' : 'Publicar'; }
      uiTrace('loading_cleared', {correlation_id: correlation});
    }
  }
  $('#publicationCancel')?.addEventListener('click', closePublicationModal);
  $('#publicationModalClose')?.addEventListener('click', closePublicationModal);
  $('#publicationModalOverlay')?.addEventListener('click', event => {
    if (event.target === $('#publicationModalOverlay')) closePublicationModal();
  });
  $('#publicationForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    uiTrace('publication_submit_received', {correlation_id: appState.publicationCorrelation});
    if (!appState.publicationRecord) { publicationError('Nenhum capítulo selecionado.', 'missing_record'); return; }
    if (!validatePublicationForm(appState.publicationRecord)) return;
    await publishToCommunity(appState.publicationRecord);
  });

  function clearCommunityObjectUrls() {
    for (const objectUrl of appState.communityObjectUrls) {
      try { URL.revokeObjectURL(objectUrl); } catch (_) { /* best effort */ }
    }
    appState.communityObjectUrls.clear();
  }

  async function openAuthenticatedCommunityPdf(postId, trigger) {
    if (!isCanonicalCommunityAuthenticated()) {
      showToast('Sua sessão expirou. Entre novamente.', 'warn');
      uiTrace('community_pdf_open_failed', {status: 401, code: 'authentication_required'});
      return;
    }
    const viewer = window.open('', '_blank');
    if (!viewer) {
      showToast('O navegador bloqueou a nova aba do PDF.', 'warn');
      return;
    }
    try { viewer.opener = null; } catch (_) { /* best-effort opener isolation */ }
    const originalLabel = trigger?.textContent || 'Abrir PDF';
    if (trigger) { trigger.disabled = true; trigger.textContent = 'Abrindo...'; }
    const correlation = correlationId();
    uiTrace('community_pdf_open_started', {correlation_id: correlation});
    try {
      const response = await api(`/api/community/posts/${encodeURIComponent(postId)}/pdf`, {
        rawResponse: true,
        timeoutMs: 30000,
      });
      const contentType = String(response.headers.get('Content-Type') || '').toLowerCase();
      if (!contentType.includes('application/pdf')) {
        const error = new Error('O arquivo retornado não é um PDF.');
        error.code = 'invalid_pdf_content_type';
        throw error;
      }
      const disposition = String(response.headers.get('Content-Disposition') || '').toLowerCase();
      const cacheControl = String(response.headers.get('Cache-Control') || '').toLowerCase();
      if (!disposition.includes('inline') || !cacheControl.includes('private') || !cacheControl.includes('no-store')) {
        const error = new Error('Os cabeçalhos do PDF não são seguros.');
        error.code = 'invalid_pdf_headers';
        throw error;
      }
      const blob = await response.blob();
      if (!blob.size) {
        const error = new Error('O PDF retornado está vazio.');
        error.code = 'empty_pdf';
        throw error;
      }
      const header = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
      if (String.fromCharCode(...header) !== '%PDF-') {
        const error = new Error('O arquivo retornado não possui assinatura PDF.');
        error.code = 'invalid_pdf_signature';
        throw error;
      }
      const objectUrl = URL.createObjectURL(blob);
      appState.communityObjectUrls.add(objectUrl);
      viewer.location.href = objectUrl;
      try {
        await api(`/api/community/posts/${encodeURIComponent(postId)}/reading-progress`, {
          method: 'PUT',
          body: JSON.stringify({current_page: 1, total_pages: 0, progress_percent: 1}),
          timeoutMs: 8000,
        });
      } catch (_) { /* native PDF viewers do not expose a current page; opening is enough progress evidence */ }
      // Keep the object URL alive while the viewer is open, and revoke it later.
      window.setTimeout(() => {
        try { URL.revokeObjectURL(objectUrl); } catch (_) { /* best effort */ }
        appState.communityObjectUrls.delete(objectUrl);
      }, 300000);
      uiTrace('community_pdf_opened', {status: 200, correlation_id: correlation});
    } catch (errorValue) {
      try { viewer.close(); } catch (_) { /* best effort */ }
      const status = Number(errorValue?.status || 0);
      const message = status === 401
        ? 'Sua sessão expirou. Entre novamente.'
        : status === 403
          ? 'Você não possui acesso a este arquivo.'
          : status === 404
            ? 'O PDF desta publicação não foi encontrado.'
            : errorValue?.code === 'connection_error'
              ? 'Não foi possível abrir o PDF.'
              : 'Não foi possível abrir o PDF.';
      showToast(message, 'warn');
      uiTrace('community_pdf_open_failed', {status, code: errorValue?.code || 'pdf_open_failed', correlation_id: correlation});
    } finally {
      if (trigger) { trigger.disabled = false; trigger.textContent = originalLabel; }
      uiTrace('community_pdf_loading_cleared', {correlation_id: correlation});
    }
  }

  const communityPanels = {
    explore: '#communityFeed',
    favorites: '#communityFavorites',
    reading: '#communityReading',
    mine: '#communityMine',
    notifications: '#communityNotifications',
  };
  function communitySkeleton(container) {
    container.innerHTML = '<div class="skeleton-card"><div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line"></div></div><div class="skeleton-card"><div class="skeleton-line"></div><div class="skeleton-line"></div></div>';
  }
  function setCommunityTab(tab) {
    appState.communityTab = communityPanels[tab] ? tab : 'explore';
    $$('.community-tab').forEach(button => {
      const active = button.dataset.communityTab === appState.communityTab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    $$('.community-tab-panel').forEach(panel => {
      panel.hidden = panel.dataset.communityPanel !== appState.communityTab;
    });
    void loadCommunityFeed();
  }
  function updateCommunityCounters(tab, count) {
    const map = {
      explore: '#communityExploreCount',
      favorites: '#communityFavoritesCount',
      reading: '#communityReadingCount',
      mine: '#communityMineCount',
    };
    if (map[tab] && $(map[tab])) $(map[tab]).textContent = String(count);
  }
  async function loadCommunityFeed() {
    const tab = appState.communityTab || 'explore';
    const container = $(communityPanels[tab] || '#communityFeed');
    if (!container) return;
    communitySkeleton(container);
    try {
      const endpoint = {
        explore: '/api/community/posts',
        favorites: '/api/community/favorites',
        reading: '/api/community/reading-progress',
        mine: '/api/community/my-posts',
        notifications: '/api/community/notifications',
      }[tab];
      const data = await api(endpoint);
      if (!data || (!Array.isArray(data.posts) && !Array.isArray(data.notifications))) {
        const schemaError = new Error('community_schema_invalid');
        schemaError.code = 'community_schema_invalid';
        throw schemaError;
      }
      if (tab === 'notifications') {
        const notifications = data.notifications || [];
        appState.communityCache.notifications = notifications;
        $('#communityNotificationsCount').textContent = String(data.unread || notifications.length || 0);
        container.innerHTML = notifications.length ? notifications.map(renderNotificationCard).join('') : '<div class="empty-real-state">você não possui notificações.</div>';
        return;
      }
      const posts = data.posts || [];
      appState.communityCache[tab] = posts;
      updateCommunityCounters(tab, posts.length);
      if (!posts.length) {
        const empty = {
          explore: 'nenhuma publicação na comunidade ainda',
          favorites: 'Você ainda não adicionou nenhuma obra aos favoritos.',
          reading: 'Nenhuma leitura em andamento.',
          mine: 'Você ainda não publicou nenhuma obra.',
        }[tab] || 'nada para mostrar aqui.';
        container.innerHTML = `<div class="empty-real-state">${escapeHtml(empty)}</div>`;
        return;
      }
      container.innerHTML = posts.map(post => renderCommunityCard(post, {tab})).join('');
      hydrateCommunityAuthorMedia(container);
    } catch (error) {
      const message = error?.code === 'community_schema_invalid'
        ? 'A Comunidade ainda não está disponível neste ambiente.'
        : humanCommunityError(error, 'Não foi possível carregar a Comunidade.');
      container.innerHTML = `<div class="empty-real-state community-error-state"><span>${escapeHtml(message)}</span><button type="button" class="btn-ghost" data-action="retry-community">Tentar novamente</button></div>`;
    }
  }

  $('#communityRefreshBtn')?.addEventListener('click', loadCommunityFeed);
  $('.community-tabs')?.addEventListener('click', event => {
    const button = event.target.closest('[data-community-tab]');
    if (!button) return;
    setCommunityTab(button.dataset.communityTab || 'explore');
  });

  function renderCommunityCard(post, options = {}) {
    const title = post.title || post.series_title || 'Capítulo';
    const sub = `${escapeHtml(post.series_title || '')} · ep ${escapeHtml(String(post.episode_number || ''))} · ${Number(post.views || 0)} leituras`;
    const author = post.author || {};
    const authorName = escapeHtml(author.display_name || 'Usuário');
    const authorRole = author.public_role ? ` · ${escapeHtml(author.public_role)}` : '';
    const initial = escapeHtml((author.display_name || 'U').slice(0, 1).toUpperCase());
    const avatarAttrs = author.avatar_url ? ` data-community-author-avatar-url="${escapeAttr(author.avatar_url)}"` : '';
    const pages = Number(post.page_count || post.pages || 0);
    const quality = post.quality || post.quality_status || post.review_status || '';
    const postId = escapeAttr(post.publication_id || post.post_id || '');
    const favoriteLabel = post.favorited ? 'Remover dos favoritos' : 'Favoritar';
    const reading = post.reading || {};
    const progress = reading.total_pages ? ` · página ${Number(reading.current_page || 0)} de ${Number(reading.total_pages || 0)} · ${Number(reading.progress_percent || 0).toFixed(0)}%` : '';
    const tags = Array.isArray(post.tags) && post.tags.length
      ? `<div class="community-card-tags">${post.tags.slice(0, 6).map(tag => `<span>${escapeHtml(tag)}</span>`).join('')}</div>`
      : '';
    const ownerActions = options.tab === 'mine'
      ? `<button class="btn-ghost danger" type="button" data-community-delete-id="${postId}" aria-label="Excluir publicação própria" title="Excluir publicação">Excluir</button>`
      : '';
    return `<article class="hist-item community-publication-card" data-publication-id="${postId}" tabindex="0">
      <div class="hist-cover community-publication-cover" style="background:#b8557a">${escapeHtml(title.slice(0,1).toUpperCase())}</div>
      <div class="hist-meta"><div class="hm-title">${escapeHtml(title)}</div><div class="hm-sub">${sub}${pages ? ` · ${pages} páginas` : ''}${progress}</div><div class="hm-sub community-author"><span class="community-author-avatar community-author-letter"${avatarAttrs}>${initial}</span><span>${authorName}${authorRole}</span></div>${quality ? `<div class="community-quality-chip">${escapeHtml(String(quality))}</div>` : ''}${tags}<div class="community-card-stats">${Number(post.favorite_count || 0)} favoritos · ${Number(post.comment_count || 0)} comentários</div><div class="community-comments-panel" data-comments-for="${postId}" hidden></div></div>
      <div class="hm-actions"><button class="btn-ghost" type="button" data-community-favorite-id="${postId}" aria-pressed="${post.favorited ? 'true' : 'false'}" aria-label="${escapeAttr(favoriteLabel)}" title="${escapeAttr(favoriteLabel)}">${escapeHtml(favoriteLabel)}</button><button class="btn-ghost" type="button" data-community-comments-id="${postId}" aria-label="Abrir comentários" title="Comentários">Comentários</button><button class="btn-ghost" type="button" data-community-pdf-id="${postId}" aria-label="Abrir PDF da publicação" title="Abrir PDF">Abrir PDF</button>${ownerActions}</div></article>`;
  }
  function renderNotificationCard(item) {
    const title = item.type === 'comment_created' ? 'Novo comentário' : 'Notificação';
    const read = item.read_at ? 'lida' : 'não lida';
    return `<article class="hist-item community-publication-card" data-notification-id="${escapeAttr(item.notification_id || '')}">
      <div class="hist-cover community-publication-cover" style="background:#2f7a6b">N</div>
      <div class="hist-meta"><div class="hm-title">${escapeHtml(title)}</div><div class="hm-sub">${escapeHtml(read)}</div></div>
      <div class="hm-actions">${item.read_at ? '' : `<button class="btn-ghost" type="button" data-notification-read-id="${escapeAttr(item.notification_id || '')}">Marcar lida</button>`}</div>
    </article>`;
  }
  $('#view-community')?.addEventListener('click', event => {
    const retry = event.target.closest('[data-action="retry-community"]');
    if (retry) { void loadCommunityFeed(); return; }
    const favorite = event.target.closest('[data-community-favorite-id]');
    if (favorite && !favorite.disabled) { void toggleCommunityFavorite(favorite.dataset.communityFavoriteId || '', favorite); return; }
    const comments = event.target.closest('[data-community-comments-id]');
    if (comments) { void toggleCommunityComments(comments.dataset.communityCommentsId || ''); return; }
    const deleteButton = event.target.closest('[data-community-delete-id]');
    if (deleteButton) { void deleteOwnPublication(deleteButton.dataset.communityDeleteId || '', deleteButton); return; }
    const readButton = event.target.closest('[data-notification-read-id]');
    if (readButton) { void markNotificationRead(readButton.dataset.notificationReadId || '', readButton); return; }
    const button = event.target.closest('[data-community-pdf-id]');
    if (!button || button.disabled) return;
    void openAuthenticatedCommunityPdf(button.dataset.communityPdfId || '', button);
  });
  $('#view-community')?.addEventListener('submit', event => {
    const form = event.target.closest('[data-comment-form]');
    if (!form) return;
    event.preventDefault();
    void submitCommunityComment(form.dataset.commentForm || '', form);
  });
  async function toggleCommunityFavorite(postId, button) {
    if (!postId) return;
    const pressed = button.getAttribute('aria-pressed') === 'true';
    button.disabled = true;
    button.textContent = pressed ? 'Removendo...' : 'Favoritando...';
    uiTrace('favorite_click_received', {publication_id: postId});
    try {
      await api(`/api/community/publications/${encodeURIComponent(postId)}/favorite`, {method: pressed ? 'DELETE' : 'POST'});
      showToast(pressed ? 'Removido dos favoritos.' : 'Adicionado aos favoritos.', 'ok');
      await loadCommunityFeed();
    } catch (errorValue) {
      showToast(humanCommunityError(errorValue, 'Não foi possível atualizar favorito.'), 'error');
      button.disabled = false;
      button.textContent = pressed ? 'Remover dos favoritos' : 'Favoritar';
    }
  }
  async function toggleCommunityComments(postId) {
    const panel = document.querySelector(`[data-comments-for="${CSS.escape(postId)}"]`);
    if (!panel) return;
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.hidden = false;
    panel.innerHTML = '<div class="community-loading">carregando comentários...</div>';
    uiTrace('comments_click_received', {publication_id: postId});
    try {
      const data = await api(`/api/community/publications/${encodeURIComponent(postId)}/comments`);
      const comments = Array.isArray(data.items) ? data.items : (Array.isArray(data.comments) ? data.comments : []);
      panel.innerHTML = `${comments.length ? comments.map(renderCommunityComment).join('') : '<div class="community-empty">Ainda não há comentários.</div>'}
        <form class="community-comment-form" data-comment-form="${escapeAttr(postId)}"><input name="body" maxlength="1000" placeholder="Escreva um comentário..." required><button type="submit" class="btn-ghost">Comentar</button></form>`;
    } catch (errorValue) {
      panel.innerHTML = `<div class="community-error-state">${escapeHtml(humanCommunityError(errorValue, 'Não foi possível carregar comentários.'))}</div>`;
    }
  }
  function renderCommunityComment(comment) {
    const mine = String(comment.user_id || '') === String(getGlobal('__tradutorCommunityUserId') || '');
    const body = comment.is_deleted ? 'Comentário removido.' : (comment.body || '');
    return `<div class="community-comment" data-comment-id="${escapeAttr(comment.comment_id || '')}"><strong>${escapeHtml(comment.author?.display_name || (mine ? 'Você' : 'Usuário'))}</strong><span>${escapeHtml(body)}</span>${mine && !comment.is_deleted ? `<button type="button" class="btn-ghost danger" data-comment-delete-id="${escapeAttr(comment.comment_id || '')}">Excluir</button>` : ''}</div>`;
  }
  async function submitCommunityComment(postId, form) {
    const input = $('input[name="body"]', form);
    const body = String(input?.value || '').trim();
    if (!body) return;
    const button = $('button', form);
    if (button) { button.disabled = true; button.textContent = 'Enviando...'; }
    try {
      await api(`/api/community/publications/${encodeURIComponent(postId)}/comments`, {method: 'POST', body: JSON.stringify({body})});
      const panel = document.querySelector(`[data-comments-for="${CSS.escape(postId)}"]`);
      if (panel) panel.hidden = true;
      await toggleCommunityComments(postId);
      await loadCommunityFeed();
    } catch (errorValue) {
      showToast(humanCommunityError(errorValue, 'Não foi possível comentar.'), 'error');
    } finally {
      if (button) { button.disabled = false; button.textContent = 'Comentar'; }
    }
  }
  async function deleteCommunityComment(commentId, button) {
    if (!commentId || !window.confirm('Excluir este comentário?')) return;
    button.disabled = true;
    try {
      await api(`/api/community/comments/${encodeURIComponent(commentId)}`, {method: 'DELETE'});
      showToast('Comentário removido.', 'ok');
      await loadCommunityFeed();
    } catch (errorValue) {
      showToast(humanCommunityError(errorValue, 'Não foi possível excluir comentário.'), 'error');
      button.disabled = false;
    }
  }
  $('#view-community')?.addEventListener('click', event => {
    const button = event.target.closest('[data-comment-delete-id]');
    if (!button) return;
    void deleteCommunityComment(button.dataset.commentDeleteId || '', button);
  });
  async function deleteOwnPublication(postId, button) {
    if (!postId || !window.confirm('Remover esta publicação da Comunidade? O PDF local será preservado.')) return;
    button.disabled = true;
    button.textContent = 'Excluindo...';
    try {
      const result = await api(`/api/community/posts/${encodeURIComponent(postId)}`, {method: 'DELETE', body: JSON.stringify({reason: 'user_requested'})});
      showToast(result.code === 'publication_deleted' ? 'Publicação removida da Comunidade.' : 'Publicação já estava removida.', 'ok');
      await loadCommunityFeed();
    } catch (errorValue) {
      showToast(humanCommunityError(errorValue, 'Não foi possível excluir publicação.'), 'error');
      button.disabled = false;
      button.textContent = 'Excluir';
    }
  }
  async function markNotificationRead(notificationId, button) {
    button.disabled = true;
    try {
      await api(`/api/community/notifications/${encodeURIComponent(notificationId)}/read`, {method: 'PATCH'});
      await loadCommunityFeed();
    } catch (errorValue) {
      showToast(humanCommunityError(errorValue, 'Não foi possível marcar a notificação.'), 'error');
      button.disabled = false;
    }
  }

  function hydrateCommunityAuthorMedia(container) {
    $$('[data-community-author-avatar-url]', container).forEach(element => {
      const source = element.dataset.communityAuthorAvatarUrl || '';
      if (!source) return;
      revokeElementMedia(element);
      void loadAuthenticatedMediaElement(element, source, 'image/*', element.textContent || 'U');
    });
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
      ['PDF base', record.pdf_path], ['Pasta', record.output_folder], ['Qualidade', record.quality_report_path],
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
    renderPendingPreviewSurfaces();
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
      const status = record.status === 'error' ? 'erro no processamento' : (record.review_status === 'completed' || record.review_confirmed === true) ? 'revisão concluída' : record.status === 'review_required' || boolish(record.quality_gate) === false ? 'revisão necessária' : record.pdf_path ? 'PDF gerado' : 'tradução concluída';
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
    renderWorkspaceSourcePolicy(settings.workspace_source_policy || {});
  }
  function renderWorkspaceSourcePolicy(policy = {}) {
    const active = policy.status === 'active'
      && policy.all_submitted_sources_authorized === true;
    const toggle = $('#workspaceSourcePolicyToggle');
    if (toggle) {
      toggle.checked = active;
      toggle.setAttribute('aria-checked', String(active));
    }
    const status = $('#workspaceSourcePolicyStatus');
    if (status) {
      const activated = policy.activated_at
        ? new Date(Number(policy.activated_at) * 1000).toLocaleString('pt-BR') : '—';
      status.textContent = active
        ? `Ativado · responsável: workspace local · desde ${activated}`
        : 'Desativado';
    }
    const action = $('#workspaceSourcePolicyAction');
    if (action) {
      action.textContent = active ? 'Revogar política' : 'Ativar política';
      action.classList.toggle('danger', active);
    }
  }
  let pendingWorkspacePolicyState = null;
  function openWorkspacePolicyConfirmation(active) {
    pendingWorkspacePolicyState = Boolean(active);
    const dialog = $('#workspaceSourcePolicyDialog');
    const text = $('#workspaceSourcePolicyDialogText');
    if (!dialog || !text) return;
    text.textContent = active
      ? 'Confirme que este workspace processará somente conteúdo próprio, licenciado, autorizado ou de domínio público.'
      : 'A revogação impedirá novas autorizações automáticas, sem cancelar jobs já iniciados nem apagar o histórico.';
    dialog.hidden = false;
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
    $('#workspaceSourcePolicyConfirm')?.focus();
  }
  function closeWorkspacePolicyConfirmation() {
    const dialog = $('#workspaceSourcePolicyDialog');
    if (dialog?.open) dialog.close();
    if (dialog) dialog.hidden = true;
    pendingWorkspacePolicyState = null;
  }
  $('#workspaceSourcePolicyAction')?.addEventListener('click', () => {
    const current = appState.settings?.workspace_source_policy || {};
    openWorkspacePolicyConfirmation(current.status !== 'active');
  });
  $('#workspaceSourcePolicyToggle')?.addEventListener('click', event => {
    event.preventDefault();
    const current = appState.settings?.workspace_source_policy || {};
    openWorkspacePolicyConfirmation(current.status !== 'active');
  });
  $('#workspaceSourcePolicyCancel')?.addEventListener('click', closeWorkspacePolicyConfirmation);
  $('#workspaceSourcePolicyConfirmForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    if (pendingWorkspacePolicyState === null) return;
    const desired = pendingWorkspacePolicyState;
    const button = $('#workspaceSourcePolicyConfirm');
    if (button) button.disabled = true;
    try {
      const result = await api('/api/ui/source-policy', {
        method: 'POST', body: JSON.stringify({active: desired}),
      });
      appState.settings.workspace_source_policy = result.policy || {};
      renderWorkspaceSourcePolicy(result.policy || {});
      closeWorkspacePolicyConfirmation();
      showToast(desired ? 'Política das fontes ativada.' : 'Política das fontes revogada.', 'ok');
    } catch (error) {
      showToast(error.message || 'Não foi possível alterar a política das fontes.', 'error');
      renderWorkspaceSourcePolicy(appState.settings?.workspace_source_policy || {});
    } finally {
      if (button) button.disabled = false;
    }
  });
  function applyProductSettings(settings = {}) {
    const form = $('#productSettingsForm');
    if (!form) return;
    Object.entries(settings).forEach(([key, value]) => {
      const field = form.elements.namedItem(key);
      if (!field) return;
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else field.value = String(value);
    });
    const language = $('#interfaceLanguageSelect');
    if (language && window.TradutorI18n) {
      language.value = window.TradutorI18n.selectedLanguage();
      window.TradutorI18n.apply(document);
    }
  }
  async function loadProductSettings() {
    if (!isCanonicalCommunityAuthenticated()) return;
    try {
      const result = await api('/api/community/settings');
      applyProductSettings(result.settings || {});
    } catch (_) { /* settings stay at safe defaults */ }
  }
  function collectProductSettings() {
    const form = $('#productSettingsForm');
    const payload = {};
    if (!form) return payload;
    Array.from(form.elements).forEach(field => {
      if (!field.name) return;
      payload[field.name] = field.type === 'checkbox' ? field.checked : field.value;
    });
    return payload;
  }
  $('#productSettingsForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#settingsSaveBtn');
    const error = $('#settingsError');
    if (button) { button.disabled = true; button.textContent = 'Salvando...'; }
    if (error) error.hidden = true;
    try {
      const result = await api('/api/community/settings', {
        method: 'PUT',
        body: JSON.stringify(collectProductSettings()),
      });
      applyProductSettings(result.settings || {});
      showToast('Configurações salvas.', 'ok');
    } catch (errorValue) {
      if (error) { error.textContent = humanCommunityError(errorValue, 'Não foi possível salvar configurações.'); error.hidden = false; }
    } finally {
      if (button) { button.disabled = false; button.textContent = 'Salvar configurações'; }
    }
  });
  $('#interfaceLanguageSelect')?.addEventListener('change', event => {
    window.TradutorI18n?.setLanguage(event.target.value);
  });
  $('#settingsClearSession')?.addEventListener('click', async () => {
    if (!window.confirm('Limpar a sessão atual neste navegador?')) return;
    try {
      await api('/api/community/auth/logout', {method: 'POST', body: JSON.stringify({})});
    } catch (_) {
      /* stale logout may already be complete */
    }
    setGlobal('__tradutorAccessToken', '');
    setGlobal('__tradutorAuthState', 'unauthenticated');
    setGlobal('__tradutorCommunityAuthenticated', false);
    window.dispatchEvent(new CustomEvent('tradutor-auth-changed', {
      detail: {authenticated: false, state: 'unauthenticated'},
    }));
    showToast('Sessão local limpa.', 'ok');
  });
  $('#settingsClearLocalData')?.addEventListener('click', () => {
    if (!window.confirm('Limpar preferências locais deste navegador? Outputs e cache do projeto não serão apagados.')) return;
    try {
      localStorage.removeItem('tradutor.interfaceLanguage');
      sessionStorage.clear();
    } catch (_) { /* storage may be unavailable */ }
    window.TradutorI18n?.setLanguage('auto');
    showToast('Dados locais da interface limpos.', 'ok');
  });
  $('#settingsResetBtn')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/community/settings', {method: 'PUT', body: JSON.stringify({})});
      applyProductSettings(result.settings || {});
      showToast('Configurações restauradas.', 'ok');
    } catch (errorValue) {
      showToast(humanCommunityError(errorValue, 'Não foi possível restaurar configurações.'), 'error');
    }
  });

  /* ---------- local profile ---------- */
  function applyProfileToForm(profile) {
    setGlobal('__tradutorDisplayName', String(profile.display_name || '').trim());
    if (currentCanonicalAuthState() === 'authenticated' && $('#authStatus')) {
      $('#authStatus').textContent = getGlobal('__tradutorDisplayName') || 'Usuário';
    }
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
    revokeElementMedia(element);
    element.innerHTML = '';
    element.style.backgroundImage = '';
    if (source && String(mediaType).startsWith('image/')) {
      if (String(source).startsWith('/api/')) {
        element.textContent = fallback || '';
        void loadAuthenticatedMediaElement(element, source, mediaType, fallback);
      } else {
        const image = document.createElement('img');
        image.src = source; image.alt = '';
        element.appendChild(image);
      }
    } else element.textContent = fallback;
  }
  async function loadAuthenticatedMediaElement(element, source, mediaType, fallback) {
    const requestKey = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    element.dataset.mediaRequest = requestKey;
    try {
      const response = await api(source, {rawResponse: true, timeoutMs: 12000});
      const blob = await response.blob();
      if (!String(blob.type || mediaType || '').startsWith('image/')) throw new Error('invalid_media_type');
      if (element.dataset.mediaRequest !== requestKey) return;
      const objectUrl = URL.createObjectURL(blob);
      appState.communityObjectUrls.add(objectUrl);
      element.dataset.objectUrl = objectUrl;
      const image = document.createElement('img');
      image.src = objectUrl; image.alt = '';
      image.addEventListener('load', () => {
        window.setTimeout(() => {
          try { URL.revokeObjectURL(objectUrl); } catch (_) { /* best effort */ }
          appState.communityObjectUrls.delete(objectUrl);
          if (element.dataset.objectUrl === objectUrl) delete element.dataset.objectUrl;
        }, 300000);
      }, {once: true});
      element.innerHTML = '';
      element.appendChild(image);
      uiTrace('profile_media_loaded', {status: 200});
    } catch (errorValue) {
      if (element.dataset.mediaRequest === requestKey) element.textContent = fallback || '';
      uiTrace('profile_media_load_failed', {
        status: errorValue?.status || 0,
        code: errorValue?.code || errorValue?.message || 'media_load_failed',
      });
    }
  }
  function revokeElementMedia(element) {
    const current = element?.dataset?.objectUrl || '';
    if (!current) return;
    try { URL.revokeObjectURL(current); } catch (_) { /* best effort */ }
    appState.communityObjectUrls.delete(current);
    delete element.dataset.objectUrl;
  }
  function renderProfile(profile = profileFromForm()) {
    const authenticated = String(getGlobal('__tradutorAuthState') || '') === 'authenticated';
    const name = authenticated ? (profile.display_name || 'você') : 'Visitante';
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
    const state = appState.profileMedia?.[kind] || {};
    const source = state.objectUrl || profile[`${kind}_media_url`];
    if (!card) return;
    card.hidden = !source;
    if (!source) return;
    $(`#${kind}MediaName`).textContent = profile[`${kind}_media_name`] || 'mídia local';
    $(`#${kind}MediaMeta`).textContent = `${formatBytes(profile[`${kind}_media_size`])} · ${profile[`${kind}_media_type`] || 'arquivo'}`;
    setMedia($(`#${kind}MediaPreview`), source, profile[`${kind}_media_type`], '');
  }
  function revokeProfileMediaPreview(kind) {
    const state = appState.profileMedia?.[kind];
    if (!state?.objectUrl) return;
    try { URL.revokeObjectURL(state.objectUrl); } catch (_) { /* best effort */ }
    state.objectUrl = '';
  }
  function mergeProfileMediaPayload(kind, profile = {}) {
    appState.profile = {...appState.profile};
    for (const key of ['path', 'type', 'name', 'size', 'updated_at', 'url']) {
      const fullKey = `${kind}_media_${key}`;
      if (Object.prototype.hasOwnProperty.call(profile, fullKey)) appState.profile[fullKey] = profile[fullKey];
    }
    if (kind === 'avatar' && Object.prototype.hasOwnProperty.call(profile, 'avatar_mode')) {
      appState.profile.avatar_mode = profile.avatar_mode;
    }
    if (kind === 'banner' && Object.prototype.hasOwnProperty.call(profile, 'banner')) {
      appState.profile.banner = profile.banner;
    }
  }
  async function uploadProfileMedia(kind, file) {
    if (!file) return;
    if (!['avatar', 'banner'].includes(kind)) return;
    const allowed = new Set(['image/png','image/jpeg','image/webp']);
    if (!allowed.has(file.type) || file.size > 12 * 1024 * 1024) {
      showToast('Use PNG, JPG ou WEBP de até 12 MB.', 'error');
      return;
    }
    const state = appState.profileMedia[kind];
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    state.requestId = correlationId();
    const requestId = state.requestId;
    revokeProfileMediaPreview(kind);
    if (typeof URL?.createObjectURL === 'function') {
      state.objectUrl = URL.createObjectURL(file);
      renderMediaCard(kind, {
        ...appState.profile,
        [`${kind}_media_url`]: state.objectUrl,
        [`${kind}_media_name`]: file.name,
        [`${kind}_media_size`]: file.size,
        [`${kind}_media_type`]: file.type,
      });
    }
    uiTrace('profile_media_upload_started', {kind, request_id: requestId});
    const timeout = window.setTimeout(() => state.controller?.abort(), 20000);
    try {
      const payload = await api(`/api/ui/profile/media/${kind}?filename=${encodeURIComponent(file.name)}&content_type=${encodeURIComponent(file.type)}`, {
        method: 'POST', headers: {'Content-Type': file.type}, body: file, signal: state.controller.signal,
      });
      if (state.requestId !== requestId) {
        uiTrace('stale_response_discarded', {kind, request_id: requestId});
        return;
      }
      revokeProfileMediaPreview(kind);
      mergeProfileMediaPayload(kind, payload.profile || {});
      applyProfileToForm(appState.profile);
      showToast(kind === 'avatar' ? 'Avatar salvo neste computador.' : 'Banner salvo neste computador.', 'ok');
    } finally {
      window.clearTimeout(timeout);
      if (state.requestId === requestId) state.controller = null;
    }
  }
  async function removeProfileMedia(kind) {
    if (!['avatar', 'banner'].includes(kind)) return;
    if (!window.confirm('Remover esta imagem do perfil?')) return;
    const state = appState.profileMedia[kind];
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    state.requestId = correlationId();
    const requestId = state.requestId;
    uiTrace('profile_media_remove_started', {kind, request_id: requestId});
    try {
      const payload = await api(`/api/ui/profile/media/${kind}`, {method:'DELETE', timeoutMs: 12000});
      if (state.requestId !== requestId) {
        uiTrace('stale_response_discarded', {kind, request_id: requestId});
        return;
      }
      revokeProfileMediaPreview(kind);
      mergeProfileMediaPayload(kind, payload.profile || {});
      applyProfileToForm(appState.profile);
      showToast('Mídia removida.', 'ok');
    } finally {
      if (state.requestId === requestId) state.controller = null;
    }
  }
  function bindDropzone(kind) {
    const dropzone = $(`#${kind}ImagePicker`);
    const input = $(`#${kind}ImageInput`);
    if (!dropzone || !input) return;
    input.addEventListener('change', async () => {
      try { await uploadProfileMedia(kind, input.files?.[0]); } catch (error) { showToast(humanCommunityError(error, 'Não foi possível salvar a mídia.'), 'error'); }
      input.value = '';
    });
    ['dragenter','dragover'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add('dragging'); }));
    ['dragleave','drop'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove('dragging'); }));
    dropzone.addEventListener('drop', async event => {
      try { await uploadProfileMedia(kind, event.dataTransfer?.files?.[0]); } catch (error) { showToast(humanCommunityError(error, 'Não foi possível salvar a mídia.'), 'error'); }
    });
    dropzone.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); } });
    $(`#${kind}MediaReplace`)?.addEventListener('click', () => input.click());
    $(`#${kind}MediaRemove`)?.addEventListener('click', async () => { try { await removeProfileMedia(kind); } catch (error) { showToast(humanCommunityError(error, 'Não foi possível remover a mídia.'), 'error'); } });
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

  /* ---------- modal and copy ---------- */
  $('#visitorModalClose')?.addEventListener('click', () => $('#visitorModalOverlay').classList.remove('open'));
  $('#visitorModalOverlay')?.addEventListener('click', event => { if (event.target.id === 'visitorModalOverlay') event.currentTarget.classList.remove('open'); });
  $('#copyRepo')?.addEventListener('click', async event => {
    try { await navigator.clipboard.writeText(event.currentTarget.dataset.copy || ''); showToast('Copiado.', 'ok'); }
    catch (_) { showToast('Não foi possível copiar.', 'error'); }
  });
  document.addEventListener('keydown', event => {
    const tag = document.activeElement?.tagName || '';
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
    const index = Number(event.key) - 1;
    const tabs = $$('.rail-tab');
    if (index >= 0 && index < tabs.length) activateTab(tabs[index].dataset.tab);
  });

  /* ---------- data lifecycle ---------- */
  function applyStandaloneSourceReady(data) {
    if (!data?.source_ready && data?.standalone_source_ready && !data?.active
        && !data?.source_review && !data?.pending) {
      data.source_ready = data.standalone_source_ready;
      data.status = 'source_analysis_ready';
    }
    return data;
  }
  async function refreshBootstrap() {
    if (bootVisualTest) return;
    try {
      setBootStage(1);
      const data = await api(`/api/ui/bootstrap?cursor=${appState.cursor}`);
      setBootStage(2);
      appState.bootstrap = data;
      appState.history = Array.isArray(data.history) ? data.history : [];
      const authState = syncCanonicalAuthFromBootstrap(data);
      setBootStage(3);
      const authenticated = authState === 'authenticated';
      appState.profile = authenticated ? (data.profile || {}) : {};
      setBootStage(4);
      setGlobal('__tradutorDisplayName', authenticated ? String(appState.profile.display_name || '').trim() : '');
      appState.settings = data.settings || {};
      applyStandaloneSourceReady(data);
      setBootStage(5);
      appState.historyRevision = data.history_revision || appState.historyRevision;
      renderSettings(appState.settings);
      applyCanonicalAuthSurface(authState);
      if (authenticated) {
        applyProfileToForm(appState.profile);
        void loadProductSettings();
      }
      await loadPendingHumanPreviews();
      renderHistory();
      renderDashboard();
      if (!appState.reviewRestoreAttempted) {
        appState.reviewRestoreAttempted = true;
        restoreReviewModeFromUrl();
      }
      setBootStage(6);
      renderRuntime(data);
      rememberRuntimeTerminalState(data);
      setBootStage(7);
      window.clearTimeout(bootTimer);
      window.setTimeout(closeBoot, 250);
    } catch (error) {
      window.clearTimeout(bootTimer);
      setBootFailed('Não foi possível carregar a interface local.');
      showToast(`Interface local: ${error.message}`, 'error');
    }
  }
  async function pollState() {
    if (appState.polling || document.hidden) return;
    appState.polling = true;
    try {
      const data = await api(`/api/ui/state?cursor=${appState.cursor}`);
      applyStandaloneSourceReady(data);
      renderRuntime(data);
      handleTerminalRuntimeTransition(data);
      appState.cursor = Math.max(appState.cursor, Number(data.log_cursor || 0));
      document.documentElement.dataset.uiPollCount = String(Number(document.documentElement.dataset.uiPollCount || 0) + 1);
      uiTrace('EVENT_CURSOR_ADVANCED', {status: appState.status});
    } catch (error) {
      $('#appStatus').textContent = 'sem conexão';
      $('#appStatus').dataset.state = 'error';
    } finally { appState.polling = false; }
  }
  refreshBootstrap();
  if (getGlobal('__tradutorUiPollingTimer')) {
    window.clearInterval(getGlobal('__tradutorUiPollingTimer'));
    uiTrace('POLLING_DUPLICATE_BLOCKED', {status: appState.status});
  }
  setGlobal('__tradutorUiPollingTimer', window.setInterval(pollState, 850));
  document.documentElement.dataset.tradutorUiReady = '1';
  uiTrace('POLLING_STARTED', {status: appState.status});
  window.addEventListener('beforeunload', () => {
    window.clearInterval(getGlobal('__tradutorUiPollingTimer'));
    setGlobal('__tradutorUiPollingTimer', 0);
    uiTrace('POLLING_STOPPED', {status: appState.status});
    cancelAnimationFrame(animationFrame);
  }, {once: true});
  window.addEventListener('unhandledrejection', event => {
    const reason = event.reason || {};
    showToast(reason.message || 'Uma operação não foi concluída.', 'error');
  });
})();
