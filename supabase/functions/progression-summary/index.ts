import { json } from "../_shared/http.ts"; import { requireAuth } from "../_shared/auth.ts"; import { callRpc } from "../_shared/rpc.ts";
Deno.serve(async(req)=>{const d=requireAuth(req); if(d)return d; const r=await callRpc(req,"progression_summary",{}); if(r.error)return json({code:r.error},503); return r.response?.ok?json(r.payload):json({code:"progression_unavailable"},503);});
