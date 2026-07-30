/**
 * Renders the loading surface from a view model. Nothing else.
 *
 * Every label, number, tone and available action arrives already decided by
 * loading_view.js. This module does not compute progress, duration, stage,
 * status or permissions — if it did, there would be two sources of truth and
 * they would eventually disagree.
 *
 * All dynamic text goes through textContent. innerHTML is never used with a
 * value that came from the backend; the only markup this module writes is the
 * static SVG it owns.
 */
(function (global) {
  'use strict';

  var SURFACE_VERSION = '2';
  var SVG_NS = 'http://www.w3.org/2000/svg';

  function doc() {
    return global.document;
  }

  function el(tag, className, text) {
    var node = doc().createElement(tag);
    if (className) node.className = className;
    if (text != null && text !== '') node.textContent = String(text);
    return node;
  }

  function decorative(node) {
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  /** Static, code-owned SVG. No backend value ever reaches these. */
  function svg(viewBox, paths, className) {
    var node = doc().createElementNS(SVG_NS, 'svg');
    node.setAttribute('viewBox', viewBox);
    node.setAttribute('fill', 'none');
    node.setAttribute('stroke', 'currentColor');
    node.setAttribute('stroke-width', '1.6');
    node.setAttribute('stroke-linecap', 'round');
    node.setAttribute('stroke-linejoin', 'round');
    if (className) node.setAttribute('class', className);
    node.setAttribute('aria-hidden', 'true');
    paths.forEach(function (d) {
      var path = doc().createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      node.appendChild(path);
    });
    return node;
  }

  var ICONS = {
    search: ['M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z', 'M16 16l4 4'],
    list: ['M8 6h13', 'M8 12h13', 'M8 18h13', 'M3 6h.01', 'M3 12h.01', 'M3 18h.01'],
    download: ['M12 3v12', 'M7 11l5 5 5-5', 'M4 20h16'],
    frame: ['M4 8V5a1 1 0 0 1 1-1h3', 'M20 8V5a1 1 0 0 0-1-1h-3',
            'M4 16v3a1 1 0 0 0 1 1h3', 'M20 16v3a1 1 0 0 1-1 1h-3'],
    text: ['M5 7h14', 'M9 7v10', 'M7 17h4'],
    translate: ['M5 8h9', 'M9.5 5v3c0 3-2 5.5-5 6.5', 'M7 8c0 2 2 4.5 6 6',
                'M13 21l4-9 4 9', 'M14.5 18h5'],
    brush: ['M4 20c0-3 2-4 4-4s3 1 3 3-1 2-3 2H4z', 'M11 16L20 5l-2-2-11 9'],
    document: ['M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z', 'M14 3v5h5'],
    shield: ['M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z', 'M9 12l2 2 4-4'],
    spark: ['M12 3v4', 'M12 17v4', 'M3 12h4', 'M17 12h4']
  };

  // Which icon belongs to which domain group. A visual detail, not a rule.
  var GROUP_ICONS = {
    preparation: 'search', pages: 'download', ocr: 'text', translation: 'translate',
    reconstruction: 'brush', document: 'document', review: 'shield',
    session: 'shield', environment: 'frame', interface: 'spark'
  };

  var STATE_WORDS = {
    completed: 'concluído',
    active: 'em andamento',
    pending: 'aguardando',
    review_required: 'requer revisão',
    failed: 'falhou',
    cancelled: 'cancelado'
  };

  /* ---------- decorative illustration (original, code-drawn) ---------- */

  /**
   * An abstract comic page: frame, speech balloon, detection boxes, halftone.
   * Entirely geometric — no character, no external asset, no reference art.
   */
  function pagePlate(label, tone) {
    var plate = el('div', 'ls-plate');
    plate.dataset.tone = tone || 'neutral';

    var tag = el('span', 'ls-plate-tag', label);
    plate.appendChild(tag);

    var art = decorative(el('div', 'ls-plate-art'));
    var frame = doc().createElementNS(SVG_NS, 'svg');
    frame.setAttribute('viewBox', '0 0 120 160');
    frame.setAttribute('class', 'ls-plate-svg');
    frame.setAttribute('aria-hidden', 'true');

    // Panel borders.
    [[6, 6, 108, 62], [6, 74, 50, 80], [62, 74, 52, 80]].forEach(function (r) {
      var rect = doc().createElementNS(SVG_NS, 'rect');
      rect.setAttribute('x', r[0]); rect.setAttribute('y', r[1]);
      rect.setAttribute('width', r[2]); rect.setAttribute('height', r[3]);
      rect.setAttribute('rx', '3');
      rect.setAttribute('class', 'ls-panel');
      frame.appendChild(rect);
    });
    // Speed lines.
    for (var i = 0; i < 5; i += 1) {
      var line = doc().createElementNS(SVG_NS, 'line');
      line.setAttribute('x1', 10); line.setAttribute('y1', 88 + i * 9);
      line.setAttribute('x2', 46); line.setAttribute('y2', 84 + i * 9);
      line.setAttribute('class', 'ls-speed');
      frame.appendChild(line);
    }
    // Halftone dots.
    for (var r = 0; r < 4; r += 1) {
      for (var c = 0; c < 5; c += 1) {
        var dot = doc().createElementNS(SVG_NS, 'circle');
        dot.setAttribute('cx', 70 + c * 9); dot.setAttribute('cy', 96 + r * 9);
        dot.setAttribute('r', '1.4');
        dot.setAttribute('class', 'ls-halftone');
        frame.appendChild(dot);
      }
    }
    // Balloon.
    var balloon = doc().createElementNS(SVG_NS, 'ellipse');
    balloon.setAttribute('cx', '60'); balloon.setAttribute('cy', '32');
    balloon.setAttribute('rx', '44'); balloon.setAttribute('ry', '20');
    balloon.setAttribute('class', 'ls-balloon');
    frame.appendChild(balloon);
    var tail = doc().createElementNS(SVG_NS, 'path');
    tail.setAttribute('d', 'M44 49 L40 60 L54 50 Z');
    tail.setAttribute('class', 'ls-balloon');
    frame.appendChild(tail);
    art.appendChild(frame);

    plate.appendChild(art);
    return plate;
  }

  function previewPanel(view) {
    var panel = el('section', 'ls-panel-box ls-preview');
    panel.appendChild(el('h3', 'ls-panel-title', 'Prévia da tradução'));

    var pair = el('div', 'ls-plate-pair');
    // Generic labels are used only when the view model has no language.
    // The geometric plates carry no sample dialogue or factual metadata.
    var sourceLabel = view.languages && view.languages.source
      ? view.languages.source : 'Original';
    var targetLabel = view.languages && view.languages.target
      ? view.languages.target : 'Tradução';
    pair.appendChild(pagePlate(sourceLabel, 'neutral'));
    var arrow = decorative(el('div', 'ls-plate-arrow'));
    arrow.appendChild(svg('0 0 24 24', ['M4 12h14', 'M13 6l6 6-6 6']));
    pair.appendChild(arrow);
    pair.appendChild(pagePlate(targetLabel, 'accent'));
    panel.appendChild(pair);

    var chips = el('ul', 'ls-chips');
    [['Detecção', 'frame'], ['OCR', 'text'], ['Tradução', 'translate'],
     ['Reconstrução', 'brush']].forEach(function (pair2) {
      var item = el('li', 'ls-chip');
      item.appendChild(svg('0 0 24 24', ICONS[pair2[1]]));
      item.appendChild(el('span', null, pair2[0]));
      chips.appendChild(item);
    });
    panel.appendChild(chips);
    return panel;
  }

  /* ---------- centre: progress ---------- */

  function progressPanel(view) {
    var panel = el('section', 'ls-panel-box ls-progress');
    panel.dataset.tone = view.tone || 'neutral';
    panel.setAttribute('role', 'status');
    panel.setAttribute('aria-live', 'polite');

    var determinate = view.progress && view.progress.mode === 'determinate';
    var showPercent = determinate && view.showPercent === true;

    var dial = el('div', 'ls-dial');
    dial.dataset.mode = determinate ? 'determinate' : 'indeterminate';
    var ring = doc().createElementNS(SVG_NS, 'svg');
    ring.setAttribute('viewBox', '0 0 72 72');
    ring.setAttribute('class', 'ls-dial-svg');
    ring.setAttribute('aria-hidden', 'true');
    ['ls-dial-track', 'ls-dial-fill'].forEach(function (cls) {
      var circle = doc().createElementNS(SVG_NS, 'circle');
      circle.setAttribute('cx', '36'); circle.setAttribute('cy', '36');
      circle.setAttribute('r', '31');
      circle.setAttribute('class', cls);
      if (cls === 'ls-dial-fill' && determinate) {
        var circumference = 2 * Math.PI * 31;
        var offset = circumference * (1 - Number(view.progress.percent) / 100);
        circle.setAttribute('stroke-dasharray', String(circumference));
        circle.setAttribute('stroke-dashoffset', String(offset));
      }
      ring.appendChild(circle);
    });
    dial.appendChild(ring);

    var glyph = decorative(el('div', 'ls-dial-icon'));
    glyph.appendChild(svg('0 0 24 24', ICONS[GROUP_ICONS[view.activeGroupKey] || 'spark'] || ICONS.spark));
    dial.appendChild(glyph);

    if (showPercent) {
      var pct = el('div', 'ls-dial-pct');
      pct.appendChild(el('span', 'ls-dial-num', String(view.progress.percent)));
      pct.appendChild(el('span', 'ls-dial-sign', '%'));
      dial.appendChild(pct);
    }
    panel.appendChild(dial);

    panel.appendChild(el('h2', 'ls-progress-title', view.title));
    if (view.description) panel.appendChild(el('p', 'ls-progress-desc', view.description));

    var bar = el('div', 'ls-bar');
    if (determinate) {
      bar.setAttribute('role', 'progressbar');
      bar.setAttribute('aria-valuemin', '0');
      bar.setAttribute('aria-valuemax', '100');
      bar.setAttribute('aria-valuenow', String(view.progress.percent));
      var fill = el('span', 'ls-bar-fill');
      // Scaled, not widened: the stylesheet animates a transform so the row is
      // not relaid out on every update.
      fill.style.setProperty('--ls-fill', String(Number(view.progress.percent) / 100));
      bar.appendChild(fill);
    } else {
      // No number to expose: a band travels instead, and no aria value is set.
      bar.classList.add('is-indeterminate');
      bar.appendChild(decorative(el('span', 'ls-bar-band')));
    }
    panel.appendChild(bar);

    var meta = el('p', 'ls-progress-meta');
    if (determinate && view.progress.total != null) {
      meta.appendChild(el('span', 'ls-count',
        view.progress.current + ' de ' + view.progress.total
        + (view.progress.unit ? ' ' + view.progress.unit : '')));
    } else if (!determinate) {
      meta.appendChild(el('span', 'ls-count', 'Preparando…'));
    }
    if (view.duration && view.duration.label) {
      meta.appendChild(el('span', 'ls-duration', view.duration.label));
    }
    if (view.reasonCode) {
      meta.appendChild(el('code', 'ls-reason', view.reasonCode));
    }
    if (meta.childNodes.length) panel.appendChild(meta);
    return panel;
  }

  /* ---------- right: activity ---------- */

  function activityPanel(view) {
    var panel = el('section', 'ls-panel-box ls-activity');
    panel.appendChild(el('h3', 'ls-panel-title', 'Atividade ao vivo'));

    var disclosure = el('details', 'ls-activity-disclosure');
    disclosure.setAttribute('open', '');
    disclosure.appendChild(el('summary', null, 'Etapas e eventos'));
    var body = el('div', 'ls-activity-body');
    var list = el('ul', 'ls-activity-list');
    (view.groups || []).forEach(function (group) {
      var item = el('li', 'ls-activity-item');
      item.dataset.status = group.status;
      item.appendChild(decorative(el('span', 'ls-marker')));
      item.appendChild(el('span', 'ls-activity-label', group.label));
      // The state word carries the meaning; the marker only reinforces it.
      item.appendChild(el('span', 'ls-activity-state',
        STATE_WORDS[group.status] || group.status));
      list.appendChild(item);
    });
    body.appendChild(list);

    if ((view.events || []).length) {
      var details = el('details', 'ls-events');
      details.appendChild(el('summary', null, 'Eventos do processo'));
      var log = el('ul', 'ls-events-list');
      view.events.forEach(function (event) {
        var row = el('li', 'ls-event');
        if (event.seq != null) row.appendChild(el('span', 'ls-event-seq', String(event.seq)));
        if (event.at) row.appendChild(el('time', 'ls-event-at', event.at));
        if (event.stage) row.appendChild(el('span', 'ls-event-stage', event.stage));
        if (event.message) row.appendChild(el('span', 'ls-event-message', event.message));
        if (event.reasonCode) row.appendChild(el('code', 'ls-event-code', event.reasonCode));
        log.appendChild(row);
      });
      details.appendChild(log);
      body.appendChild(details);
    }
    disclosure.appendChild(body);
    panel.appendChild(disclosure);
    return panel;
  }

  /* ---------- bottom band ---------- */

  function stageBand(view) {
    var band = el('ol', 'ls-band');
    (view.groups || []).forEach(function (group) {
      var card = el('li', 'ls-band-card');
      card.dataset.status = group.status;
      card.appendChild(decorative(el('span', 'ls-band-index',
        String(group.index).padStart(2, '0'))));
      var icon = decorative(el('span', 'ls-band-icon'));
      icon.appendChild(svg('0 0 24 24', ICONS[GROUP_ICONS[group.key] || 'spark'] || ICONS.spark));
      card.appendChild(icon);
      card.appendChild(el('span', 'ls-band-label', group.label));
      card.appendChild(el('span', 'ls-band-state',
        STATE_WORDS[group.status] || group.status));
      band.appendChild(card);
    });
    return band;
  }

  /* ---------- header, banner, footer ---------- */

  function header(view) {
    var head = el('header', 'ls-head');
    var brand = el('div', 'ls-brand');
    brand.appendChild(el('span', 'ls-brand-mark', 'Tradutor'));
    brand.appendChild(el('span', 'ls-brand-dot', '.'));
    brand.appendChild(el('span', 'ls-brand-ia', 'IA'));
    head.appendChild(brand);

    // Only when the view model says so — never inferred from the environment.
    if (view.localTest) head.appendChild(el('span', 'ls-badge', 'ambiente local de teste'));

    head.appendChild(el('h1', 'ls-head-title', view.title));
    if (view.description) head.appendChild(el('p', 'ls-head-desc', view.description));

    if (view.languages && (view.languages.source || view.languages.target)) {
      var langs = el('p', 'ls-langs');
      langs.appendChild(el('span', 'ls-lang', view.languages.source || '—'));
      langs.appendChild(decorative(el('span', 'ls-lang-arrow', '→')));
      langs.appendChild(el('span', 'ls-lang', view.languages.target || '—'));
      head.appendChild(langs);
    }
    return head;
  }

  function banner() {
    var box = el('aside', 'ls-banner');
    var art = decorative(el('div', 'ls-banner-art'));
    art.appendChild(svg('0 0 64 40', [
      'M6 6h22v16H14l-6 6z', 'M36 12h22v14H44l-5 5z'
    ], 'ls-banner-svg'));
    box.appendChild(art);
    box.appendChild(el('p', 'ls-banner-text',
      'Traduzir não é apenas trocar palavras. É preservar contexto, ritmo e significado.'));
    return box;
  }

  function footer(view) {
    var facts = [];
    facts.push(['progresso', view.progress && view.progress.mode === 'determinate'
      ? 'determinado' : 'indeterminado']);
    if (view.localTest) facts.push(['ambiente', 'local de teste']);
    if (view.languages && view.languages.source) facts.push(['origem', view.languages.source]);
    if (view.languages && view.languages.target) facts.push(['destino', view.languages.target]);
    if (view.reducedMotion) facts.push(['movimento', 'reduzido']);
    if (view.status) facts.push(['estado', view.status]);
    if (!facts.length) return null;

    var foot = el('footer', 'ls-foot');
    facts.forEach(function (pair) {
      var item = el('span', 'ls-foot-item');
      item.appendChild(el('span', 'ls-foot-key', pair[0]));
      item.appendChild(el('span', 'ls-foot-value', pair[1]));
      foot.appendChild(item);
    });
    return foot;
  }

  /* ---------- terminal states ---------- */

  function terminalPanel(view) {
    var panel = el('section', 'ls-terminal');
    panel.dataset.tone = view.tone || 'neutral';
    panel.setAttribute('role', view.failed ? 'alert' : 'status');

    panel.appendChild(el('h2', 'ls-terminal-title', view.title));
    if (view.description) panel.appendChild(el('p', 'ls-terminal-desc', view.description));

    var meta = el('p', 'ls-terminal-meta');
    if (view.needsReview && view.pendingReviewCount != null) {
      meta.appendChild(el('span', 'ls-count',
        view.pendingReviewCount + ' item(ns) para revisar'));
    }
    if (view.reasonCode) meta.appendChild(el('code', 'ls-reason', view.reasonCode));
    if (view.duration && view.duration.label) {
      meta.appendChild(el('span', 'ls-duration', view.duration.label));
    }
    if (meta.childNodes.length) panel.appendChild(meta);

    // Every action is gated on the view model. Nothing is offered speculatively.
    var actions = el('div', 'ls-actions');
    if (view.hasResult) {
      actions.appendChild(action('open-result', 'ABRIR RESULTADO', 'primary'));
    }
    if (view.hasPdf) actions.appendChild(action('open-pdf', 'ABRIR PDF', 'ghost'));
    if (view.canOpenReview) actions.appendChild(action('open-review', 'ABRIR REVISÃO', 'primary'));
    if (view.canRetry) actions.appendChild(action('retry', 'TENTAR NOVAMENTE', 'primary'));
    if (view.failed && !view.canRetry) {
      panel.appendChild(el('p', 'ls-no-action',
        'Nova tentativa automática não está disponível para esta falha.'));
    }
    if (actions.childNodes.length) panel.appendChild(actions);
    return panel;
  }

  function action(name, label, kind) {
    var button = el('button', 'ls-action ls-action-' + (kind || 'ghost'), label);
    button.type = 'button';
    button.dataset.lsAction = name;
    return button;
  }

  /* ---------- entry point ---------- */

  /**
   * Replace the surface with the view model's rendering.
   *
   * @param {Element} root
   * @param {object} view  the value returned by mapJobStateToLoadingView
   */
  function renderProcessingSurface(root, view) {
    if (!root || !view) return root;
    var bootstrap = view.mode === 'bootstrap';
    var fragment = doc().createDocumentFragment();

    root.dataset.lsMode = bootstrap ? 'bootstrap' : 'pipeline';
    root.dataset.lsTone = view.tone || 'neutral';
    root.dataset.lsStatus = view.status || '';

    fragment.appendChild(header(view));

    var grid = el('div', 'ls-grid');
    // Bootstrap is deliberately smaller: no preview, no event log, no pipeline.
    if (!bootstrap) grid.appendChild(previewPanel(view));
    grid.appendChild(view.isTerminal ? terminalPanel(view) : progressPanel(view));
    if (!bootstrap) grid.appendChild(activityPanel(view));
    else grid.appendChild(activityPanel({groups: view.groups, events: []}));
    grid.appendChild(stageBand(view));
    fragment.appendChild(grid);

    if (!bootstrap) fragment.appendChild(banner());
    var foot = footer(view);
    if (foot) fragment.appendChild(foot);

    // replaceChildren keeps the swap atomic and drops the previous listeners.
    root.replaceChildren(fragment);
    return root;
  }

  var api = {
    SURFACE_VERSION: SURFACE_VERSION,
    STATE_WORDS: STATE_WORDS,
    renderProcessingSurface: renderProcessingSurface
  };

  if (typeof module === 'object' && module.exports) module.exports = api;
  global.TradutorProcessingSurface = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
