const publicJson = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "content-type": "application/json", "cache-control": "no-store", "access-control-allow-origin": "*" },
});

// Public read-only metadata endpoint. Publication tooling injects the already-signed
// manifest as UPDATE_MANIFEST_JSON; this function never owns a signing key or mutates data.
Deno.serve((request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, OPTIONS" } });
  }
  if (request.method !== "GET") {
    return publicJson({ error: "method_not_allowed" }, 405);
  }
  const raw = Deno.env.get("UPDATE_MANIFEST_JSON")?.trim();
  if (!raw) return publicJson({ error: "manifest_not_published" }, 404);
  try {
    const document = JSON.parse(raw);
    if (!document || typeof document !== "object" || !document.payload || document.payload.channel !== "beta" || !document.signature || !document.key_id) {
      return publicJson({ error: "manifest_unavailable" }, 503);
    }
    return publicJson(document);
  } catch (_error) {
    return publicJson({ error: "manifest_unavailable" }, 503);
  }
});
