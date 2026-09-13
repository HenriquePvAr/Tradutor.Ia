import { json, preflight } from "../_shared/http.ts";
import { bearer, requireAuth } from "../_shared/auth.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return preflight();
  const d = requireAuth(req);
  if (d) return d;
  try {
    const b: any = await req.json();
    const u = Deno.env.get('SUPABASE_URL'), k = Deno.env.get('SUPABASE_ANON_KEY'), t = bearer(req);
    if (!u || !k || !t) return json({ code: 'control_plane_not_configured' }, 503);
    const r = await fetch(`${u}/rest/v1/rpc/register_device`, {
      method: 'POST',
      headers: { apikey: k, Authorization: `Bearer ${t}`, 'content-type': 'application/json' },
      body: JSON.stringify({ p_device_id: String(b?.device_id || ''), p_public_key: String(b?.public_key || '') })
    });
    const p = await r.json().catch(() => ({ code: 'device_unavailable' }));
    return r.ok ? json(p) : json({ code: String(p?.message || 'device_register_failed').includes('device_limit_reached') ? 'device_limit_reached' : 'device_register_failed' }, r.status >= 500 ? 503 : 400);
  } catch {
    return json({ code: 'device_unavailable' }, 503);
  }
});
