"use client";

import { ArrowLeft, ClipboardList, Cpu, History, ListChecks, Plane } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  Actions,
  AuditTrail,
  chosenFor,
  CommittedPlan,
  type Choice,
  Coverage,
  DeskBanner,
  JointPlan,
  PlanList,
  TripFacts,
} from "@/components/disruption";
import { AirlineBadge } from "@/components/airline";
import { HandlingBanner, HandlingDetail, NoticeList } from "@/components/handling";
import { useLiveListener } from "@/components/live";
import { useMe } from "@/components/me";
import { SeatOptions } from "@/components/solutions";
import { CANCEL, SeatWorkspace, SHORT, seatKey } from "@/components/workspace";
import { clock, day, Empty, Mono, Notice, Panel, Spinner, Status } from "@/components/ui";
import { tool } from "@/lib/client";
import { describe, shownStatus, type Detail } from "@/lib/types";

export default function DisruptionPage() {
  const { id } = useParams<{ id: string }>();
  const me = useMe();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Record<string, string>>({});

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

  // The recommendation per seat is the analysis's joint plan (one person never covers two
  // seats), matched by trip and role in order as the server does; else the live top option.
  const recommended = useMemo(() => {
    const joint: Record<string, { crew_id: string; name?: string }[]> = {};
    for (const a of (detail?.joint_plan?.assignments ?? []) as {
      pairing_id: string;
      role: string;
      crew_id: string;
      name?: string;
    }[]) {
      (joint[`${a.pairing_id}/${a.role}`] ??= []).push(a);
    }
    const out: Record<string, { crewId: string; name: string } | null> = {};
    for (const s of detail?.seats ?? []) {
      if (s.status !== "options") continue;
      const j = joint[`${s.pairing_id}/${s.role}`]?.shift();
      const top = s.live?.options[0];
      const name = (id: string) => s.live?.options.find((o) => o.crew_id === id)?.name ?? j?.name ?? id;
      out[seatKey(s)] = j
        ? { crewId: j.crew_id, name: name(j.crew_id) }
        : top?.crew_id
          ? { crewId: top.crew_id, name: top.name }
          : null;
    }
    return out;
  }, [detail]);

  // What Submit shows and sends. Only seats the controller changed are sent: the server
  // falls back to the joint plan for the rest. A pick no longer in the live list is stale.
  const choices: Choice[] = useMemo(
    () =>
      (detail?.seats ?? []).flatMap((s): Choice[] => {
        const key = seatKey(s);
        const rec = recommended[key];
        const mine = chosen[key];
        if (!rec) {
          // No legal crew: the seat can only be cancelled or flown short, and only on purpose.
          if (s.status !== "no_legal_cover" || (mine !== CANCEL && mine !== SHORT)) return [];
          const label = mine === CANCEL ? "Cancel the flights" : "Operate one cabin crew short";
          return [
            { seat: key, crewId: mine, name: label, overrides: true, stale: mine === SHORT && !s.live?.short_crew },
          ];
        }
        const gone = detail?.unavailable_now ?? {};
        if (!mine || mine === rec.crewId)
          return [
            { seat: key, crewId: rec.crewId, name: rec.name, overrides: false, stale: Boolean(gone[rec.crewId]) },
          ];
        if (mine === CANCEL || mine === SHORT) {
          const label = mine === CANCEL ? "Cancel the flights" : "Operate one cabin crew short";
          return [
            { seat: key, crewId: mine, name: label, overrides: true, stale: mine === SHORT && !s.live?.short_crew },
          ];
        }
        const pick = s.live?.options.find((o) => o.crew_id === mine);
        return [
          { seat: key, crewId: mine, name: pick?.name ?? mine, overrides: true, stale: !pick || Boolean(gone[mine]) },
        ];
      }),
    [detail, chosen, recommended],
  );
  useLiveListener(useCallback((e) => e.disruption_id === id && load(), [id, load]));

  // Back to where this role works: approvers come here from their queue.
  const approverOnly =
    me.roles.includes("approver") && !me.roles.includes("controller") && !me.roles.includes("auditor");
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
          <Status value={shownStatus(detail.status, detail.handling?.kind)} />
          {(detail.airlines ?? []).map((a) => (
            <AirlineBadge key={a.code} code={a.code} />
          ))}
        </div>
        <p className="text-sm text-muted">
          <Mono>{detail.disruption_id}</Mono> · {(detail.report_type ?? "").replace(/_/g, " ")} · reported{" "}
          <Mono>
            {day(detail.reported_utc)} {clock(detail.reported_utc)}
          </Mono>
          {detail.reported_by && <> by {detail.reported_by}</>}
        </p>
        <DeskBanner detail={detail} />
        <HandlingBanner handling={detail.handling} />
      </header>

      <Coverage detail={detail} />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <div className="flex min-w-0 flex-col gap-4">
          {detail.seats.length === 0 && (
            <Panel title="Analysis" icon={<Cpu size={14} className="text-accent" />}>
              {detail.summary ? (
                <div className="flex flex-col gap-2 text-sm leading-relaxed text-slate-800">
                  <p>{detail.summary}</p>
                  {(detail.notes ?? []).length > 0 && (
                    <ul className="list-disc pl-5 text-xs text-muted">
                      {(detail.notes ?? []).map((n) => (
                        <li key={n}>{n}</li>
                      ))}
                    </ul>
                  )}
                </div>
              ) : (
                <Empty icon={<Cpu size={22} />}>Not analysed yet.</Empty>
              )}
            </Panel>
          )}
          <CommittedPlan detail={detail} />
          <HandlingDetail detail={detail} />
          {detail.seats.length > 0 &&
            detail.seats.map((s) => (
              <Panel
                key={seatKey(s)}
                title={`Cover for ${s.pairing_id} · ${s.role}${s.crew_out ? ` · for ${s.crew_out}` : ""}`}
                icon={<Cpu size={14} className="text-accent" />}
              >
                {detail.status === "COMMITTED" ? (
                  <SeatOptions seat={s} chosen={chosenFor(detail, s)} />
                ) : (
                  <SeatWorkspace
                    seat={s}
                    contention={detail.contention ?? {}}
                    chosen={chosen[seatKey(s)] ?? null}
                    recommendedId={recommended[seatKey(s)]?.crewId ?? null}
                    unavailable={detail.unavailable_now ?? {}}
                    suggestions={(detail.suggestions ?? []).filter((sg) => sg.seat === seatKey(s))}
                    onSuggest={
                      !detail.yours && me.roles.includes("controller") && detail.status === "OPTIONS_READY"
                        ? async (crewId, note) => {
                            await tool("suggest_cover", {
                              disruption_id: detail.disruption_id,
                              seat: seatKey(s),
                              crew_id: crewId,
                              note,
                            });
                          }
                        : undefined
                    }
                    onChoose={
                      detail.yours &&
                      detail.status === "OPTIONS_READY" &&
                      (s.status === "options" || s.status === "no_legal_cover")
                        ? (crewId) => {
                            // A seat the analysis found no legal crew for may only be
                            // cancelled or flown short; the server refuses anything else.
                            if (s.status === "options" || crewId === CANCEL || crewId === SHORT)
                              setChosen((c) => ({ ...c, [seatKey(s)]: crewId }));
                          }
                        : undefined
                    }
                  />
                )}
              </Panel>
            ))}
          <JointPlan detail={detail} />
          <NoticeList notices={detail.notices ?? []} />
        </div>

        <aside className="flex flex-col gap-4">
          <Panel title="Actions" icon={<ClipboardList size={14} className="text-accent" />}>
            <Actions
              detail={detail}
              choices={choices}
              onChanged={() => {
                setChosen({});
                load();
              }}
            />
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
          {detail.plans.length > 0 && (
            <Panel title="Plans" icon={<ListChecks size={14} className="text-accent" />}>
              <PlanList plans={detail.plans} />
            </Panel>
          )}
          <Panel title="Audit trail" icon={<History size={14} className="text-accent" />}>
            <AuditTrail audit={detail.audit} />
          </Panel>
        </aside>
      </div>
    </div>
  );
}
