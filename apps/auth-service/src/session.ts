import type { TradutorAuth } from "./auth.js";

export type SanitizedSession = {
  authenticated: boolean;
  user: {
    id: string;
    name: string;
    emailVerified: boolean;
  } | null;
  session: {
    expiresAt: string;
  } | null;
};

export async function getSanitizedSession(auth: TradutorAuth, headers: Headers): Promise<SanitizedSession> {
  const session = await auth.api.getSession({ headers });
  if (!session?.user || !session?.session) {
    return { authenticated: false, user: null, session: null };
  }
  return {
    authenticated: true,
    user: {
      id: String(session.user.id || ""),
      name: String(session.user.name || ""),
      emailVerified: Boolean(session.user.emailVerified),
    },
    session: {
      expiresAt: new Date(session.session.expiresAt).toISOString(),
    },
  };
}

