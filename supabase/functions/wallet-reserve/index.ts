import { json } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

const KNOWN_CODES = new Set(["insufficient_yk", "device_not_found", "license_not_found", "pricing_unavailable", "unauthorized", "invalid_request"]);

Deno.serve(async (req) => {
  const denied = requireAuth(req);
  if (denied) return denied;
  try {
    const body: any = await req.json();
    const result = await callRpc(req, "reserve_translation_yk", {
      p_job_id: String(body?.job_id || ""), p_device_id: body?.device_id,
    });
    if (result.error) return json({code: String(result.error)}, 503);
    if (result.response?.ok) return json(result.payload);
    const payload: any = result.payload || {};
    const raw = String(payload.code || payload.message || "").toLowerCase();
    const normalized = KNOWN_CODES.has(raw) ? raw : "wallet_reserve_rpc_failed";
    return json({code: normalized}, normalized === "insufficient_yk" ? 409 : 503);
  } catch {
    return json({code: "wallet_reserve_failed"}, 503);
  }
});
