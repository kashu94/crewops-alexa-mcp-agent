import "server-only";

import { cookies } from "next/headers";

import { refresh, type Tokens } from "./cognito";
import { expiresSoon } from "./tokens";

// The tokens live in httpOnly cookies: page scripts never see the access or refresh
// token. Only /api/live hands the ID token to the page, for the live-events socket.
const ACCESS = "crewops_access";
const ID = "crewops_id";
const REFRESH = "crewops_refresh";
const REFRESH_DAYS = 30;

function options(maxAge: number) {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    maxAge,
  };
}

export async function saveTokens(tokens: Tokens): Promise<void> {
  const jar = await cookies();
  jar.set(ACCESS, tokens.access, options(60 * 60));
  jar.set(ID, tokens.id, options(60 * 60));
  if (tokens.refresh) jar.set(REFRESH, tokens.refresh, options(REFRESH_DAYS * 24 * 60 * 60));
}

export async function clearTokens(): Promise<void> {
  const jar = await cookies();
  for (const name of [ACCESS, ID, REFRESH]) jar.delete(name);
}

/** Current tokens, refreshed first when they are about to expire; null when signed out. */
export async function currentTokens(): Promise<Tokens | null> {
  const jar = await cookies();
  const access = jar.get(ACCESS)?.value;
  const id = jar.get(ID)?.value;
  const now = Math.floor(Date.now() / 1000);
  if (access && id && !expiresSoon(access, now) && !expiresSoon(id, now)) {
    return { access, id };
  }
  const stored = jar.get(REFRESH)?.value;
  if (!stored) return null;
  const fresh = await refresh(stored);
  if (!fresh) {
    await clearTokens();
    return null;
  }
  await saveTokens(fresh);
  return fresh;
}
