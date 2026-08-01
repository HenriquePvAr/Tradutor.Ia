/**
 * Maps a job's real state onto a loading view.
 *
 * The reference composition for this screen shows a percentage, elapsed times
 * per step and a system log. None of those may be invented here:
 *
 *   - a percentage appears only when the backend reported countable work;
 *   - a duration appears only when two real timestamps can be subtracted;
 *   - an event line appears only when the backend supplied a sanitised one.
 *
 * Everything else renders as indeterminate, blank, or absent. The module knows
 * nothing about a title, a URL, an episode, an owner or a job id, so the same
 * mapping serves any job.
 */
(function (global) {
  'use strict';

  var VIEW_VERSION = '1';

  // Two modes share the component. Bootstrap is short and has no pipeline.
  var MODE_BOOTSTRAP = 'bootstrap';
  var MODE_PIPELINE = 'pipeline';

  var STATUS_PENDING = 'pending';
  var STATUS_ACTIVE = 'active';
  var STATUS_DONE = 'completed';
  var STATUS_REVIEW = 'review_required';
  var STATUS_FAILED = 'failed';
  var STATUS_CANCELLED = 'cancelled';

  // Short groups for the bottom band. Ordered as the pipeline runs, not as a
  // layout preference.
  var PIPELINE_GROUPS = [
    { key: 'preparation', label: 'Preparação', stages: ['source_analysis', 'awaiting_source_review'] },
    { key: 'pages', label: 'Páginas', stages: ['download'] },
    { key: 'ocr', label: 'OCR', stages: ['validation', 'ocr'] },
    { key: 'translation', label: 'Tradução', stages: ['translate'] },
    { key: 'reconstruction', label: 'Reconstrução', stages: ['render'] },
    { key: 'document', label: 'PDF', stages: ['pdf'] },
    { key: 'review', label: 'Revisão', stages: ['quality_review'] }
  ];

  var BOOTSTRAP_GROUPS = [
    { key: 'session', label: 'Sessão' },
    { key: 'environment', label: 'Ambiente' },
    { key: 'interface', label: 'Interface' }
  ];

  function isNumber(value) {
    return typeof value === 'number' ? Number.isFinite(value)
      : (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value)));
  }

  function humanize(key) {
    var text = String(key == null ? '' : key).trim().replace(/[_\-.]+/g, ' ').replace(/\s+/g, ' ');
    if (!text) return '';
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  /**
   * A percentage only from countable work the backend actually reported.
   *
   * Deliberately refuses a time-derived guess: elapsed seconds say nothing
   * about how much is left, and a bar built from them is a fabrication.
   */
  function resolveProgress(source) {
    var data = source || {};
    if (data.indeterminate === true) {
      return { mode: 'indeterminate', percent: null, current: null, total: null,
               unit: '', source: 'backend_indeterminate' };
    }
    // The runtime payload reports countable work as current/total, with an
    // optional fraction. Nothing else is a denominator: pages and groups are
    // display counters that the backend already folds into current.
    var pairs = [
      ['', data.current, data.total],
      ['etapas', data.completed_stages, data.total_stages]
    ];
    for (var i = 0; i < pairs.length; i += 1) {
      var unit = pairs[i][0], done = pairs[i][1], total = pairs[i][2];
      if (isNumber(done) && isNumber(total) && Number(total) > 0 && Number(done) >= 0) {
        var pct = Math.max(0, Math.min(100, Math.round((Number(done) / Number(total)) * 100)));
        return { mode: 'determinate', percent: pct, current: Number(done),
                 total: Number(total), unit: unit, source: 'counts' };
      }
    }
    if (isNumber(data.fraction) && Number(data.fraction) >= 0 && Number(data.fraction) <= 1) {
      return { mode: 'determinate', percent: Math.round(Number(data.fraction) * 100),
               current: null, total: null, unit: '', source: 'fraction' };
    }
    return { mode: 'indeterminate', percent: null, current: null, total: null,
             unit: '', source: 'no_data' };
  }

  /**
   * A duration only from two real timestamps. No timestamps, no number — the
   * reference screen's per-step times are exactly what must not be guessed.
   */
  function resolveDuration(startedAt, endedAt) {
    if (!startedAt || !endedAt) return { seconds: null, label: '', source: 'unavailable' };
    var start = new Date(startedAt).getTime();
    var end = new Date(endedAt).getTime();
    if (isNaN(start) || isNaN(end) || end < start) {
      return { seconds: null, label: '', source: 'unusable_timestamps' };
    }
    var seconds = Math.round((end - start) / 1000);
    return { seconds: seconds, label: seconds + 's', source: 'timestamps' };
  }

  /**
   * Only events the backend already sanitised are shown, and each is checked
   * again here: a line carrying anything credential-shaped is dropped rather
   * than displayed.
   */
  var UNSAFE = /(authorization|bearer|cookie|csrf|token|senha|password|api[_-]?key|secret|owner_id|traceback|[A-Za-z]:\\|\/home\/|\.env)/i;

  /**
   * Normalises the runtime's log entries, whose real shape is
   * `{seq, time, kind, text}`, already sanitised server-side. This is a second
   * pass, not the only one: a line that still carries anything
   * credential-shaped is dropped rather than displayed.
   */
  function sanitiseEvents(events) {
    if (!Array.isArray(events)) return [];
    return events.filter(function (event) {
      if (!event || typeof event !== 'object') return false;
      var blob = [event.text, event.message, event.stage, event.status,
                  event.kind, event.reason_code].join(' ');
      return !UNSAFE.test(blob);
    }).map(function (event) {
      return {
        seq: isNumber(event.seq) ? Number(event.seq) : null,
        at: String(event.time || event.at || ''),
        stage: String(event.stage || ''),
        status: String(event.kind || event.status || ''),
        message: String(event.text || event.message || ''),
        reasonCode: String(event.reason_code || event.reasonCode || '')
      };
    }).filter(function (event) {
      // An entry with nothing to say is noise, not evidence.
      return event.message !== '' || event.reasonCode !== '';
    }).slice(-30);
  }

  /** Where each visual group stands, derived from the active stage. */
  function resolveGroups(activeStage, status, groups) {
    var order = groups || PIPELINE_GROUPS;
    var activeIndex = -1;
    order.forEach(function (group, index) {
      if (Array.isArray(group.stages) && group.stages.indexOf(String(activeStage)) >= 0) {
        activeIndex = index;
      }
    });
    var terminalOk = status === 'finished' || status === 'review_completed';
    var review = status === STATUS_REVIEW;
    var failed = status === STATUS_FAILED || status === 'error';
    var cancelled = status === STATUS_CANCELLED;

    return order.map(function (group, index) {
      var state;
      if (terminalOk || review) state = STATUS_DONE;
      else if (activeIndex < 0) state = STATUS_PENDING;
      else if (index < activeIndex) state = STATUS_DONE;
      else if (index > activeIndex) state = STATUS_PENDING;
      else if (failed) state = STATUS_FAILED;
      else if (cancelled) state = STATUS_CANCELLED;
      else state = STATUS_ACTIVE;
      if (review && group.key === 'review') state = STATUS_REVIEW;
      return { key: group.key, label: group.label, status: state, index: index + 1 };
    });
  }

  var STAGE_COPY = {
    source_analysis: ['Validando a origem', 'Verificando se a página está disponível e compatível.'],
    awaiting_source_review: ['Aguardando sua confirmação', 'Revise as páginas encontradas para continuar.'],
    download: ['Obtendo páginas', 'Baixando as páginas autorizadas.'],
    validation: ['Identificando regiões', 'Detectando áreas que podem conter texto.'],
    ocr: ['Identificando textos', 'Lendo o conteúdo das regiões detectadas.'],
    translate: ['Traduzindo', 'Preparando o texto em português brasileiro.'],
    render: ['Reconstruindo páginas', 'Aplicando o texto traduzido às páginas.'],
    pdf: ['Gerando PDF', 'Montando o documento final.'],
    quality_review: ['Verificando qualidade', 'Executando as verificações finais.']
  };

  var TERMINAL_COPY = {
    finished: ['Tradução processada', 'Seu capítulo foi processado.', 'success'],
    review_completed: ['Revisão concluída', 'Os itens pendentes foram resolvidos.', 'success'],
    review_required: ['Tradução processada', 'Alguns trechos precisam da sua revisão.', 'review'],
    failed: ['A operação não pôde continuar', '', 'failed'],
    error: ['A operação não pôde continuar', '', 'failed'],
    cancelled: ['Operação cancelada', 'Nada foi aplicado.', 'neutral']
  };

  /**
   * The whole loading view for a job state. Pure: no fetching, no timers.
   */
  function mapJobStateToLoadingView(jobState) {
    var state = jobState || {};
    var mode = state.mode === MODE_BOOTSTRAP ? MODE_BOOTSTRAP : MODE_PIPELINE;
    var status = String(state.status || '').toLowerCase();
    var stage = String(state.stage || '');
    var progress = resolveProgress(state.progress);
    // A completed bootstrap means only that session/environment/interface are ready.
    // Terminal translation wording belongs exclusively to a real pipeline job.
    var terminal = mode === MODE_PIPELINE ? TERMINAL_COPY[status] : null;

    var title, description, tone;
    if (terminal) {
      title = terminal[0];
      // A failure description comes from the backend's sanitised message only.
      description = terminal[1] || String(state.message || '');
      tone = terminal[2];
    } else if (mode === MODE_BOOTSTRAP) {
      title = status === 'finished' ? 'Tradutor.IA pronto' : 'Preparando o Tradutor.IA';
      description = status === 'finished'
        ? 'Sessão, ambiente e interface preparados.'
        : 'Restaurando sua sessão e preparando o ambiente.';
      tone = status === 'finished' ? 'success' : 'progress';
    } else if (STAGE_COPY[stage]) {
      title = STAGE_COPY[stage][0];
      description = STAGE_COPY[stage][1];
      tone = 'progress';
    } else if (stage) {
      // An unfamiliar stage still reads, without pretending to explain itself.
      title = humanize(stage);
      description = 'Etapa reportada pelo processamento.';
      tone = 'progress';
    } else {
      title = 'Preparando';
      description = '';
      tone = 'neutral';
    }

    var groups = mode === MODE_BOOTSTRAP
      ? resolveGroups(stage, status, BOOTSTRAP_GROUPS.map(function (g) {
          return { key: g.key, label: g.label, stages: [g.key] };
        }))
      : resolveGroups(stage, status, PIPELINE_GROUPS);

    var duration = resolveDuration(state.started_at, state.updated_at);

    // Languages come from the job. Absent, nothing is shown — never a guessed
    // flag or a default source language.
    var languages = null;
    if (state.source_language || state.target_language) {
      languages = {
        source: String(state.source_language || '').toUpperCase() || null,
        target: String(state.target_language || '').toUpperCase() || null
      };
    }

    return {
      viewVersion: VIEW_VERSION,
      mode: mode,
      status: status,
      stage: stage,
      knownStage: Boolean(STAGE_COPY[stage]),
      title: title,
      description: description,
      tone: tone,
      progress: progress,
      // Bootstrap may expose real stage counts and a proportional bar, but a
      // large isolated percentage looks like chapter-processing progress.
      showPercent: mode !== MODE_BOOTSTRAP && progress.mode === 'determinate',
      isTerminal: Boolean(terminal),
      needsReview: status === STATUS_REVIEW,
      failed: status === STATUS_FAILED || status === 'error',
      cancelled: status === STATUS_CANCELLED,
      reasonCode: UNSAFE.test(String(state.reason_code || ''))
        ? '' : String(state.reason_code || ''),
      duration: duration,
      groups: groups,
      events: sanitiseEvents(state.events),
      languages: languages,
      pendingReviewCount: isNumber(state.pending_review_count)
        ? Number(state.pending_review_count) : null,
      // Environment facts, passed through rather than sniffed: the renderer must
      // never infer "local test" from a hostname or a port.
      localTest: Boolean(state.local_test),
      reducedMotion: Boolean(state.reduced_motion),
      activeGroupKey: (function () {
        var active = null;
        (PIPELINE_GROUPS.concat(BOOTSTRAP_GROUPS)).forEach(function (group) {
          if (Array.isArray(group.stages) && group.stages.indexOf(stage) >= 0) active = group.key;
          else if (!group.stages && group.key === stage) active = group.key;
        });
        return active;
      })(),
      // A result is offered only when the backend says one exists.
      hasResult: Boolean(state.result_available),
      hasPdf: Boolean(state.pdf_available),
      canRetry: Boolean(state.retry_available),
      canOpenReview: Boolean(state.review_available),
      ariaLabel: title + (description ? '. ' + description : '')
    };
  }

  var api = {
    VIEW_VERSION: VIEW_VERSION,
    MODE_BOOTSTRAP: MODE_BOOTSTRAP,
    MODE_PIPELINE: MODE_PIPELINE,
    PIPELINE_GROUPS: PIPELINE_GROUPS,
    BOOTSTRAP_GROUPS: BOOTSTRAP_GROUPS,
    resolveProgress: resolveProgress,
    resolveDuration: resolveDuration,
    sanitiseEvents: sanitiseEvents,
    resolveGroups: resolveGroups,
    humanize: humanize,
    mapJobStateToLoadingView: mapJobStateToLoadingView
  };

  if (typeof module === 'object' && module.exports) module.exports = api;
  global.TradutorLoadingView = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
