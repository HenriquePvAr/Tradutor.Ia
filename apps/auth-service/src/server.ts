import { serve } from "@hono/node-server";
import { Hono } from "hono";
import { createAuth } from "./auth.js";
import { loadConfig } from "./config.js";
import { getSanitizedSession } from "./session.js";

const config = loadConfig();
const auth = createAuth(config);
const app = new Hono();

function noStore(response: Response): Response {
  response.headers.set("Cache-Control", "no-store");
  return response;
}

app.get("/internal/auth/ok", (c) => c.json({
  ok: true,
  provider: "better_auth",
  googleEnabled: config.googleEnabled,
}));

app.get("/internal/auth/session", async (c) => {
  return c.json(await getSanitizedSession(auth, c.req.raw.headers), 200, {
    "Cache-Control": "no-store",
  });
});

app.on(["GET", "POST"], "/api/auth/*", async (c) => noStore(await auth.handler(c.req.raw)));

app.notFound((c) => c.json({ error: "not_found" }, 404));

serve({
  fetch: app.fetch,
  hostname: config.host,
  port: config.port,
});

console.log(`tradutor auth-service listening on ${config.host}:${config.port}`);

