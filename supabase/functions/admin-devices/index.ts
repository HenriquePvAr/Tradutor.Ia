import { json } from "../_shared/http.ts"; import { requireAuth } from "../_shared/auth.ts";
Deno.serve((req)=>{const d=requireAuth(req); return d||json({code:"forbidden"},403);});
