import { settings } from "@/lib/config";

/** The app's public push key, which a browser needs to subscribe to desktop alerts. Public
 * by design (the private half never leaves Parameter Store); empty until it is set up. */
export async function GET() {
  return Response.json({ key: settings.pushPublicKey() }, { headers: { "Cache-Control": "no-store" } });
}
