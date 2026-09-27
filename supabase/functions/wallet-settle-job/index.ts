import { json, preflight } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const JOB_ID = /^[A-Za-z0-9][A-Za-z0-9:_.-]{0,219}$/;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9:_.-]{15,199}$/;

export async function handleWalletSettleJob(req: Request,
  rpc: typeof callRpc = callRpc): Promise<Response> {
  if (req.method === "OPTIONS") return preflight();
  if (req.method !== "POST") return json({ code: "method_not_allowed" }, 405);
  const denied = requireAuth(req);
  if (denied) return denied;
  let body: unknown;
  try { body = await req.json(); } catch { return json({ code: "invalid_json" }, 400); }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return json({ code: "invalid_settlement_request" }, 400);
  }
  const value = body as Record<string, unknown>;
  const allowed = new Set(["job_id", "reservation_id", "action", "idempotency_key"]);
  if (Object.keys(value).some((key) => !allowed.has(key)) || Object.keys(value).length !== 4) {
    return json({ code: "invalid_settlement_request" }, 400);
  }
  const jobId = String(value.job_id ?? "").trim();
  const reservationId = String(value.reservation_id ?? "").trim();
  const action = String(value.action ?? "").trim();
  const idempotencyKey = String(value.idempotency_key ?? "").trim();
  if (!JOB_ID.test(jobId) || !UUID.test(reservationId)
      || !["consume", "release"].includes(action)
      || !IDEMPOTENCY.test(idempotencyKey)) {
    return json({ code: "invalid_settlement_request" }, 400);
  }
  let result;
  try {
    result = await rpc(req, "settle_translation_job", {
      p_job_id: jobId,
      p_reservation_id: reservationId,
      p_action: action,
      p_idempotency_key: idempotencyKey,
    });
  } catch {
    return json({ code: "settlement_unavailable" }, 503);
  }
  if (result.error) return json({ code: "settlement_unavailable" }, 503);
  if (!result.response?.ok) {
    const raw = String(result.payload?.message || result.payload?.code || "");
    const code = /^[a-z][a-z0-9_]{0,79}$/.test(raw) ? raw : "settlement_rejected";
    return json({ code }, code === "unauthorized" ? 401 : 409);
  }
  return json(result.payload);
}

if (import.meta.main) Deno.serve((req) => handleWalletSettleJob(req));
