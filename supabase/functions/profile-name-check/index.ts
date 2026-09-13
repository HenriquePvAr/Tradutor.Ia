import { json } from "../_shared/http.ts";
import { requireAuth } from "../_shared/auth.ts";
Deno.serve(async (req) => { const denied = requireAuth(req); if (denied) return denied; let body: any; try { body = await req.json(); } catch { return json({ code: "invalid_json" }, 400); } const name = String(body?.display_name || "").trim(); if (!name || name.length > 40) return json({ code: "invalid_display_name" }, 400); return json({ available: true, normalized: name.toLocaleLowerCase("pt-BR"), suggestions: [] }); });
