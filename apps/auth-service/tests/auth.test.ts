import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import Database from "better-sqlite3";
import { afterEach, describe, expect, test } from "vitest";
import { createAuth, prepareAuthDatabase } from "../src/auth.js";
import type { AuthServiceConfig } from "../src/config.js";

const tmpDirs: string[] = [];

function testConfig(databasePath: string): AuthServiceConfig {
  return {
    host: "127.0.0.1",
    port: 8787,
    databasePath,
    baseUrl: "http://127.0.0.1:8080",
    secret: "x".repeat(40),
    trustedOrigins: ["http://127.0.0.1:8080"],
    googleEnabled: false,
  };
}

function tempDbPath(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tradutor-auth-service-"));
  tmpDirs.push(dir);
  return path.join(dir, "better-auth.sqlite3");
}

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EPERM") {
        throw error;
      }
    }
  }
});

describe("Better Auth local database", () => {
  test("prepares the sqlite schema before handling password sign-up", async () => {
    const dbPath = tempDbPath();
    const config = testConfig(dbPath);

    await prepareAuthDatabase(config);

    const db = new Database(dbPath, { readonly: true });
    try {
      expect(db.prepare("select name from sqlite_master where type = 'table' and name = ?").get("user")).toBeTruthy();
      expect(db.prepare("select name from sqlite_master where type = 'table' and name = ?").get("session")).toBeTruthy();
      expect(db.prepare("select name from sqlite_master where type = 'table' and name = ?").get("account")).toBeTruthy();
      expect(db.prepare("select name from sqlite_master where type = 'table' and name = ?").get("verification")).toBeTruthy();
    } finally {
      db.close();
    }

    const auth = createAuth(config);
    const response = await auth.handler(new Request("http://127.0.0.1:8080/api/auth/sign-up/email", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "accept": "application/json",
        "origin": "http://127.0.0.1:8080",
      },
      body: JSON.stringify({
        email: "synthetic-auth@example.invalid",
        password: "local-validation-password-123",
        name: "Kayden",
      }),
    }));

    expect(response.status).not.toBe(500);
    expect(response.headers.has("set-cookie")).toBe(true);
  });
});
