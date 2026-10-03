// Contract guard: local profile-media UI, routes, bridge, and transport are removed.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const failures = [];
let passed = 0;

async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (err) { failures.push({ name, message: String(err && err.message || err) }); }
}

const shell = fs.readFileSync(path.join(ROOT, 'ui', 'ui_shell.html'), 'utf8');
const app = fs.readFileSync(path.join(ROOT, 'static', 'tradutor_ui.js'), 'utf8');
const community = fs.readFileSync(path.join(ROOT, 'static', 'social_community.js'), 'utf8');
const api = fs.readFileSync(path.join(ROOT, 'static', 'social_api.js'), 'utf8');
const authUi = fs.readFileSync(path.join(ROOT, 'static', 'auth_ui.js'), 'utf8');
const backend = fs.readFileSync(path.join(ROOT, 'app_ui.py'), 'utf8');
const bridge = fs.readFileSync(path.join(ROOT, 'ui_bridge.py'), 'utf8');

await test('main shell has no profile view, navigation, edit action, or media upload controls', async () => {
  assert.doesNotMatch(shell, /view-profile|data-tab="profile"|Editar perfil|profile-(?:avatar|banner)-input/i);
  assert.doesNotMatch(shell, /data-preview-sources="[^"]*view-profile/);
});

await test('normal client code has no profile reads, writes, or avatar/banner media hydration', async () => {
  for (const source of [app, community, api]) {
    assert.doesNotMatch(source, /\/api\/ui\/profile\/media|\/api\/community\/social\/profile\/me|\/functions\/v1\/profile-media/);
  }
  assert.doesNotMatch(api, /\b(?:getMyProfile|updateMyProfile|getProfileByUsername)\b/);
  assert.doesNotMatch(community, /\b(?:getMyProfile|updateMyProfile|renderProfile)\b/);
});

await test('local backend and bridge expose no profile-media surface', async () => {
  assert.doesNotMatch(backend, /profile_media|profile-media|avatar_object_key|banner_object_key|api_profile_media/);
  assert.doesNotMatch(bridge, /profile_media|profile-media|avatar_object_key|banner_object_key|save_profile_media/);
});

await test('bootstrap does not query or synchronize social profile metadata', async () => {
  const start = backend.indexOf('def api_bootstrap(');
  const end = backend.indexOf('\n@app.', start + 1);
  assert.ok(start >= 0 && end > start, 'bootstrap handler block must be located');
  const bootstrap = backend.slice(start, end);
  assert.doesNotMatch(bootstrap, /_remote_profile_for_principal|_profile_for_principal|_sync_public_profile/);
  assert.doesNotMatch(bootstrap, /profile_state|profile_error_code/);
  const bridgeStart = bridge.indexOf('    def bootstrap(self, cursor: int = 0, *, principal: RequestPrincipal)');
  const bridgeEnd = bridge.indexOf('\n    def latest_source_analysis', bridgeStart + 1);
  assert.ok(bridgeStart >= 0 && bridgeEnd > bridgeStart, 'bridge bootstrap method must be located');
  assert.doesNotMatch(bridge.slice(bridgeStart, bridgeEnd), /profile_for_user|"profile"\s*:/);
});

await test('obsolete main-shell profile navigation falls back to Home', async () => {
  assert.match(app, /function activateTab[\s\S]{0,500}profile[\s\S]{0,200}inicio/);
  assert.match(app, /tradutor-goto-tab[\s\S]{0,400}profile[\s\S]{0,200}inicio/);
});

await test('account identity remains session-based and does not require social profile metadata', async () => {
  assert.match(authUi, /status\.textContent\s*=\s*displayName\s*\|\|\s*session\.user\?\.email\s*\|\|\s*'Conta'/);
  assert.doesNotMatch(authUi, /\/api\/ui\/profile\/media|\/api\/community\/social\/profile\/me/);
});

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
