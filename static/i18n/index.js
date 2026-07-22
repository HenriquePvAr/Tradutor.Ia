(function () {
  'use strict';
  const SUPPORTED = ['pt-BR', 'en-US', 'es-ES', 'fr-FR', 'ja-JP', 'ko-KR'];
  const FALLBACK = 'pt-BR';
  const MAP = {pt: 'pt-BR', en: 'en-US', es: 'es-ES', fr: 'fr-FR', ja: 'ja-JP', ko: 'ko-KR'};
  const STORAGE_KEY = 'tradutor.interfaceLanguage';

  function normalize(value) {
    const raw = String(value || '').trim();
    if (!raw || raw === 'auto') return 'auto';
    if (SUPPORTED.includes(raw)) return raw;
    const prefix = raw.split('-')[0].toLowerCase();
    return MAP[prefix] || FALLBACK;
  }

  function resolveAuto() {
    for (const language of navigator.languages || [navigator.language]) {
      const normalized = normalize(language);
      if (normalized !== 'auto' && SUPPORTED.includes(normalized)) return normalized;
    }
    return FALLBACK;
  }

  function selectedLanguage() {
    return normalize(localStorage.getItem(STORAGE_KEY) || 'auto');
  }

  function activeLanguage() {
    const selected = selectedLanguage();
    return selected === 'auto' ? resolveAuto() : selected;
  }

  function t(key, params = {}) {
    const language = activeLanguage();
    const catalogs = window.TradutorI18nCatalogs || {};
    let text = catalogs[language]?.[key] ?? catalogs[FALLBACK]?.[key] ?? key;
    for (const [name, value] of Object.entries(params || {})) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
    return text;
  }

  function apply(root = document) {
    root.querySelectorAll('[data-i18n]').forEach((node) => {
      node.textContent = t(node.getAttribute('data-i18n'));
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
      node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder')));
    });
    root.querySelectorAll('[data-i18n-aria-label]').forEach((node) => {
      node.setAttribute('aria-label', t(node.getAttribute('data-i18n-aria-label')));
    });
  }

  function setLanguage(value) {
    localStorage.setItem(STORAGE_KEY, normalize(value));
    document.documentElement.lang = activeLanguage();
    apply(document);
    window.dispatchEvent(new CustomEvent('tradutor:language-changed', {
      detail: {selected: selectedLanguage(), active: activeLanguage()},
    }));
  }

  function formatNumber(value, options) {
    return new Intl.NumberFormat(activeLanguage(), options).format(value);
  }

  function formatDate(value, options) {
    return new Intl.DateTimeFormat(activeLanguage(), options).format(value);
  }

  window.TradutorI18n = {
    supported: SUPPORTED.slice(),
    fallback: FALLBACK,
    normalize,
    selectedLanguage,
    activeLanguage,
    setLanguage,
    t,
    apply,
    formatNumber,
    formatDate,
  };
  document.documentElement.lang = activeLanguage();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => apply(document), {once: true});
  } else {
    apply(document);
  }
}());
