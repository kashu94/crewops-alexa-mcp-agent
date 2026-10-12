import "server-only";

import { createHash, createHmac, timingSafeEqual } from "node:crypto";

import { cookies } from "next/headers";

import { signIn, type Tokens } from "./cognito";
import { callTool } from "./mcp";
import { ToolCallError } from "./mcp-result";
import { expiresSoon } from "./tokens";
import type { ToolName } from "./tools";

// The desk speaker is a device, not a person. It signs in with its own account (the
// "speaker" role, which only hears alert briefs), held here on the server and never in a
// browser cookie, so no one's sign-in or sign-out touches it.

const PAIRED = "crewops_speaker_paired";
let session: Tokens | null = null;

function account(): { username: string; password: string } {
  const username = process.env.CREWOPS_SPEAKER_USERNAME ?? "ops.speaker";
  const password = process.env.CREWOPS_SPEAKER_PASSWORD;
  if (!password) {
    throw new DeviceError(
      503,
      "The speaker is not set up: run `uv run python -m crewops.web_env --speaker` and restart.",
    );
  }
  return { username, password };
}

export class DeviceError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function token(fresh: boolean): Promise<string> {
  if (fresh || !session || expiresSoon(session.access, Date.now() / 1000, 120)) {
    const { username, password } = account();
    session = await signIn(username, password);
  }
  return session.access;
}

/** A tool call as the speaker device: signs in again once if its session has ended. */
export async function deviceCall(name: ToolName, args: Record<string, unknown>): Promise<unknown> {
  try {
    return await callTool(await token(false), name, args);
  } catch (err) {
    if (err instanceof ToolCallError && err.code === "UNAUTHORIZED") {
      return callTool(await token(true), name, args);
    }
    throw err;
  }
}

function same(a: string, b: string): boolean {
  // Compared as fixed-length hashes, so neither length nor content leaks through timing.
  const x = createHash("sha256").update(a).digest();
  const y = createHash("sha256").update(b).digest();
  return timingSafeEqual(x, y);
}

/** The paired screen's cookie: an HMAC under a server-side secret (the speaker's own random
 * password), so it cannot be computed or guessed from the pairing code alone. */
function pairingToken(): string | null {
  const code = process.env.CREWOPS_SPEAKER_PAIRING_CODE;
  const secret = process.env.CREWOPS_SPEAKER_PASSWORD;
  if (!code || !secret) return null;
  return createHmac("sha256", secret).update(`crewops-speaker-paired:${code}`).digest("hex");
}

/** Run as "npm run dev" on your own machine, a speaker needs nothing. A hosted app (a
 * production build) hears alerts only on screens paired with the code, like a new Echo.
 * Request headers are never trusted for this: anyone can send "Host: localhost". */
export async function paired(): Promise<boolean> {
  if (process.env.NODE_ENV !== "production") return true;
  const token = pairingToken();
  const cookie = (await cookies()).get(PAIRED)?.value;
  return Boolean(token && cookie && same(token, cookie));
}

/** Pairs this screen if the code is right: a device cookie, kept for 30 days and separate
 * from any person's sign-in. */
export async function pair(code: string): Promise<boolean> {
  const expected = process.env.CREWOPS_SPEAKER_PAIRING_CODE;
  const token = pairingToken();
  if (!expected || !token || !code || !same(code, expected)) return false;
  (await cookies()).set(PAIRED, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: 30 * 24 * 3600,
  });
  return true;
}
