import { json, preflight } from "../_shared/http.ts";
import { bearer, requireAuth } from "../_shared/auth.ts";

const decode = (s: string) => {
  const x = s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=');
  return Uint8Array.from(atob(x), c => c.charCodeAt(0));
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return preflight();
  const denied = requireAuth(req);
  if (denied) return denied;
  try {
    const b: any = await req.json();
    const u = Deno.env.get('SUPABASE_URL'), k = Deno.env.get('SUPABASE_ANON_KEY'), t = bearer(req);
    if (!u || !k || !t) return json({ code: 'control_plane_not_configured' }, 503);
    const h = { apikey: k, Authorization: `Bearer ${t}`, 'content-type': 'application/json' };
    const q = await fetch(`${u}/rest/v1/rpc/get_device_challenge_for_verify`, {
      method: 'POST', headers: h, body: JSON.stringify({ p_challenge_id: b?.challenge_id })
    });
    const c: any = await q.json().catch(() => null);
    if (!q.ok || !c) return json({ code: 'device_challenge_not_found' }, 404);
    if (c.consumed_at) return json({ code: 'device_challenge_consumed' }, 409);
    if (new Date(c.expires_at).getTime() <= Date.now()) return json({ code: 'device_challenge_expired' }, 410);
    const nonce = String(b?.nonce || '');
    const dig = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(nonce));
    const hash = Array.from(new Uint8Array(dig)).map(x => x.toString(16).padStart(2, '0')).join('');
    if (hash !== c.nonce_hash) return json({ code: 'device_challenge_invalid' }, 401);
    let key: CryptoKey;
    try {
      key = await crypto.subtle.importKey('raw', decode(String(c.public_key)), { name: 'Ed25519' }, false, ['verify']);
    } catch {
      return json({ code: 'device_signature_invalid' }, 401);
    }
    let sig: Uint8Array;
    try {
      sig = decode(String(b?.signature || ''));
    } catch {
      return json({ code: 'device_signature_invalid' }, 401);
    }
    if (!await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, new TextEncoder().encode(nonce))) {
      return json({ code: 'device_signature_invalid' }, 401);
    }
    const consume = await fetch(`${u}/rest/v1/rpc/consume_device_challenge`, {
      method: 'POST', headers: h, body: JSON.stringify({ p_challenge_id: b.challenge_id, p_nonce_hash: hash })
    });
    const out = await consume.json().catch(() => ({}));
    return consume.ok ? json({ verified: true, ...out }) : json({ code: 'device_challenge_invalid' }, 409);
  } catch {
    return json({ code: 'device_verify_unavailable' }, 503);
  }
});
