import { DeviceError, deviceCall, pairedDevice } from "@/lib/device";
import { ToolCallError } from "@/lib/mcp-result";

/** The ops-room wall display's data (the Fire TV app): fetched as the speaker device, so
 * nobody signs in on the TV. A hosted app answers only a paired device (see `pairedDevice`). */
export async function GET(request: Request) {
  if (!(await pairedDevice(request))) {
    return Response.json({ error: "This screen is not paired." }, { status: 401 });
  }
  try {
    return Response.json({ result: await deviceCall("ops_board", {}) }, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    if (err instanceof DeviceError) return Response.json({ error: err.message }, { status: err.status });
    if (err instanceof ToolCallError) return Response.json({ error: err.message, code: err.code }, { status: 422 });
    console.error("ops board failed", err);
    return Response.json({ error: "CrewOps is unreachable right now." }, { status: 502 });
  }
}
