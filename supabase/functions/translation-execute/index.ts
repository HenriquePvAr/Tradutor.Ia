import { json } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
import { callRpc } from "../_shared/rpc.ts";

const ALLOWED_BASES = new Set(["https://api-free.deepl.com", "https://api.deepl.com"]);
const safeCode = (value: unknown, fallback: string) => String(value || fallback).replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 80);
const localTestRuntime = ["local", "test"].includes(String(Deno.env.get("YOMU_ENV") || "").toLowerCase());
const faultMode = localTestRuntime ? String(Deno.env.get("YOMU_FAULT_MODE") || "").toLowerCase() : "";

async function serverRpc(name: string, body: unknown) {
  const url = Deno.env.get("SUPABASE_URL");
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || Deno.env.get("SUPABASE_SECRET_KEY");
  if (!url || !key) return { configured: false, ok: false, payload: {} };
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, { method: "POST", headers: { apikey: key, Authorization: `Bearer ${key}`, "content-type": "application/json" }, body: JSON.stringify(body) });
  return { configured: true, ok: response.ok, payload: await response.json().catch(() => ({})) };
}

function resultMetadata(result: any, fallbackCharacterCount: number) {
  const metadata = result && typeof result.metadata === "object" && result.metadata ? result.metadata : {};
  const billedRaw = Number(metadata.billed_characters);
  return {
    billed_characters: Number.isFinite(billedRaw) ? billedRaw : fallbackCharacterCount,
    detected_source_language: metadata.detected_source_language ? String(metadata.detected_source_language) : null,
    model_type_used: metadata.model_type_used ? String(metadata.model_type_used) : null,
  };
}

async function commitBatchAndMaybeFinalize(requestId: string, jobId: string, reservationId: string,
  metadata: { billed_characters: number | null; detected_source_language: string | null; model_type_used: string | null },
  result: unknown, finalizeJob: boolean) {
  const committed = await serverRpc("commit_translation_batch_success", {
    p_request_id: requestId,
    p_billed_characters: metadata.billed_characters,
    p_detected_source_language: metadata.detected_source_language,
    p_model_type_used: metadata.model_type_used,
    p_result: result,
  });
  if (!committed.configured || !committed.ok) return false;
  if (!finalizeJob) return true;
  const finalized = await serverRpc("finalize_translation_job", {
    p_job_id: jobId,
    p_reservation_id: reservationId,
  });
  return finalized.configured && finalized.ok;
}

