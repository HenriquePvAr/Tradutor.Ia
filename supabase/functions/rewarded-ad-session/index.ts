import { json, preflight } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return preflight();
  const denied = requireAuth(req); if (denied) return denied;
  if (req.method !== "POST") return json({ code: "method_not_allowed" }, 405);
  let body: any = {}; try { body = await req.json(); } catch { /* defaults */ }
  const adslot = String(body?.adslot_id || Deno.env.get("AYET_REWARDED_ADSLOT_ID") || "").trim();
  if (!adslot || adslot.length > 64) return json({ code: "provider_not_configured" }, 503);
  const r = await callRpc(req, "create_rewarded_ad_session", { p_adslot_id: adslot });
  if (r.error) return json({ code: r.error }, 503);
  return r.response?.ok ? json(r.payload) : json({ code: String(r.payload?.message || "rewarded_ads_unavailable").replace(/[^a-zA-Z0-9_.-]/g, "_") }, 409);
});
