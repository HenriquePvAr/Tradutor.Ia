export function bearer(req: Request): string | null {
  const value = req.headers.get("authorization") || "";
  return value.startsWith("Bearer ") && value.length > 12 ? value.slice(7) : null;
}
export function requireAuth(req: Request): Response | null {
  return bearer(req) ? null : new Response(JSON.stringify({ code: "unauthenticated" }), { status: 401, headers: { "content-type": "application/json" } });
}
