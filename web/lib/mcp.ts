import "server-only";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { invocationUrl, settings } from "./config";
import { valueOf, type ToolResult } from "./mcp-result";
import { TOOLS, type ToolName } from "./tools";

const TIMEOUT_MS = 60_000;

/** Call one MCP tool as the signed-in user: their access token goes to AgentCore, whose
 * authorizer and the server both verify it. */
export async function callTool(
  accessToken: string,
  name: ToolName,
  args: Record<string, unknown>,
): Promise<unknown> {
  const url = new URL(invocationUrl(settings.region(), settings.runtimeArn()));
  const transport = new StreamableHTTPClientTransport(url, {
    requestInit: { headers: { Authorization: `Bearer ${accessToken}` } },
  });
  const client = new Client({ name: "crewops-web", version: "0.1.0" });
  try {
    await client.connect(transport);
    const result = await client.callTool({ name, arguments: args }, undefined, {
      timeout: TIMEOUT_MS,
    });
    return valueOf(result as ToolResult, TOOLS[name].list);
  } finally {
    await client.close().catch(() => undefined);
  }
}
