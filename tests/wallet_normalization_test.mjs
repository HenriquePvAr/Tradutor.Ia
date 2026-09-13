import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const elements = new Map();
const fetchUrls = [];
const context = {
  console,
  Date,
  URLSearchParams,
  window: {dispatchEvent() {}, addEventListener() {}, setInterval() { return 0; }, clearInterval() {}, __tradutorDesktopRuntime: false},
  document: {querySelector(selector) { if (!elements.has(selector)) elements.set(selector, {textContent: '', querySelector() { return null; }, addEventListener() {}}); return elements.get(selector); }},
  fetch: async (url) => { fetchUrls.push(String(url)); return {ok: true, json: async () => ({})}; },
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(fs.readFileSync(new URL('../static/control_plane_client.js', import.meta.url), 'utf8'), context);
const api = context.window.__yomuControlPlane;

assert.equal(api.normalizeWalletState({}).status, 'unknown_schema');
assert.equal(api.normalizeWalletState({total: 5}).status, 'unknown_schema');
assert.deepEqual({...api.normalizeWalletState({daily: 2, subscription: 3, permanent: 1, reserved: 1})}, {status: 'ready', active_yk: 5, daily_yk: 2, subscription_yk: 3, permanent_yk: 1, reserved_yk: 1});
assert.equal(api.normalizeWalletState({daily: 0, subscription: 0, permanent: 0, reserved: 0}).active_yk, 0);
api.applyWallet({daily: 2, subscription: 3, permanent: 1, reserved: 1}, 'beta-bootstrap');
assert.ok(fetchUrls.length > 0, 'wallet diagnostics trace should be emitted');
assert.equal(fetchUrls.filter(url => url.includes('wallet-summary')).length, 0, 'normalization must not issue an extra wallet fetch');
console.log('wallet normalization: PASS');
