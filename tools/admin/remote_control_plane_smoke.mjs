import crypto from 'node:crypto';

const url = process.env.SUPABASE_URL;
const publishable = process.env.SUPABASE_PUBLISHABLE_KEY;
const secret = process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_SECRET_KEY;
if (!url || !publishable || !secret) throw new Error('secure_supabase_credentials_missing');
const run = Date.now().toString();
const h = (token, admin = false) => ({ apikey: admin ? secret : publishable, Authorization: `Bearer ${token}`, 'content-type': 'application/json' });
async function req(path, token, body, admin = false) { const r = await fetch(`${url}${path}`, { method: 'POST', headers: h(token, admin), body: body === undefined ? undefined : JSON.stringify(body) }); let p = {}; try { p = await r.json(); } catch {} return { status: r.status, ok: r.ok, p }; }
async function rpc(name, token, body, admin = false) { return req(`/rest/v1/rpc/${name}`, token, body, admin); }
async function edge(name, token, body) { return req(`/functions/v1/${name}`, token, body); }
async function adminCreate(label) { const email = `ys-remote-smoke-${label}-${run}@example.com`; const password = crypto.randomBytes(24).toString('base64url') + 'A1!'; const r = await req('/auth/v1/admin/users', secret, { email, password, email_confirm: true, user_metadata: { purpose: 'yomu_remote_control_plane_smoke', run_id: run, label } }, true); if (!r.ok || !r.p.id) throw new Error(`admin_create_${label}_${r.status}`); return { id: r.p.id, email, password }; }
async function login(f) { const r = await fetch(`${url}/auth/v1/token?grant_type=password`, { method: 'POST', headers: { apikey: publishable, 'content-type': 'application/json' }, body: JSON.stringify({ email: f.email, password: f.password }) }); const p = await r.json().catch(() => ({})); if (!r.ok || !p.access_token) throw new Error(`login_${r.status}`); return p.access_token; }
const dev = await adminCreate('dev');
const user = await adminCreate('user');
const devToken = await login(dev); const userToken = await login(user);
const staff = await fetch(`${url}/rest/v1/staff_members`, { method: 'POST', headers: { ...h(secret, true), Prefer: 'resolution=merge-duplicates' }, body: JSON.stringify({ user_id: dev.id, role: 'dev', created_by: dev.id }) });
if (!staff.ok) throw new Error(`staff_${staff.status}`);
const invite = await rpc('admin_create_beta_invite', devToken, { p_max_uses: 1, p_default_license_days: 30, p_idempotency_key: `smoke-${run}` });
if (!invite.ok) throw new Error(`invite_${invite.status}`);
const redeem = await edge('beta-invite-redeem', userToken, { code: invite.p.code });
if (!redeem.ok) throw new Error(`redeem_${redeem.status}`);
const licenseId = redeem.p.license_id;
const deviceKey = crypto.generateKeyPairSync('ed25519');
const pub = deviceKey.publicKey.export({ type: 'spki', format: 'der' }).subarray(-32).toString('base64url');
const device = await edge('device-register', userToken, { device_id: `remote-smoke-${run}`, public_key: pub });
if (!device.ok) throw new Error(`device_${device.status}`);
const deviceUuid = device.p.device_id;
const challenge = await edge('device-challenge', userToken, { device_uuid: deviceUuid });
if (!challenge.ok) throw new Error(`challenge_${challenge.status}`);
const signature = crypto.sign(null, Buffer.from(challenge.p.nonce), deviceKey.privateKey).toString('base64url');
const verify = await edge('device-verify', userToken, { challenge_id: challenge.p.challenge_id, nonce: challenge.p.nonce, signature });
if (!verify.ok) throw new Error(`verify_${verify.status}`);
const boot = await edge('beta-bootstrap', userToken, {});
const claim1 = await edge('wallet-daily-claim', userToken, {}); const claim2 = await edge('wallet-daily-claim', userToken, {});
const summary = await edge('wallet-summary', userToken, {});
const job = `remote-smoke-job-${run}`;
const reserve = await edge('wallet-reserve', userToken, { job_id: job, device_id: deviceUuid, amount: 1 });
if (!reserve.ok) throw new Error(`reserve_${reserve.status}`);
const translation = await edge('translation-execute', userToken, { request_id: `remote-smoke-request-${run}`, job_id: job, device_id: deviceUuid, reservation_id: reserve.p.reservation_id, character_count: 1, text: 'fixture' });
const release = await edge('wallet-finalize', userToken, { reservation_id: reserve.p.reservation_id, release: true });
const heartbeat = await edge('device-heartbeat', userToken, { device_uuid: deviceUuid });
const revoke = await rpc('admin_revoke_license', devToken, { p_license_id: licenseId, p_reason: 'remote smoke fixture' });
const heartbeatRevoked = await edge('device-heartbeat', userToken, { device_uuid: deviceUuid });
const denied = {};
for (const [name, body] of [['award_translation_xp',{p_reference_id:`smoke-${run}`,p_amount:1}],['complete_translation_request',{p_request_id:`smoke-${run}`}],['fail_translation_request',{p_request_id:`smoke-${run}`,p_error_code:'x'}]]) denied[name] = (await rpc(name, userToken, body)).status;
console.log(JSON.stringify({run,dev_id:dev.id,user_id:user.id,invite:invite.status,redeem:redeem.status,device_register:device.status,device_verify:verify.status,bootstrap:boot.status,daily_1:claim1.p.amount||null,daily_2:claim2.p.amount||null,wallet_summary:summary.status,reserve:reserve.status,translation_guard:translation.status,release:release.status,heartbeat:heartbeat.status,revoke:revoke.status,heartbeat_after_revoke:heartbeatRevoked.status,direct_rpc_denials:denied}));
