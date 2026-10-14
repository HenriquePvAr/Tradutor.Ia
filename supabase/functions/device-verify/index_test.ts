import { assertEquals } from "jsr:@std/assert";
import { handleRequest } from "./index.ts";

const savedFetch = globalThis.fetch;
const savedUrl = Deno.env.get("SUPABASE_URL");
const savedKey = Deno.env.get("SUPABASE_ANON_KEY");

Deno.test("device verification fails closed for revoked or expired authority", async () => {
  Deno.env.set("SUPABASE_URL", "https://example.supabase.co");
  Deno.env.set("SUPABASE_ANON_KEY", "public-test-key");

  const cases = [
    [{ device_status: "revoked", license_status: "active" }, "device_revoked"],
    [{ device_status: "unknown", license_status: "active" }, "device_revoked"],
    [{ device_status: "active", license_status: "revoked" }, "license_not_active"],
    [{ device_status: "active", license_status: "active", license_expires_at: "2000-01-01T00:00:00Z" }, "license_not_active"],
    [{ device_status: "active", license_status: "active", license_expires_at: "invalid" }, "license_not_active"],
  ] as const;

  try {
    for (const [state, expectedCode] of cases) {
      let calls = 0;
      globalThis.fetch = async () => {
        calls += 1;
        return new Response(JSON.stringify({
          id: "challenge-1",
          expires_at: "2999-01-01T00:00:00Z",
          consumed_at: null,
          nonce_hash: "unused",
          public_key: "unused",
          ...state,
        }), { status: 200 });
      };

      const response = await handleRequest(new Request("https://edge.test/device-verify", {
        method: "POST",
        headers: { authorization: "Bearer test-token", "content-type": "application/json" },
        body: JSON.stringify({ challenge_id: "challenge-1", nonce: "n", signature: "s" }),
      }));

      assertEquals(response.status, 403);
      assertEquals(await response.json(), { code: expectedCode });
      assertEquals(calls, 1, "revoked state must stop before challenge consumption");
    }
  } finally {
    globalThis.fetch = savedFetch;
    if (savedUrl === undefined) Deno.env.delete("SUPABASE_URL");
    else Deno.env.set("SUPABASE_URL", savedUrl);
    if (savedKey === undefined) Deno.env.delete("SUPABASE_ANON_KEY");
    else Deno.env.set("SUPABASE_ANON_KEY", savedKey);
  }
});
