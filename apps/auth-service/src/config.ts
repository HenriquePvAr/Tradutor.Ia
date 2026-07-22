import path from "node:path";
import { fileURLToPath } from "node:url";

export type AuthServiceConfig = {
  host: string;
  port: number;
  databasePath: string;
  baseUrl: string;
  secret: string;
  trustedOrigins: string[];
  googleEnabled: boolean;
};

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..", "..");

function env(name: string, fallback = ""): string {
  return String(process.env[name] ?? fallback).trim();
}

function requireSecret(value: string): string {
  if (Buffer.byteLength(value, "utf8") < 32) {
    throw new Error("BETTER_AUTH_SECRET must contain at least 32 bytes");
  }
  return value;
}

function parsePort(value: string): number {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1024 || parsed > 65535) {
    throw new Error("TRADUTOR_AUTH_SERVICE_PORT must be a TCP port between 1024 and 65535");
  }
  return parsed;
}

export function loadConfig(): AuthServiceConfig {
  const host = env("TRADUTOR_AUTH_SERVICE_HOST", "127.0.0.1");
  if (host !== "127.0.0.1" && host !== "localhost") {
    throw new Error("auth-service must bind to loopback only");
  }
  const port = parsePort(env("TRADUTOR_AUTH_SERVICE_PORT", "8787"));
  const baseUrl = env("BETTER_AUTH_URL", "http://127.0.0.1:8080");
  const db = env(
    "BETTER_AUTH_DATABASE_PATH",
    path.join(repoRoot, ".cache", "runtime", "better-auth.sqlite3"),
  );
  const trustedOrigins = env("BETTER_AUTH_TRUSTED_ORIGINS", baseUrl)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    host,
    port,
    baseUrl,
    databasePath: db,
    secret: requireSecret(env("BETTER_AUTH_SECRET", "")),
    trustedOrigins,
    googleEnabled: Boolean(env("GOOGLE_CLIENT_ID") && env("GOOGLE_CLIENT_SECRET")),
  };
}

