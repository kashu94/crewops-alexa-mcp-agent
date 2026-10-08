// The shapes the MCP server returns, as the web app reads them.

export type FeedItem = {
  disruption_id: string;
  status: string;
  report_type: string | null;
  reported_utc: string | null;
  reported_by: string | null;
  details: Record<string, unknown>;
  summary: string;
};

export type Option = {
  rank: number;
  action: string;
  strategy: string;
  crew_id: string | null;
  name: string;
  crew_rank: string | null;
  cost_inr: number;
  cost_breakdown: Record<string, number>;
  delay_hours: number;
};

export type Seat = {
  pairing_id: string;
  role: string;
  crew_out: string | null;
  status: string;
  note: string;
  legal_options: number | null;
  excluded: number | null;
  cancellation_cost_inr: number | null;
  cancellation_multiple: number | null;
  delay_hours: number;
  impact: { affected_dates?: string[]; uncovered_flights?: string[]; at_risk_flights?: string[]; passengers?: number };
  options: Option[];
};

export type Plan = {
  status: string;
  plan_id: string;
  desk: string;
  submitted_by: string;
  total_cost_inr: number;
  assignments: string[];
  uncovered: string[];
  reason: string | null;
};

export type Detail = {
  disruption_id: string;
  status: string;
  summary: string;
  report_type: string | null;
  reported_utc: string | null;
  seats: Seat[];
  plans: Plan[];
  audit: { at: string; action: string; actor: string; plan_id?: string; reason?: string }[];
};

export type Verdict = {
  rule_id: string;
  status: string;
  detail: string;
  used: number | null;
  limit: number | null;
  headroom: number | null;
  date: string | null;
};

export type Legality = { legal: boolean; blocking: string[]; verdicts: Verdict[]; name: string };

export type QueueItem = {
  kind: string;
  disruption_id: string;
  plan_id?: string;
  summary?: string;
  desk?: string;
  submitted_by?: string;
  total_cost_inr?: number;
  assignments?: string[];
  uncovered?: string[];
  warnings?: string[];
  reason?: string | null;
};

const s = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

/** One line a controller reads at a glance, built from the report itself. */
export function describe(item: Pick<FeedItem, "report_type" | "details">): string {
  const d = item.details ?? {};
  switch (item.report_type) {
    case "crew_unavailable": {
      const to = s(d.to_date) && s(d.to_date) !== s(d.from_date) ? ` to ${s(d.to_date)}` : "";
      return `${s(d.crew_id)} unavailable ${s(d.from_date)}${to} (${s(d.reason)})`;
    }
    case "certification_lapse":
      return `${s(d.crew_id)} ${s(d.cert_type).replace(/_/g, " ")} lapsed`;
    case "flight_delay":
      return `${s(d.flight_id)} delayed ${s(d.delay_minutes)} min`;
    case "aircraft_technical":
      return `${s(d.aircraft)} technical fault`;
    case "station_disruption":
      return `${s(d.station)} closed ${s(d.from_utc).slice(11, 16)}–${s(d.to_utc).slice(11, 16)}Z`;
    case "gate_change":
      return `${s(d.flight_id)} moved to gate ${s(d.new_gate)}`;
    default:
      return (item.report_type ?? "disruption").replace(/_/g, " ");
  }
}

/** One verdict per rule: the tightest margin across the pairing's days. */
export function byRule(verdicts: Verdict[]): Verdict[] {
  const out = new Map<string, Verdict>();
  for (const v of verdicts) {
    const seen = out.get(v.rule_id);
    const worse =
      !seen ||
      (v.status !== "PASS" && seen.status === "PASS") ||
      (v.headroom !== null && (seen.headroom === null || v.headroom < seen.headroom));
    if (worse) out.set(v.rule_id, v);
  }
  return [...out.values()].sort((a, b) => a.rule_id.localeCompare(b.rule_id));
}
