/* Reusable passive-ad surface. Rendering is UI-only and never changes YK. */
(function () {
  const URL = 'https://henriquepvar.github.io/ad/banner-728x90.html';
  // Resolve at mount time: this script is loaded in <head>, while the
  // bootstrap flag is assigned by the body script afterwards.
  const providerEnabled = () => window.__yomuPassiveAdsProviderEnabled === true;
  function mount(container) {
    if (!container) return;
    if (container.querySelector('iframe')) return;
    const enabled = providerEnabled();
    if (!container || !enabled) { if (container) container.hidden = true; return; }
    container.hidden = false; container.dataset.state = 'LOADING';
    const frame = document.createElement('iframe');
    frame.title = 'Publicidade'; frame.className = 'passive-ad-frame';
    // Eager loading is intentional: WebView2 can keep lazy frames in a
    // permanently blank state when their parent view is mounted before the
    // navigation becomes visible. The frame remains isolated and does not
    // grant script access to the host document.
    frame.loading = 'eager';
    // Do not leak the loopback UI origin to the ad provider. This also avoids
    // localhost referrer rejection while preserving the public ad origin.
    frame.referrerPolicy = 'no-referrer'; frame.src = URL;
    frame.addEventListener('load', () => { container.dataset.state = 'LOADED'; });
    frame.addEventListener('error', () => { container.dataset.state = 'ERROR'; frame.remove(); });
    container.append(frame);
  }
  function init() {
    const targets = [
      ['#view-inicio', 'home'],
      ['#view-nova', 'new-translation'],
      ['#view-queue', 'queue'],
      ['#view-rewards', 'rewards'],
      ['#view-hist', 'translated-chapters'],
    ];
    targets.forEach(([selector, name]) => {
      const parent = document.querySelector(selector); if (!parent || parent.querySelector(`[data-passive-ad-slot="${name}"]`)) return;
      const container = document.createElement('div'); container.className = 'passive-ad-slot'; container.dataset.passiveAdSlot = name; container.hidden = true;
      container.innerHTML = '<span class="passive-ad-label">Publicidade</span>'; parent.insertBefore(container, parent.firstElementChild); mount(container);
    });
    document.querySelectorAll('[data-passive-ad-slot]').forEach(mount);
  }
  window.PassiveAdSlot = { init, url: URL, isEnabled: providerEnabled };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true}); else init();
})();
