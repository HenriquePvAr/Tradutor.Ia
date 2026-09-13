import { json } from "../_shared/http.ts";
Deno.serve(()=>json({channel:"beta",version:"0.0.0",mandatory:false,signature:null}));
