/* Reusable passive-ad surface. Rendering is UI-only and never changes YK. */
(function () {
  const URL = 'https://henriquepvar.github.io/ad/banner-728x90.html';
  const enabled = window.__yomuPassiveAdsProviderEnabled === true;
  function mount(container) {
    if (container.querySelector('iframe')) return;
    if (!container || !enabled) { if (container) container.hidden = true; return; }
    container.hidden = false; container.dataset.state = 'LOADING';
    const frame = document.createElement('iframe');
    frame.title = 'Publicidade'; frame.className = 'passive-ad-frame'; frame.loading = 'lazy';
    frame.referrerPolicy = 'strict-origin-when-cross-origin'; frame.src = URL;
    frame.addEventListener('load', () => { container.dataset.state = 'LOADED'; });
    frame.addEventListener('error', () => { container.dataset.state = 'ERROR'; frame.remove(); });
    container.append(frame);
  }
  function init() {
    const targets = [['#view-inicio', 'home'], ['#view-rewards', 'rewards'], ['#view-scans', 'scans']];
    targets.forEach(([selector, name]) => {
      const parent = document.querySelector(selector); if (!parent || parent.querySelector(`[data-passive-ad-slot="${name}"]`)) return;
      const container = document.createElement('div'); container.className = 'passive-ad-slot'; container.dataset.passiveAdSlot = name; container.hidden = true;
      container.innerHTML = '<span class="passive-ad-label">Publicidade</span>'; parent.insertBefore(container, parent.firstElementChild); mount(container);
    });
    document.querySelectorAll('[data-passive-ad-slot]').forEach(mount);
  }
  window.PassiveAdSlot = { init, url: URL, enabled };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true}); else init();
})();
