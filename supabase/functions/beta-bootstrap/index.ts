import { json, preflight } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return preflight();
  const denied = requireAuth(req);
  if (denied) return denied;
  const r = await callRpc(req, "beta_bootstrap", {});
  if (r.error) return json({ code: r.error }, 503);
  return r.response?.ok ? json(r.payload) : json({ code: "bootstrap_unavailable" }, 503);
});
