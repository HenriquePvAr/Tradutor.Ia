import { bearer } from './auth.ts';
export async function callRpc(req: Request, name: string, body: unknown): Promise<{ response?: Response; payload?: any; error?: string }> {
  const url=Deno.env.get('SUPABASE_URL'), key=Deno.env.get('SUPABASE_ANON_KEY'), token=bearer(req);
  if(!url||!key||!token) return {error:'control_plane_not_configured'};
  const response=await fetch(`${url}/rest/v1/rpc/${name}`,{method:'POST',headers:{apikey:key,Authorization:`Bearer ${token}`,'content-type':'application/json'},body:JSON.stringify(body)});
  const payload=await response.json().catch(()=>({})); return {response,payload};
}
