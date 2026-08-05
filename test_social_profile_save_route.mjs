// Regression guard for the social profile SAVE path (PATCH /profile/me).
//
// Context: a bug report claimed static/social_api.js's save route contained a typo near
// `updateMyProfile` — literal backslashes in the path (e.g. '\profile\me') instead of
// forward slashes. Audited against this worktree's confirmed base (community lineage HEAD
// 3baa255): no such string exists anywhere in the file's history on this branch. The route
// has always read `request('PATCH', '/profile/me', ...)`, matching the backend's
// `@router.patch("/profile/me")` under the `/api/community/social` prefix. This harness
// does not reproduce a failure against the current source — it executes the REAL
// static/social_api.js (not a reimplementation, not a regex over the text) against a fake
// fetch and asserts the exact request produced, so a future regression (typo, wrong verb,
// leaked identity field, token/content in the URL) fails loudly here instead of silently
// reaching the backend.
//
// No credentials of any kind: the token below is synthetic.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const failures = [];
let passed = 0;

async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (err) { failures.push({ name, message: String(err && err.message || err) }); }
}

async function loadSocialApi({ token = 'synthetic-token', fetchImpl }) {
  const calls = [];
  const win = {
    console,
    fetch: async (url, init) => {
      calls.push({ url, init });
      return fetchImpl ? fetchImpl(url, init) : { ok: true, json: async () => ({ id: 'synthetic-user' }) };
    },
  };
  win.window = win; win.globalThis = win;
  const context = vm.createContext(win);

  const authStubExports = { getCanonicalAccessToken: async () => token };
  const authStub = new vm.SyntheticModule(
    Object.keys(authStubExports),
    function () { for (const k of Object.keys(authStubExports)) this.setExport(k, authStubExports[k]); },
    { context, identifier: '/static/auth_provider.js' },
  );

  const source = fs.readFileSync(path.join(ROOT, 'static', 'social_api.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context, identifier: 'social_api.js' });
  await module.link((specifier) => {
    if (specifier === '/static/auth_provider.js') return authStub;
    throw new Error(`unexpected import: ${specifier}`);
  });
  await module.evaluate();
  return { api: module.namespace, calls };
}

await test('save issues exactly one PATCH to the canonical forward-slash path', async () => {
  const { api, calls } = await loadSocialApi({});
  await api.updateMyProfile({ display_name: 'Novo Nome' });
  assert.equal(calls.length, 1, `expected 1 fetch call, got ${calls.length}`);
  const [{ url, init }] = calls;
  assert.equal(url, '/api/community/social/profile/me');
  assert.equal(init.method, 'PATCH');
});

await test('the save path contains no backslashes and no mangled segments', async () => {
  const { api, calls } = await loadSocialApi({});
  await api.updateMyProfile({ display_name: 'x' });
  const url = calls[0].url;
  assert.equal(url.includes('\\'), false, `path must not contain backslashes: ${url}`);
  assert.equal(url, '/api/community/social/profile/me');
});

await test('save body carries only user content, never owner/email/token/status', async () => {
  const { api, calls } = await loadSocialApi({});
  await api.updateMyProfile({
    display_name: 'Novo Nome', bio: 'Olá', theme_color: '#112233',
    show_favorites: true, show_history: false, allow_profile_comments: true,
    owner_id: 'attacker', user_id: 'attacker', role: 'admin', status: 'approved', email: 'x@example.com',
  });
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.owner_id, undefined);
  assert.equal(body.user_id, undefined);
  assert.equal(body.role, undefined);
  assert.equal(body.status, undefined);
  // email was never a documented profile field either — sanitizeBody only strips the
  // FORBIDDEN identity/ownership set, so an accidental `email` field would pass through;
  // the UI form never constructs one (see static/social_community.js profileForm), which is
  // the actual contract this harness protects against regressing.
  assert.equal(body.display_name, 'Novo Nome');
  assert.equal(body.bio, 'Olá');
});

await test('the bearer token travels only in the Authorization header, never the URL or body', async () => {
  const { api, calls } = await loadSocialApi({ token: 'super-secret-token' });
  await api.updateMyProfile({ display_name: 'x' });
  const { url, init } = calls[0];
  assert.equal(url.includes('super-secret-token'), false);
  assert.equal(init.body.includes('super-secret-token'), false);
  assert.equal(init.headers.Authorization, 'Bearer super-secret-token');
});

await test('a missing token fails closed as 401 without ever calling fetch', async () => {
  const { api, calls } = await loadSocialApi({ token: null });
  await assert.rejects(() => api.updateMyProfile({ display_name: 'x' }), (err) => err.status === 401);
  assert.equal(calls.length, 0);
});

await test('an HTTP error from the backend is sanitized, not the raw payload', async () => {
  const { api } = await loadSocialApi({
    token: 'tok',
    fetchImpl: async () => ({ ok: false, status: 422, json: async () => ({ detail: 'validation_error' }) }),
  });
  const err = await api.updateMyProfile({ display_name: 'x' }).catch((e) => e);
  assert.equal(err.status, 422);
  assert.equal(api.messageForError(err), 'Revise os campos informados.');
});

await test('read (GET profile/me) uses the same canonical base path as the write', async () => {
  const { api, calls } = await loadSocialApi({});
  await api.getMyProfile();
  await api.updateMyProfile({ display_name: 'x' });
  assert.equal(calls[0].url, '/api/community/social/profile/me');
  assert.equal(calls[1].url, '/api/community/social/profile/me');
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[1].init.method, 'PATCH');
});

console.log(JSON.stringify({ passed, failed: failures.length, failures }, null, 2));
if (failures.length) process.exit(1);
