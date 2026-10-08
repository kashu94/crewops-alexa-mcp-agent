"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ArrowLeft, Cpu, History, ListChecks } from "lucide-react";

import { useLiveListener } from "@/components/live";
import { useMe } from "@/components/me";
import { SeatOptions } from "@/components/solutions";
import { Button, Card, Field, Mono, Notice, Panel, rupees, Spinner, Status } from "@/components/ui";
import { tool } from "@/lib/client";
import type { Detail } from "@/lib/types";

type Outcome = { status: string; reasons?: string[]; contention?: string[] };

export default function DisruptionPage() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(() => {
    tool<Detail>("show_disruption", { disruption_id: id }).then(
      (d) => {
        setDetail(d);
        setError(null);
      },
      (err: Error) => setError(err.message),
    );
  }, [id]);

  useEffect(load, [load]);
  useLiveListener(useCallback((e) => e.disruption_id === id && load(), [id, load]));

  async function act(name: string, args: Record<string, unknown>, done: string) {
    setBusy(true);
    setMessage(null);
    try {
      const out = await tool<Outcome>(name, { disruption_id: id, ...args });
      const extra = [...(out.reasons ?? []), ...(out.contention ?? [])].join(" · ");
      setMessage({ tone: "ok", text: `${done}: ${out.status}${extra ? ` (${extra})` : ""}` });
      load();
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Failed." });
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <div className="flex flex-col gap-4">
        <Link href="/console" className="flex items-center gap-1 text-sm text-muted hover:text-foreground"><ArrowLeft size={14} /> Crew control</Link>
        <Notice tone="error">{error}</Notice>
      </div>
    );
  }
  if (!detail) return <Spinner />;

  const pending = detail.plans.find((p) => p.status === "SUBMITTED");
  const isController = me.roles.includes("controller");
  const isApprover = me.roles.includes("approver");

  return (
    <div className="flex flex-col gap-6">
      <Link href="/console" className="flex items-center gap-1 text-sm text-muted hover:text-foreground"><ArrowLeft size={14} /> Crew control</Link>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-mono text-2xl font-semibold tracking-tight">{detail.disruption_id}</h1>
        <Status value={detail.status} />
        {detail.report_type && <span className="text-sm text-muted">{detail.report_type.replace(/_/g, " ")}</span>}
      </div>

      <Panel title="Analysis" icon={<Cpu size={14} className="text-accent" />}>
        <p className="whitespace-pre-line text-sm leading-relaxed">{detail.summary || "Not analysed yet."}</p>
        {detail.seats.length > 0 && (
          <div className="mt-4 grid gap-5 lg:grid-cols-2">
            {detail.seats.map((s) => (
              <SeatOptions key={`${s.pairing_id}-${s.role}-${s.crew_out}`} seat={s} />
            ))}
          </div>
        )}
      </Panel>

      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      {isController && detail.status === "OPTIONS_READY" && (
        <Card>
          <h2 className="mb-3 font-semibold">Submit the recommended plan</h2>
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              act(
                "submit_plan",
                { note: String(f.get("note") ?? ""), accept_uncovered: f.get("uncovered") === "on" },
                "Submitted",
              );
            }}
          >
            <Field label="Note for the approver" name="note" maxLength={500} />
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" name="uncovered" /> Accept seats left without legal cover
            </label>
            <div className="flex flex-wrap gap-2">
              <Button disabled={busy}>Submit for approval</Button>
            </div>
          </form>
          <form
            className="mt-5 flex flex-col gap-3 border-t border-line pt-4"
            onSubmit={(e) => {
              e.preventDefault();
              const reason = String(new FormData(e.currentTarget).get("reason") ?? "");
              act("close_disruption", { reason }, "Closed");
            }}
          >
            <Field
              label="Close without a plan"
              name="reason"
              required
              maxLength={300}
              hint="Only when no seat can be covered, for example the flights were cancelled."
            />
            <div>
              <Button tone="secondary" disabled={busy}>Close disruption</Button>
            </div>
          </form>
        </Card>
      )}

      {pending && (isApprover || isController) && (
        <Card>
          <h2 className="mb-1 font-semibold">Plan {pending.plan_id} is waiting</h2>
          <p className="mb-3 text-sm text-muted">
            Submitted by {pending.submitted_by} for {rupees(pending.total_cost_inr)}.
            {isApprover && " Approving re-checks legality and changes the roster in one step."}
          </p>
          {/* No form: Enter in the reason box must never trigger an action, and approving
              changes the roster, so it takes a second, explicit click. */}
          <div className="flex flex-col gap-3">
            <Field
              label="Reason (for reject or withdraw)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && e.preventDefault()}
              maxLength={300}
            />
            <div className="flex flex-wrap gap-2">
              {isApprover &&
                (confirming ? (
                  <>
                    <Button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setConfirming(false);
                        act("approve_plan", { plan_id: pending.plan_id }, "Approved");
                      }}
                    >
                      Confirm: approve and commit
                    </Button>
                    <Button type="button" tone="secondary" onClick={() => setConfirming(false)}>
                      Cancel
                    </Button>
                  </>
                ) : (
                  <Button type="button" disabled={busy} onClick={() => setConfirming(true)}>
                    Approve and commit…
                  </Button>
                ))}
              {isApprover && (
                <Button
                  type="button"
                  tone="danger"
                  disabled={busy || !reason.trim()}
                  onClick={() => act("reject_plan", { plan_id: pending.plan_id, reason }, "Rejected")}
                >
                  Reject
                </Button>
              )}
              {isController && pending.submitted_by === me.user_id && (
                <Button
                  type="button"
                  tone="secondary"
                  disabled={busy}
                  onClick={() => act("withdraw_plan", { plan_id: pending.plan_id, reason }, "Withdrawn")}
                >
                  Withdraw
                </Button>
              )}
            </div>
          </div>
        </Card>
      )}

      <Panel title="Plans" icon={<ListChecks size={14} className="text-accent" />}>
        {detail.plans.length === 0 ? (
          <p className="text-sm text-muted">No plan yet.</p>
        ) : (
          <ul className="flex flex-col gap-4">
            {detail.plans.map((p) => (
              <li key={p.plan_id} className="rounded-lg border border-line p-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Mono>{p.plan_id}</Mono>
                  <Status value={p.status} />
                  <span className="text-muted">
                    {p.desk} · {p.submitted_by} · {rupees(p.total_cost_inr)}
                  </span>
                </div>
                <ul className="mt-2 list-disc pl-5 text-sm">
                  {p.assignments.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                  {p.uncovered.map((u) => (
                    <li key={u} className="text-orange-700">Uncovered: {u}</li>
                  ))}
                </ul>
                {p.reason && <p className="mt-1 text-xs text-muted">Reason: {p.reason}</p>}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Card>
        <h2 className="mb-3 flex items-center gap-2 text-[13px] font-semibold uppercase tracking-[0.08em] text-slate-600"><History size={14} className="text-accent" /> Audit trail</h2>
        <ol className="flex flex-col gap-2 text-sm">
          {detail.audit.map((a, i) => (
            <li key={i} className="flex flex-wrap gap-x-3">
              <span className="font-mono text-xs text-muted">{a.at}</span>
              <span className="font-medium">{a.action.replace(/_/g, " ")}</span>
              <span className="text-muted">by {a.actor}</span>
              {a.plan_id && <span className="font-mono text-xs">{a.plan_id}</span>}
              {a.reason && <span className="text-muted">— {a.reason}</span>}
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}
