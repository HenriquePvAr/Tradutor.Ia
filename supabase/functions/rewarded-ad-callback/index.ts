import { json } from "../_shared/http.ts";
Deno.serve(()=>json({code:"rewarded_ads_disabled"},503));
