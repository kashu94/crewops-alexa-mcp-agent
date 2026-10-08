"use client";

import { ArrowLeft, ClipboardList, Cpu, History, ListChecks, Plane } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import {
  Actions,
  AuditTrail,
  Coverage,
  DeskBanner,
  JointPlan,
  PlanList,
  Recommendation,
  TripFacts,
} from "@/components/disruption";
import { useLiveListener } from "@/components/live";
import { useMe } from "@/components/me";
import { SeatOptions } from "@/components/solutions";
import { clock, day, Empty, Mono, Notice, Panel, Spinner, Status } from "@/components/ui";
import { tool } from "@/lib/client";
import { describe, type Detail } from "@/lib/types";

export default function DisruptionPage() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

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

  // Back to where this role works: approvers come here from their queue.
  const approverOnly = me.roles.includes("approver") && !me.roles.includes("controller") && !me.roles.includes("auditor");
  const back = (
    <Link
      href={approverOnly ? "/approvals" : "/console"}
      className="flex w-fit items-center gap-1 text-sm text-muted hover:text-foreground"
    >
      <ArrowLeft size={14} /> {approverOnly ? "Approval queue" : "Crew control"}
    </Link>
  );
  if (error) {
    return (
      <div className="flex flex-col gap-4">
        {back}
        <Notice tone="error">{error}</Notice>
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="flex flex-col gap-4 text-muted">
        {back}
        <Spinner />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {back}
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {describe({ report_type: detail.report_type, details: detail.details ?? {} })}
          </h1>
          <Status value={detail.status} />
        </div>
        <p className="text-sm text-muted">
          <Mono>{detail.disruption_id}</Mono> · {(detail.report_type ?? "").replace(/_/g, " ")} · reported{" "}
          <Mono>
            {day(detail.reported_utc)} {clock(detail.reported_utc)}
          </Mono>
          {detail.reported_by && <> by {detail.reported_by}</>}
        </p>
        <DeskBanner detail={detail} />
      </header>

      <Coverage detail={detail} />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex min-w-0 flex-col gap-4">
          {detail.seats.length === 0 ? (
            <Panel title="Analysis" icon={<Cpu size={14} className="text-accent" />}>
              <Empty icon={<Cpu size={22} />}>{detail.summary || "Not analysed yet."}</Empty>
            </Panel>
          ) : (
            detail.seats.map((s) => (
              <Panel
                key={`${s.pairing_id}-${s.role}-${s.crew_out}`}
                title={`Cover for ${s.pairing_id} · ${s.role}`}
                icon={<Cpu size={14} className="text-accent" />}
              >
                <div className="flex flex-col gap-4">
                  <Recommendation seat={s} />
                  <SeatOptions seat={s} contention={detail.contention} />
                </div>
              </Panel>
            ))
          )}
          <JointPlan detail={detail} />
        </div>

        <aside className="flex flex-col gap-4">
          <Panel title="Actions" icon={<ClipboardList size={14} className="text-accent" />}>
            <Actions detail={detail} onChanged={load} />
          </Panel>
          {detail.seats[0]?.context && (
            <Panel title="What needs covering" icon={<Plane size={14} className="text-accent" />}>
              <div className="flex flex-col gap-5">
                {detail.seats.map((s) => (
                  <TripFacts key={`${s.pairing_id}-${s.role}-${s.crew_out}`} seat={s} />
                ))}
              </div>
            </Panel>
          )}
          <Panel title="Plans" icon={<ListChecks size={14} className="text-accent" />}>
            <PlanList plans={detail.plans} />
          </Panel>
          <Panel title="Audit trail" icon={<History size={14} className="text-accent" />}>
            <AuditTrail audit={detail.audit} />
          </Panel>
        </aside>
      </div>
    </div>
  );
}
