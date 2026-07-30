/**
 * Visual-only pipeline surface harness.
 *
 * Loaded by the server exclusively when TRADUTOR_UI_VISUAL_TEST=1. It is also
 * fail-closed in the browser: loopback, the explicit server flag and an
 * allow-listed query state are all required. It performs no request, starts no
 * worker and persists nothing.
 */
(function () {
  'use strict';

  var loopback = ['127.0.0.1', 'localhost', '::1'].indexOf(window.location.hostname) >= 0;
  if (window.__tradutorVisualTestEnabled !== true || !loopback) return;

  var params = new URLSearchParams(window.location.search);
  var requested = params.get('visual_pipeline_state');
  var reducedMotion = params.get('visual_reduced_motion') === '1';
  var fixtures = {
    indeterminate: {status: 'running', stage: 'source_analysis'},
    determinate: {
      status: 'running', stage: 'download', progress: {current: 8, total: 12},
      started_at: '2026-01-01T10:00:00Z', updated_at: '2026-01-01T10:00:09Z'
    },
    ocr: {status: 'running', stage: 'ocr', progress: {current: 3, total: 10}},
    translation: {
      status: 'running', stage: 'translate', progress: {current: 8, total: 12},
      source_language: 'en', target_language: 'pt-br',
      events: [
        {seq: 21, time: '10:00:01', kind: 'completed', text: 'Leitura concluída.'},
        {seq: 22, time: '10:00:02', kind: 'active', text: 'Tradução em andamento.'}
      ]
    },
    reconstruction: {status: 'running', stage: 'render', progress: {current: 7, total: 12}},
    pdf: {status: 'running', stage: 'pdf', progress: {current: 11, total: 12}},
    review: {
      status: 'review_required', stage: 'quality_review', pending_review_count: 3,
      review_available: true
    },
    completed: {
      status: 'finished', stage: 'pdf', progress: {current: 12, total: 12},
      result_available: true, pdf_available: true
    },
    failed: {
      status: 'failed', stage: 'ocr', reason_code: 'ocr_input_unavailable',
      message: 'Não foi possível continuar a leitura do texto.'
    },
    recoverable: {
      status: 'failed', stage: 'download', reason_code: 'temporary_source_failure',
      message: 'A origem não respondeu dentro do prazo.', retry_available: true
    },
    cancelled: {status: 'cancelled', stage: 'translate'},
    events: {
      status: 'running', stage: 'translate', progress: {current: 8, total: 12},
      events: [
        {seq: 31, time: '10:01:01', kind: 'completed', text: 'OCR concluído.'},
        {seq: 32, time: '10:01:02', kind: 'active', text: '<etapa> tratada como texto.'},
        {seq: 33, time: '10:01:03', kind: 'debug', text: 'Authorization: Bearer removido'},
        {seq: 34, time: '10:01:04', kind: 'debug', text: 'C:\\Users\\privado\\.env'}
      ]
    }
  };
  if (!Object.prototype.hasOwnProperty.call(fixtures, requested)) return;

  function renderHarness() {
    var root = document.getElementById('pipelineVisualHarnessRoot');
    var mapper = window.TradutorLoadingView;
    var renderer = window.TradutorProcessingSurface;
    if (!mapper || !renderer) return;
    if (!root) {
      root = document.createElement('section');
      root.id = 'pipelineVisualHarnessRoot';
      root.className = 'ls-surface';
      root.setAttribute('aria-label', 'Estado visual do processamento');
      document.body.appendChild(root);
    }
    var state = Object.assign({
      mode: 'pipeline', local_test: true,
      reduced_motion: reducedMotion || window.matchMedia('(prefers-reduced-motion: reduce)').matches
    }, fixtures[requested]);
    if (reducedMotion) document.documentElement.dataset.pipelineReducedMotion = '1';
    document.documentElement.dataset.pipelineVisualHarness = requested;
    root.dataset.pipelineVisualHarness = requested;
    renderer.renderProcessingSurface(root, mapper.mapJobStateToLoadingView(state));
  }

  // Three real signals, no timers. The two timers that used to stand in here
  // (50ms and 1000ms) existed to catch the moment the canonical UI settled,
  // because the auth listener below was subscribed to 'tradutor:auth-changed'
  // while the application has only ever dispatched 'tradutor-auth-changed'.
  // That subscription never fired, so the guesswork was the only thing keeping
  // the harness working. With the right event name the signal is exact and
  // guessing an interval is unnecessary.
  //
  // renderHarness returns early while the mapper or the renderer is missing, so
  // an early call is never a failure: a later signal renders it. It is also
  // idempotent - it reuses its own root and replaces its contents - so the
  // three signals cannot stack up duplicates.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderHarness, {once: true});
  } else {
    renderHarness();
  }
  window.addEventListener('load', renderHarness, {once: true});
  window.addEventListener('tradutor-auth-changed', renderHarness);
})();
