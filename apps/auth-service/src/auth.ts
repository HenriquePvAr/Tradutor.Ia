import fs from "node:fs";
import path from "node:path";
import Database from "better-sqlite3";
import { betterAuth } from "better-auth";
import { getMigrations } from "better-auth/db/migration";
import type { AuthServiceConfig } from "./config.js";

function authOptions(config: AuthServiceConfig, database: Database.Database) {
  return {
    baseURL: config.baseUrl,
    basePath: "/api/auth",
    secret: config.secret,
    database,
    trustedOrigins: config.trustedOrigins,
    advanced: {
      cookiePrefix: "tradutor-auth",
      defaultCookieAttributes: {
        httpOnly: true,
        sameSite: "lax" as const,
        secure: config.baseUrl.startsWith("https://"),
        path: "/",
      },
    },
    emailAndPassword: {
      enabled: true,
      autoSignIn: true,
    },
    socialProviders: config.googleEnabled
      ? {
          google: {
            clientId: process.env.GOOGLE_CLIENT_ID as string,
            clientSecret: process.env.GOOGLE_CLIENT_SECRET as string,
          },
        }
      : {},
  };
}

export async function prepareAuthDatabase(config: AuthServiceConfig) {
  fs.mkdirSync(path.dirname(config.databasePath), { recursive: true });
  const database = new Database(config.databasePath);
  try {
    const migrations = await getMigrations(authOptions(config, database));
    if (migrations.toBeCreated.length || migrations.toBeAdded.length) {
      await migrations.runMigrations();
    }
  } finally {
    database.close();
  }
}

export function createAuth(config: AuthServiceConfig) {
  fs.mkdirSync(path.dirname(config.databasePath), { recursive: true });
  const database = new Database(config.databasePath);
  return betterAuth(authOptions(config, database));
}

export type TradutorAuth = ReturnType<typeof createAuth>;
