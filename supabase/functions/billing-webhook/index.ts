import { json } from "../_shared/http.ts";
Deno.serve((req)=>req.headers.get("x-webhook-signature")?json({accepted:false,code:"provider_disabled"},503):json({code:"invalid_signature"},401));
