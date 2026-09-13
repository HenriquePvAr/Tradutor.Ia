import { json } from "../_shared/http.ts";
import { bearer, requireAuth } from "../_shared/auth.ts";
Deno.serve(async (req) => {
  const denied = requireAuth(req); if (denied) return denied;
  try {
    const body: any = await req.json(); if (!body?.code) return json({ code: "invite_invalid_or_expired" }, 400);
    const url = Deno.env.get("SUPABASE_URL"); const key = Deno.env.get("SUPABASE_ANON_KEY"); const token = bearer(req);
    if (!url || !key || !token) return json({ code: "control_plane_not_configured" }, 503);
    const response = await fetch(`${url}/rest/v1/rpc/redeem_beta_invite`, { method: "POST", headers: { apikey: key, Authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify({ p_code: String(body.code) }) });
    const payload = await response.json().catch(() => ({ code: "invite_service_unavailable" }));
    if (!response.ok) return json({ code: typeof payload?.message === "string" && payload.message.includes("license_already_active") ? "license_already_active" : "invite_invalid_or_expired" }, response.status >= 500 ? 503 : 400);
    return json(payload, 200);
  } catch { return json({ code: "invite_service_unavailable" }, 503); }
});
