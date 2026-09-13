export const corsHeaders = {
  "access-control-allow-origin": "http://127.0.0.1:8080",
  "access-control-allow-headers": "authorization, x-client-info, apikey, content-type",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-max-age": "86400",
  "vary": "Origin",
};
export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...corsHeaders } });
export const preflight = () => new Response(null, { status: 204, headers: corsHeaders });
