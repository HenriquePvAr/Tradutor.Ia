import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const ALLOWED = new Set(["image/png", "image/jpeg", "image/webp"]);
const LIMITS: Record<string, number> = { avatar: 5 * 1024 * 1024, banner: 12 * 1024 * 1024 };
const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", ...headers } });

function bearer(req: Request): string {
  const value = req.headers.get("authorization") || "";
  return value.toLowerCase().startsWith("bearer ") ? value.slice(7).trim() : "";
}

async function currentUser(token: string) {
  const base = Deno.env.get("SUPABASE_URL") || "";
  const key = Deno.env.get("SUPABASE_ANON_KEY") || Deno.env.get("SUPABASE_PUBLISHABLE_KEY") || "";
  if (!base || !key) return null;
  const response = await fetch(`${base}/auth/v1/user`, { headers: { apikey: key, Authorization: `Bearer ${token}` } });
  if (!response.ok) return null;
  const value = await response.json();
  return value?.id ? value : null;
}

async function driveAccessToken(): Promise<string> {
  const clientId = Deno.env.get("GOOGLE_OAUTH_CLIENT_ID") || "";
  const clientSecret = Deno.env.get("GOOGLE_OAUTH_CLIENT_SECRET") || "";
  const refresh = Deno.env.get("GOOGLE_REFRESH_TOKEN") || "";
  if (!clientId || !clientSecret || !refresh) throw new Error("drive_provider_not_configured");
  const body = new URLSearchParams({ client_id: clientId, client_secret: clientSecret, refresh_token: refresh, grant_type: "refresh_token" });
  const response = await fetch("https://oauth2.googleapis.com/token", { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
  if (!response.ok) throw new Error("drive_token_refresh_failed");
  const value = await response.json();
  if (!value?.access_token) throw new Error("drive_token_refresh_failed");
  return String(value.access_token);
}

function validMagic(bytes: Uint8Array, mime: string): boolean {
  if (mime === "image/png") return bytes.length > 8 && bytes.slice(0, 8).every((v, i) => v === [137,80,78,71,13,10,26,10][i]);
  if (mime === "image/jpeg") return bytes.length > 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  if (mime === "image/webp") return bytes.length > 12 && new TextDecoder().decode(bytes.slice(0, 4)) === "RIFF" && new TextDecoder().decode(bytes.slice(8, 12)) === "WEBP";
  return false;
}

async function uploadDrive(content: Uint8Array, mime: string, kind: string, userId: string, access: string): Promise<string> {
  const root = Deno.env.get("COMMUNITY_DRIVE_ROOT_FOLDER_ID") || "";
  if (!root) throw new Error("drive_provider_not_configured");
  const boundary = "yomu" + crypto.randomUUID().replaceAll("-", "");
  const metadata = JSON.stringify({ name: `profile-${userId}-${kind}-${crypto.randomUUID()}`, mimeType: mime, parents: [root] });
  const enc = new TextEncoder();
  const pre = enc.encode(`--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${metadata}\r\n--${boundary}\r\nContent-Type: ${mime}\r\n\r\n`);
  const post = enc.encode(`\r\n--${boundary}--`);
  const body = new Uint8Array(pre.length + content.length + post.length); body.set(pre); body.set(content, pre.length); body.set(post, pre.length + content.length);
  const response = await fetch("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id", { method: "POST", headers: { Authorization: `Bearer ${access}`, "Content-Type": `multipart/related; boundary=${boundary}`, "Content-Length": String(body.length) }, body });
  if (!response.ok) throw new Error("drive_upload_failed");
  const value = await response.json();
  if (!value?.id) throw new Error("drive_upload_failed");
  return String(value.id);
}

async function updateProfile(token: string, userId: string, kind: string, fileId: string, mime: string, size: number): Promise<boolean> {
  const base = Deno.env.get("SUPABASE_URL") || "";
  const key = Deno.env.get("SUPABASE_ANON_KEY") || Deno.env.get("SUPABASE_PUBLISHABLE_KEY") || "";
  const field = `${kind}_object_key`;
  const response = await fetch(`${base}/rest/v1/profiles?id=eq.${encodeURIComponent(userId)}`, { method: "PATCH", headers: { apikey: key, Authorization: `Bearer ${token}`, "Content-Type": "application/json", Prefer: "return=representation" }, body: JSON.stringify({ [field]: `drive:${fileId}` }) });
  if (!response.ok) return false;
  const rows = await response.json();
  return Array.isArray(rows) && rows.length === 1 && rows[0]?.[field] === `drive:${fileId}`;
}

async function deleteDriveFile(fileId: string, access: string): Promise<void> {
  if (!fileId) return;
  try { await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}`, { method: "DELETE", headers: { Authorization: `Bearer ${access}` } }); } catch (_) { /* best effort */ }
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization, apikey, content-type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS" } });
  if (req.method !== "GET" && req.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const token = bearer(req);
  if (!token) return json({ error: "authentication_required" }, 401);
  const kind = new URL(req.url).searchParams.get("kind") || "";
  if (!(kind in LIMITS)) return json({ error: "invalid_media_kind" }, 400);
  const user = await currentUser(token);
  if (!user) return json({ error: "authentication_required" }, 401);
  if (req.method === "POST") {
    const mime = String(req.headers.get("content-type") || "").split(";", 1)[0].toLowerCase();
    const limit = LIMITS[kind];
    const content = new Uint8Array(await req.arrayBuffer());
    if (!ALLOWED.has(mime) || !content.length || content.length > limit || !validMagic(content, mime)) return json({ error: "invalid_media" }, 400);
    let stage = "oauth_refresh";
    try {
      const access = await driveAccessToken();
      stage = "drive_upload";
      const fileId = await uploadDrive(content, mime, kind, String(user.id), access);
      stage = "profile_patch";
      if (!(await updateProfile(token, String(user.id), kind, fileId, mime, content.length))) {
        await deleteDriveFile(fileId, access);
        return json({ error: "profile_update_failed", stage }, 502);
      }
      return json({ ok: true, kind, media_type: mime, media_size: content.length });
    } catch (_) { return json({ error: "media_upload_unavailable", stage }, 502); }
  }
  const base = Deno.env.get("SUPABASE_URL") || "";
  const key = Deno.env.get("SUPABASE_ANON_KEY") || Deno.env.get("SUPABASE_PUBLISHABLE_KEY") || "";
  const profileResponse = await fetch(`${base}/rest/v1/profiles?id=eq.${encodeURIComponent(user.id)}&select=${kind}_object_key`, { headers: { apikey: key, Authorization: `Bearer ${token}` } });
  if (!profileResponse.ok) return json({ error: "profile_lookup_failed" }, 502);
  const rows = await profileResponse.json();
  const profile = Array.isArray(rows) ? rows[0] : null;
  const ref = String(profile?.[`${kind}_object_key`] || "");
  if (!ref.startsWith("drive:") || !ref.slice(6)) return json({ error: "media_not_found" }, 404);
  const access = await driveAccessToken();
  const fileId = encodeURIComponent(ref.slice(6));
  const metaResponse = await fetch(`https://www.googleapis.com/drive/v3/files/${fileId}?fields=id,mimeType,size,trashed`, { headers: { Authorization: `Bearer ${access}` } });
  if (!metaResponse.ok) return json({ error: "media_not_found" }, 404);
  const meta = await metaResponse.json();
  const mime = String(meta?.mimeType || "");
  const size = Number(meta?.size || 0);
  if (meta?.trashed || !ALLOWED.has(mime) || !size || size > LIMITS[kind]) return json({ error: "media_not_found" }, 404);
  const mediaResponse = await fetch(`https://www.googleapis.com/drive/v3/files/${fileId}?alt=media`, { headers: { Authorization: `Bearer ${access}` } });
  if (!mediaResponse.ok || !mediaResponse.body) return json({ error: "media_unavailable" }, 502);
  return new Response(mediaResponse.body, { status: 200, headers: { "Content-Type": mime, "Content-Length": String(size), "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Access-Control-Allow-Origin": "*" } });
});
