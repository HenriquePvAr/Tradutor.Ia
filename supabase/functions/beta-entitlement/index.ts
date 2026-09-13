import { json } from "../_shared/http.ts"; import { requireAuth } from "../_shared/auth.ts";
Deno.serve((req)=>{const d=requireAuth(req); return d||json({licensed:false,reason:"license_unavailable"});});