Deno.serve(async (req) => {
  const denied = requireAuth(req); if (denied) return denied;
  let body: any; try { body = await req.json(); } catch { return json({ code: "invalid_json" }, 400); }
  const legacyText = String(body?.text || "");
  const rawItems = Array.isArray(body?.items) ? body.items : (legacyText ? [{ item_id: "item-1", text: legacyText }] : []);
  const items = rawItems.map((item: any) => ({ item_id: String(item?.item_id || ""), text: String(item?.text || "") }));
  const requestId = String(body?.request_id || "");
  const deviceId = body?.device_id;
  const reservationId = body?.reservation_id;
  const finalizeJob = body?.finalize_job !== false;
  if (!requestId || !deviceId || !reservationId || !items.length || items.length > 100 || items.some((i: any) => !i.item_id || !i.text || i.text.length > 100000) || new Set(items.map((i: any) => i.item_id)).size !== items.length || items.reduce((n: number, i: any) => n + i.text.length, 0) > 100000) return json({ code: "invalid_translation_request" }, 400);
  const characterCount = items.reduce((n: number, i: any) => n + i.text.length, 0);
  const hashInput = JSON.stringify({ job_id: body?.job_id || null, source_lang: body?.source_lang || "", target_lang: body?.target_lang || "", items });
  const hashBytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(hashInput));
  const requestHash = Array.from(new Uint8Array(hashBytes)).map((b) => b.toString(16).padStart(2, "0")).join("");
  const begin = await callRpc(req, "begin_translation_request", { p_request_id: requestId, p_device_id: deviceId, p_reservation_id: reservationId, p_job_id: body?.job_id, p_character_count: characterCount, p_request_hash: requestHash });
  if (begin.error) return json({ code: begin.error }, 503);
  if (!begin.response?.ok) return json({ code: safeCode(begin.payload?.message || begin.payload?.code, "translation_begin_failed") }, 409);
  if (begin.payload?.idempotent) {
    if (begin.payload.status === "completed") {
      if (finalizeJob) {
        const finalized = await serverRpc("finalize_translation_job", {
          p_job_id: String(body?.job_id || ""),
          p_reservation_id: String(reservationId),
        });
        if (!finalized.configured || !finalized.ok) return json({ code: "translation_commit_failed", request_id: requestId }, 503);
      }
      return json({ request_id: requestId, status: "completed", items: begin.payload.result?.items || [], idempotent_replay: true }, 200);
    }
    if (begin.payload.status === "provider_succeeded" && begin.payload.result) {
      const recovered = begin.payload.result;
      const metadata = resultMetadata(recovered, characterCount);
      const committed = await commitBatchAndMaybeFinalize(requestId, String(body?.job_id || ""), String(reservationId), metadata, recovered, finalizeJob);
      if (!committed) return json({ code: "translation_commit_failed", request_id: requestId }, 503);
      return json({ request_id: requestId, status: "completed", provider: recovered.provider || "deepl", items: recovered.items || [], idempotent_replay: true }, 200);
    }
    if (begin.payload.status === "provider_outcome_unknown") return json({ code: "provider_outcome_unknown", request_id: requestId }, 409);
    return json({ code: begin.payload.status === "failed" ? "translation_failed" : "translation_in_progress", request_id: requestId }, 409);
  }
  const claimToken = begin.payload?.claim_token;
  if (!claimToken) return json({ code: "claim_token_missing", request_id: requestId }, 503);
  if (faultMode === "crash_before_provider") return json({ code: "test_crash_before_provider" }, 503);
  const started = await serverRpc("mark_translation_provider_started", { p_request_id: requestId, p_claim_token: claimToken });
  if (!started.configured || !started.ok) return json({ code: "stale_claim", request_id: requestId }, 409);
  if (faultMode === "crash_after_provider_started") return json({ code: "provider_outcome_unknown", request_id: requestId }, 503);
  if (Deno.env.get("YOMU_TRANSLATION_PROVIDER") === "mock" && !localTestRuntime) {
    return json({ code: "provider_configuration_invalid" }, 503);
  }
  if (Deno.env.get("YOMU_TRANSLATION_PROVIDER") === "mock" && localTestRuntime) {
    if (faultMode === "timeout") return json({ code: "provider_timeout", request_id: requestId }, 504);
    if (faultMode === "429") return json({ code: "rate_limited", request_id: requestId }, 429);
    if (faultMode === "500") return json({ code: "provider_rejected", request_id: requestId }, 502);
    if (faultMode === "slow") await new Promise((resolve) => setTimeout(resolve, 750));
    const mockItems = items.map((item: any) => ({ item_id: item.item_id, translated_text: `[MOCK] ${item.text}` }));
    let resultItems = mockItems;
    if (faultMode === "partial") resultItems = mockItems.slice(0, -1);
    if (faultMode === "extra") resultItems = [...mockItems, { item_id: "extra", translated_text: "[MOCK] extra" }];
    if (faultMode === "duplicate") resultItems = [mockItems[0], mockItems[0]];
    if (faultMode === "unknown") resultItems = [{ item_id: "unknown", translated_text: "[MOCK] unknown" }];
    if (faultMode === "malformed") resultItems = [{ item_id: mockItems[0]?.item_id, translated_text: null }];
    if (["partial", "extra", "duplicate", "unknown", "malformed"].includes(faultMode)) return json({ code: "invalid_provider_response", request_id: requestId }, 502);
    const result = {
      items: resultItems,
      provider: "mock",
      metadata: {
        billed_characters: characterCount,
        detected_source_language: String(body?.source_lang || "JA"),
        model_type_used: "mock",
      },
    };
    const persisted = await serverRpc("persist_translation_result", { p_request_id: requestId, p_result: result, p_claim_token: claimToken });
    if (!persisted.configured || !persisted.ok) return json({ code: "translation_result_persist_failed", request_id: requestId }, 503);
    if (faultMode === "crash_after_result") return json({ code: "test_crash_after_result", request_id: requestId }, 503);
    const committed = faultMode === "commit_fail_once" ? false : await commitBatchAndMaybeFinalize(requestId, String(body?.job_id || ""), String(reservationId), { billed_characters: characterCount, detected_source_language: String(body?.source_lang || "JA"), model_type_used: "mock" }, result, finalizeJob);
    if (!committed) return json({ code: "translation_commit_failed", request_id: requestId }, 503);
    if (faultMode === "crash_after_commit") return json({ code: "test_response_lost", request_id: requestId }, 503);
    return json({ request_id: requestId, status: "completed", provider: "mock", items: resultItems, idempotent_replay: false }, 200);
  }
  const key = Deno.env.get("DEEPL_API_KEY");
  if (!key) return json({ code: "provider_not_configured" }, 503);
  const base = Deno.env.get("DEEPL_API_BASE_URL") || (key.endsWith(":fx") ? "https://api-free.deepl.com" : "https://api.deepl.com");
  if (!ALLOWED_BASES.has(base)) return json({ code: "provider_configuration_invalid" }, 503);
  let response: Response;
  try {
    response = await fetch(`${base}/v2/translate`, { method: "POST", headers: { Authorization: `DeepL-Auth-Key ${key}`, "Content-Type": "application/json" }, body: JSON.stringify({ text: items.map((i: any) => i.text), source_lang: String(body?.source_lang || "JA").toUpperCase(), target_lang: String(body?.target_lang || "PT-BR").toUpperCase(), model_type: "quality_optimized", show_billed_characters: true, preserve_formatting: true }) });
  } catch { return json({ code: "provider_outcome_unknown", request_id: requestId }, 502); }
  const payload: any = await response.json().catch(() => ({}));
  if (!response.ok) {
    await serverRpc("record_translation_failure", { p_request_id: requestId, p_error_code: `deepl_http_${response.status}`, p_release_reservation: response.status < 500 });
    return json({ code: response.status === 429 ? "rate_limited" : "provider_rejected", request_id: requestId }, 502);
  }
  const translations = Array.isArray(payload?.translations) ? payload.translations : [];
  if (translations.length !== items.length || translations.some((t: any) => typeof t?.text !== "string" || !t.text.trim())) {
    await serverRpc("record_translation_failure", { p_request_id: requestId, p_error_code: "provider_malformed_response", p_release_reservation: false });
    return json({ code: "provider_malformed_response", request_id: requestId }, 502);
  }
  const resultItems = translations.map((t: any, index: number) => ({ item_id: items[index].item_id, translated_text: t.text }));
  const firstTranslation = translations[0];
  const billedValues = translations.map((t: any) => Number(t?.billed_characters)).filter((n: number) => Number.isFinite(n));
  const billedCharacters = billedValues.length ? billedValues.reduce((sum: number, n: number) => sum + n, 0) : null;
  const detectedSourceLanguage = firstTranslation?.detected_source_language || null;
  const result = {
    items: resultItems,
    provider: "deepl",
    metadata: {
      billed_characters: billedCharacters,
      detected_source_language: detectedSourceLanguage,
      model_type_used: "quality_optimized",
    },
  };
  const persisted = await serverRpc("persist_translation_result", { p_request_id: requestId, p_result: result, p_claim_token: claimToken });
  if (!persisted.configured || !persisted.ok) return json({ code: "translation_result_persist_failed", request_id: requestId }, 503);
  const committed = await commitBatchAndMaybeFinalize(requestId, String(body?.job_id || ""), String(reservationId), { billed_characters: billedCharacters, detected_source_language: detectedSourceLanguage, model_type_used: "quality_optimized" }, result, finalizeJob);
  if (!committed) return json({ code: "translation_commit_failed", request_id: requestId }, 503);
  return json({ request_id: requestId, status: "completed", provider: "deepl", items: resultItems, idempotent_replay: false, detected_source_language: detectedSourceLanguage }, 200);
});
