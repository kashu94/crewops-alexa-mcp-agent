// The MCP tools the web app may call on a user's behalf. The server decides what each user
// may actually do (`crew_tool` runs the deterministic tools through the same role-checked
// dispatch as the assistant); this list only keeps the proxy from being a door to anything
// else.

export const TOOLS = {
  whoami: { list: false },
  disruptions: { list: true },
  my_roster: { list: false },
  rule_thresholds: { list: true },
  set_rule_threshold: { list: false },
  activity_log: { list: true },
  crew_tool: { list: false },
  ask: { list: false },
  my_queue: { list: true },
  show_disruption: { list: false },
  report_disruption: { list: false },
  submit_plan: { list: false },
  approve_plan: { list: false },
  reject_plan: { list: false },
  withdraw_plan: { list: false },
  close_disruption: { list: false },
  alert_addresses: { list: true },
  add_alert_address: { list: false },
  remove_alert_address: { list: false },
} as const;

export type ToolName = keyof typeof TOOLS;

export function isTool(name: string): name is ToolName {
  return Object.prototype.hasOwnProperty.call(TOOLS, name);
}

/** A state-changing request must come from this site: browsers send Origin on POST. */
export function sameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
