import { describe, expect, test, vi } from "vitest";
import { loadConfig } from "../src/config.js";

describe("auth-service config", () => {
  test("fails closed without a high-entropy secret", () => {
    vi.stubEnv("BETTER_AUTH_SECRET", "short");
    expect(() => loadConfig()).toThrow(/BETTER_AUTH_SECRET/);
    vi.unstubAllEnvs();
  });

  test("binds only to loopback", () => {
    vi.stubEnv("BETTER_AUTH_SECRET", "x".repeat(40));
    vi.stubEnv("TRADUTOR_AUTH_SERVICE_HOST", "0.0.0.0");
    expect(() => loadConfig()).toThrow(/loopback/);
    vi.unstubAllEnvs();
  });

  test("keeps google disabled unless both credentials exist", () => {
    vi.stubEnv("BETTER_AUTH_SECRET", "x".repeat(40));
    vi.stubEnv("GOOGLE_CLIENT_ID", "client");
    vi.stubEnv("GOOGLE_CLIENT_SECRET", "");
    expect(loadConfig().googleEnabled).toBe(false);
    vi.stubEnv("GOOGLE_CLIENT_SECRET", "secret");
    expect(loadConfig().googleEnabled).toBe(true);
    vi.unstubAllEnvs();
  });
});

