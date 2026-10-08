import { PollyClient, SynthesizeSpeechCommand, type Engine, type VoiceId } from "@aws-sdk/client-polly";

import { settings } from "@/lib/config";
import { declaredTooLarge, RateLimit } from "@/lib/limits";
import { currentTokens } from "@/lib/session";
import { claimsOf } from "@/lib/tokens";
import { sameOrigin } from "@/lib/tools";

const MAX_CHARS = 1500;
const perUser = new RateLimit(30, 10 * 60 * 1000); // 30 answers spoken per 10 minutes
let polly: PollyClient | null = null;

/** Speaks an answer with Amazon Polly, for signed-in users only. Run locally, the server uses
 * the AWS profile in .env.local. */
export async function POST(request: Request) {
  if (!sameOrigin(request)) {
    return Response.json({ error: "Bad origin." }, { status: 403 });
  }
  if (declaredTooLarge(request, 8_000)) {
    return Response.json({ error: "Request too large." }, { status: 413 });
  }
  const tokens = await currentTokens();
  if (!tokens) {
    return Response.json({ error: "Sign in first." }, { status: 401 });
  }
  const user = claimsOf(tokens.id)?.["cognito:username"] ?? "unknown";
  if (!perUser.allow(user)) {
    return Response.json(
      { error: "Speech is paused for a few minutes; the answers stay on screen." },
      { status: 429 },
    );
  }
  const body = (await request.json().catch(() => null)) as { text?: unknown } | null;
  const text = typeof body?.text === "string" ? body.text.trim() : "";
  if (!text) {
    return Response.json({ error: "Nothing to say." }, { status: 400 });
  }
  polly ??= new PollyClient({ region: settings.region() });
  try {
    const out = await polly.send(
      new SynthesizeSpeechCommand({
        Text: text.slice(0, MAX_CHARS),
        OutputFormat: "mp3",
        VoiceId: settings.pollyVoice() as VoiceId,
        Engine: settings.pollyEngine() as Engine,
        LanguageCode: "en-IN",
      }),
    );
    const audio = await out.AudioStream?.transformToByteArray();
    if (!audio) throw new Error("no audio");
    return new Response(Buffer.from(audio), {
      headers: { "Content-Type": "audio/mpeg", "Cache-Control": "no-store" },
    });
  } catch (err) {
    console.error("speech failed", err);
    return Response.json({ error: "Speech is unavailable; the answer is on screen." }, { status: 502 });
  }
}
