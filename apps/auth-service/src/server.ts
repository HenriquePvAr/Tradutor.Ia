import { serve } from "@hono/node-server";
import { Hono } from "hono";
import Database from "better-sqlite3";
import { createAuth, prepareAuthDatabase } from "./auth.js";
import { loadConfig } from "./config.js";
import type { AuthServiceConfig } from "./config.js";
import { getSanitizedSession } from "./session.js";

const config = loadConfig();
await prepareAuthDatabase(config);
const auth = createAuth(config);
const app = new Hono();

function noStore(response: Response): Response {
  response.headers.set("Cache-Control", "no-store");
  return response;
}

function databaseReady(databasePath: string): boolean {
  const db = new Database(databasePath, { readonly: true });
  try {
    const tables = ["user", "session", "account", "verification"];
    return tables.every((table) => Boolean(
      db.prepare("select name from sqlite_master where type = 'table' and name = ?").get(table),
    ));
  } finally {
    db.close();
  }
}

function healthPayload(serviceConfig: AuthServiceConfig, database = false) {
  return {
    ok: true,
    provider: "better_auth",
    googleEnabled: serviceConfig.googleEnabled,
    databaseReady: database,
  };
}

app.get("/internal/auth/live", (c) => c.json(healthPayload(config)));

app.get("/internal/auth/db-ready", (c) => {
  try {
    const ready = databaseReady(config.databasePath);
    return c.json(healthPayload(config, ready), ready ? 200 : 503);
  } catch (_) {
    return c.json({ ok: false, provider: "better_auth", databaseReady: false }, 503);
  }
});

app.get("/internal/auth/ready", (c) => {
  try {
    const ready = databaseReady(config.databasePath);
    return c.json(healthPayload(config, ready), ready ? 200 : 503, {
      "Cache-Control": "no-store",
    });
  } catch (_) {
    return c.json({ ok: false, provider: "better_auth", databaseReady: false }, 503, {
      "Cache-Control": "no-store",
    });
  }
});

app.get("/internal/auth/ok", (c) => c.json(healthPayload(config, true)));

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
