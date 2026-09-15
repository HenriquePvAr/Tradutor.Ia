// Releasing is the only reservation transition a signed-in client still owns.
// Consuming a reservation writes ledger debits and therefore belongs to
// finalize_translation_job, which runs service-side from translation-execute
// after every provider batch of the chapter has completed.
import { json } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

const KNOWN_CODES = new Set([
  "reservation_not_found",
  "reservation_in_flight",
  "unauthorized",
]);

Deno.serve(async (req) => {
  const denied = requireAuth(req);
  if (denied) return denied;
  try {
    const body: any = await req.json();
    if (body?.release === false) {
      return json({ code: "reservation_consume_not_client_callable" }, 403);
    }
    const result = await callRpc(req, "release_yk_reservation", {
      p_reservation_id: body?.reservation_id,
    });
    if (result.error) return json({ code: String(result.error) }, 503);
    if (result.response?.ok) return json(result.payload);
    const payload: any = result.payload || {};
    const raw = String(payload.code || payload.message || "").toLowerCase();
    const normalized = KNOWN_CODES.has(raw) ? raw : "reservation_not_found";
    return json({ code: normalized }, 409);
  } catch {
    return json({ code: "wallet_finalize_failed" }, 503);
  }
});
