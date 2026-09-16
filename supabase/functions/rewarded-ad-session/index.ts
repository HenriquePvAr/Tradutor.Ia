import { json, preflight } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return preflight();
  const denied = requireAuth(req); if (denied) return denied;
  if (req.method !== "POST") return json({ code: "method_not_allowed" }, 405);
  return json({ code: "rewarded_ads_disabled" }, 503);
});
