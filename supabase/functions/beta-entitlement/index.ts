import { bearer, requireAuth } from "../_shared/auth.ts";
import { json, preflight } from "../_shared/http.ts";

const CHANNEL = "scan-beta";
const unavailable = () => json({ licensed: false, reason: "license_unavailable" }, 503);

function hasStarted(value: unknown, now: number): boolean {
  return value == null || (typeof value === "string" && Number.isFinite(Date.parse(value)) && Date.parse(value) <= now);
}

function isUnexpired(value: unknown, now: number): boolean {
  return value == null || (typeof value === "string" && Number.isFinite(Date.parse(value)) && Date.parse(value) > now);
}

async function fetchJson(url: string, headers: HeadersInit): Promise<{ response: Response; payload: any }> {
  const response = await fetch(url, { headers });
  return { response, payload: await response.json().catch(() => null) };
}

export async function handleRequest(req: Request): Promise<Response> {
  if (req.method === "OPTIONS") return preflight();
  if (req.method !== "POST") return json({ licensed: false, reason: "method_not_allowed" }, 405);

  const denied = requireAuth(req);
  if (denied) return denied;

  const token = bearer(req);
  const url = Deno.env.get("SUPABASE_URL");
  const key = Deno.env.get("SUPABASE_ANON_KEY");
  if (!token || !url || !key) return unavailable();

  const headers = { apikey: key, Authorization: `Bearer ${token}` };
  try {
    const identity = await fetchJson(`${url}/auth/v1/user`, headers);
    if (!identity.response.ok || typeof identity.payload?.id !== "string") {
      return json({ licensed: false, reason: "unauthenticated" }, 401);
    }
    const userId = identity.payload.id;
    const entitlementUrl = new URL(`${url}/rest/v1/beta_tester_entitlements`);
    entitlementUrl.searchParams.set("select", "id,user_id,status,starts_at,expires_at,revoked_at,source_license_id");
    entitlementUrl.searchParams.set("user_id", `eq.${userId}`);
    entitlementUrl.searchParams.set("beta_channel", `eq.${CHANNEL}`);
    entitlementUrl.searchParams.set("limit", "1");
    const entitlement = await fetchJson(entitlementUrl.toString(), headers);
    if (!entitlement.response.ok || !Array.isArray(entitlement.payload)) return unavailable();
    const record = entitlement.payload[0];
    if (!record) return json({ licensed: false, reason: "no_entitlement" });

    const now = Date.now();
    if (record.revoked_at != null || record.status === "revoked") return json({ licensed: false, reason: "revoked" });
    if (record.status !== "active") return json({ licensed: false, reason: "no_entitlement" });
    if (!hasStarted(record.starts_at, now)) return json({ licensed: false, reason: "not_started" });
    if (!isUnexpired(record.expires_at, now)) return json({ licensed: false, reason: "expired" });

    if (record.source_license_id != null) {
      const licenseUrl = new URL(`${url}/rest/v1/licenses`);
      licenseUrl.searchParams.set("select", "id,user_id,status,starts_at,expires_at,revoked_at");
      licenseUrl.searchParams.set("id", `eq.${record.source_license_id}`);
      licenseUrl.searchParams.set("user_id", `eq.${userId}`);
      licenseUrl.searchParams.set("limit", "1");
      const license = await fetchJson(licenseUrl.toString(), headers);
      const linked = license.payload?.[0];
      if (!license.response.ok || !linked) return unavailable();
      if (linked.revoked_at != null || linked.status === "revoked") return json({ licensed: false, reason: "revoked" });
      if (linked.status !== "active") return json({ licensed: false, reason: "no_entitlement" });
      if (!hasStarted(linked.starts_at, now)) return json({ licensed: false, reason: "not_started" });
      if (!isUnexpired(linked.expires_at, now)) return json({ licensed: false, reason: "expired" });
    }

    return json({ licensed: true, reason: "active" });
  } catch {
    return unavailable();
  }
}

if (import.meta.main) Deno.serve(handleRequest);
