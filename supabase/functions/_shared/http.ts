export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json", "access-control-allow-origin": "http://127.0.0.1:8080" },
});
export const methodGuard = (req: Request, method = "POST") => req.method === method ? null : json({ code: "method_not_allowed" }, 405);
