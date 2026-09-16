import { json } from "../_shared/http.ts";

const safe = (v: string | null) => String(v || "").trim().slice(0, 256);
const canonical = (p: URLSearchParams) => {
  const pairs: string[] = [];
  for (const [key, value] of p.entries()) if (key !== "signature" && key !== "security_hash") pairs.push(`${encodeURIComponent(key)}=${encodeURIComponent(value).replace(/%20/g, "+")}`);
  return pairs.sort().join("&");
};
async function hmacHex(secret: string, payload: string) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload)));
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function serviceRpc(name: string, body: unknown) {
  const url = Deno.env.get("SUPABASE_URL"), key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || Deno.env.get("SUPABASE_SECRET_KEY");
  if (!url || !key) return { configured: false, ok: false, payload: {} };
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, { method: "POST", headers: { apikey: key, Authorization: `Bearer ${key}`, "content-type": "application/json" }, body: JSON.stringify(body) });
  return { configured: true, ok: response.ok, payload: await response.json().catch(() => ({})) };
}
Deno.serve(async (req) => {
  if (req.method !== "GET" && req.method !== "POST") return json({ code: "method_not_allowed" }, 405);
  return json({ code: "rewarded_ads_disabled" }, 503);
  const p = new URL(req.url).searchParams;
  const secret = Deno.env.get("AYET_PUBLISHER_API_KEY") || "";
  const signature = safe(req.headers.get("X-Ayetstudios-Security-Hash"));
  const transaction = safe(p.get("transaction_id"));
  const external = safe(p.get("external_identifier"));
  const adslot = safe(p.get("adslot_id"));
  const amount = Number(p.get("amount") || "0");
  if (!secret || !transaction || !external || !adslot || amount !== 1 || !signature) return json({ code: "invalid_reward_callback" }, 200);
  const expected = await hmacHex(secret, canonical(p));
  if (expected.length !== signature.length) return json({ code: "invalid_reward_signature" }, 200);
  let mismatch = 0; for (let i = 0; i < expected.length; i++) mismatch |= expected.charCodeAt(i) ^ signature.toLowerCase().charCodeAt(i);
  if (mismatch !== 0) return json({ code: "invalid_reward_signature" }, 200);
  const result = await serviceRpc("credit_ayet_rewarded_ad", { p_external_identifier: external, p_transaction_id: transaction, p_amount: amount, p_adslot_id: adslot });
  return result.configured && result.ok ? json({ ok: true, credited: result.payload?.credited === true }, 200) : json({ code: "reward_persistence_unavailable" }, 200);
});
