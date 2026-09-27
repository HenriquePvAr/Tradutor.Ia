import { handleWalletSettleJob } from "./index.ts";
function assertEquals(actual: unknown, expected: unknown) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`assertion failed: ${JSON.stringify(actual)} !== ${JSON.stringify(expected)}`);
  }
}

const rid = "550e8400-e29b-41d4-a716-446655440001";
const base = { job_id: "job-1", reservation_id: rid, action: "consume",
  idempotency_key: "yk-settlement-v1:0123456789abcdef:consume" };
function req(body: unknown, auth = "Bearer user-jwt") {
  return new Request("http://localhost/functions/v1/wallet-settle-job", {
    method: "POST", headers: { authorization: auth, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

Deno.test("settlement forwards only identity, action and idempotency to owner-checking RPC", async () => {
  let captured: unknown;
  const response = await handleWalletSettleJob(req(base), async (_req, name, body) => {
    captured = { name, body };
    return { response: new Response(null, { status: 200 }), payload: { status: "consumed" } };
  });
  assertEquals(response.status, 200);
  assertEquals(captured, { name: "settle_translation_job", body: {
    p_job_id: base.job_id, p_reservation_id: rid, p_action: "consume",
    p_idempotency_key: base.idempotency_key,
  } });
});

Deno.test("missing auth is denied before RPC", async () => {
  let called = false;
  const response = await handleWalletSettleJob(req(base, ""), async () => {
    called = true; return {};
  });
  assertEquals(response.status, 401);
  assertEquals(called, false);
});

Deno.test("client amount or translation content is prohibited", async () => {
  for (const extra of [{ amount: 1 }, { text: "chapter text" }, { finalize_items: [] }]) {
    let called = false;
    const response = await handleWalletSettleJob(req({ ...base, ...extra }), async () => {
      called = true; return {};
    });
    assertEquals(response.status, 400);
    assertEquals(called, false);
  }
});

Deno.test("wrong owner/job association is denied by the authoritative RPC", async () => {
  const response = await handleWalletSettleJob(req(base), async () => ({
    response: new Response(null, { status: 400 }), payload: { message: "reservation_not_found" },
  }));
  assertEquals(response.status, 409);
  assertEquals((await response.json()).code, "reservation_not_found");
});
