import { assertEquals } from "jsr:@std/assert";
import { handleRequest } from "./index.ts";

const originalFetch = globalThis.fetch;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function request(token = "valid-token") {
  return new Request("https://edge.test/functions/v1/beta-entitlement", {
    method: "POST", headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
}

function mockRemote(entitlement: unknown, license: unknown = []) {
  Deno.env.set("SUPABASE_URL", "https://example.supabase.co");
  Deno.env.set("SUPABASE_ANON_KEY", "public-test-key");
  globalThis.fetch = async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return response({ id: "user-a" });
    if (url.includes("/rest/v1/licenses")) return response(license);
    return response(Array.isArray(entitlement) ? entitlement.filter((row) => row?.user_id === "user-a") : entitlement);
  };
}

Deno.test("rejects missing authorization", async () => {
  assertEquals((await handleRequest(request(""))).status, 401);
});

Deno.test("returns active entitlement for JWT identity", async () => {
  mockRemote([{ user_id: "user-a", status: "active", starts_at: null, expires_at: "2999-01-01T00:00:00Z", revoked_at: null }]);
  assertEquals(await (await handleRequest(request())).json(), { licensed: true, reason: "active" });
});

for (const [name, row, reason] of [
  ["missing", [], "no_entitlement"],
  ["revoked", [{ user_id: "user-a", status: "revoked", expires_at: "2999-01-01T00:00:00Z", revoked_at: "2026-01-01T00:00:00Z" }], "revoked"],
  ["expired", [{ user_id: "user-a", status: "active", starts_at: null, expires_at: "2020-01-01T00:00:00Z", revoked_at: null }], "expired"],
  ["not started", [{ user_id: "user-a", status: "active", starts_at: "2999-01-01T00:00:00Z", expires_at: "2999-01-02T00:00:00Z", revoked_at: null }], "not_started"],
] as const) {
  Deno.test(`returns ${name} safely`, async () => {
    mockRemote(row);
    assertEquals(await (await handleRequest(request())).json(), { licensed: false, reason });
  });
}

Deno.test("fails closed on backend error and supports preflight", async () => {
  mockRemote(null);
  globalThis.fetch = async () => { throw new Error("backend failure"); };
  assertEquals((await handleRequest(request())).status, 503);
  assertEquals((await handleRequest(new Request("https://edge.test", { method: "OPTIONS" }))).status, 204);
});

Deno.test("does not accept an unrelated user row", async () => {
  mockRemote([{ user_id: "user-b", status: "active", starts_at: null, expires_at: "2999-01-01T00:00:00Z", revoked_at: null }]);
  assertEquals(await (await handleRequest(request())).json(), { licensed: false, reason: "no_entitlement" });
});

Deno.test("restores fetch", () => { globalThis.fetch = originalFetch; });
