import { settings } from "@/lib/config";
import { realtimeUrl } from "@/lib/live";
import { currentTokens } from "@/lib/session";

/** What the page needs to open the live-events socket: the ID token (AppSync's Cognito mode
 * takes ID tokens) and the endpoints. Nothing is cached. */
export async function GET() {
  const tokens = await currentTokens();
  if (!tokens) {
    return Response.json({ error: "Sign in first." }, { status: 401 });
  }
  return Response.json(
    {
      url: realtimeUrl(settings.eventsRealtimeDomain()),
      host: settings.eventsHttpDomain(),
      token: tokens.id,
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
