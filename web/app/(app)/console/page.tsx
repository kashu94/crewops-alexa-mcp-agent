"use client";

import {
  Activity,
  AlertOctagon,
  ArrowUpRight,
  BadgeCheck,
  ClipboardCheck,
  Cpu,
  Inbox,
  Plane,
  Radio,
  Send,
  Wrench,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AlexaModule } from "@/components/alexa";
import { useLiveListener } from "@/components/live";
import { useMe } from "@/components/me";
import { SeatOptions } from "@/components/solutions";
import { Button, clock, cx, day, Empty, Mono, Notice, Panel, Spinner, Status } from "@/components/ui";
import { tool } from "@/lib/client";
import { describe, type Detail, type FeedItem, type QueueItem } from "@/lib/types";

const TYPE_ICON: Record<string, typeof Radio> = {
  crew_unavailable: AlertOctagon,
  certification_lapse: BadgeCheck,
  flight_delay: Activity,
  aircraft_technical: Wrench,
  station_disruption: Radio,
  gate_change: Plane,
};

function Kpi({
  label,
  value,
  tone,
  icon: Icon,
}: {
  label: string;
  value: number | string;
  tone: "critical" | "pending" | "accent" | "ok";
  icon: typeof Radio;
}) {
  const tones = {
    critical: "from-red-50 text-critical ring-red-100",
    pending: "from-amber-50 text-pending ring-amber-100",
    accent: "from-sky-50 text-accent ring-sky-100",
    ok: "from-emerald-50 text-ok ring-emerald-100",
  };
  return (
    <div className={cx("lift animate-rise flex items-center gap-3 rounded-2xl bg-gradient-to-br to-white p-4 ring-1", tones[tone])}>
      <span className="grid h-10 w-10 place-items-center rounded-xl bg-white shadow-sm">
        <Icon size={18} />
      </span>
      <span>
        <span className="block font-mono text-2xl font-semibold tabular-nums text-slate-900">{value}</span>
        <span className="block text-xs font-medium text-muted">{label}</span>
      </span>
    </div>
  );
}

function Feed({
  items,
  selected,
  onDesk,
  onSelect,
}: {
  items: FeedItem[] | null;
  selected: string | null;
  onDesk: Set<string>;
  onSelect: (id: string) => void;
}) {
  if (!items) return <div className="p-4 text-muted"><Spinner /></div>;
  if (items.length === 0) return <Empty icon={<Inbox size={22} />}>No disruptions reported.</Empty>;
  return (
    <ol className="relative flex flex-col">
      <span aria-hidden className="absolute bottom-3 left-[18px] top-3 w-px bg-line" />
      {items.map((d, i) => {
        const Icon = TYPE_ICON[d.report_type ?? ""] ?? Radio;
        const critical = d.status === "REPORTED" || d.status === "OPTIONS_READY";
        return (
          <li key={d.disruption_id} className="animate-rise" style={{ animationDelay: `${Math.min(i, 10) * 30}ms` }}>
            <button
              onClick={() => onSelect(d.disruption_id)}
              className={cx(
                "relative flex w-full gap-3 rounded-xl px-2 py-2.5 text-left transition",
                selected === d.disruption_id ? "bg-accent-soft ring-1 ring-sky-200" : "hover:bg-sunken",
              )}
            >
              <span
                className={cx(
                  "relative z-10 grid h-[22px] w-[22px] shrink-0 place-items-center rounded-full ring-4 ring-white",
                  critical ? "bg-critical text-white" : d.status === "SUBMITTED" ? "bg-pending text-white" : "bg-slate-300 text-white",
                )}
              >
                <Icon size={12} strokeWidth={2.5} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center justify-between gap-2">
                  <Mono className="text-[11px] text-muted">
                    {day(d.reported_utc)} {clock(d.reported_utc)}
                  </Mono>
                  <Status value={d.status} />
                </span>
                <span className="mt-1 block truncate text-sm font-medium text-slate-800">{describe(d)}</span>
                <span className="mt-0.5 flex items-center gap-2 text-[11px] text-muted">
                  <Mono>{d.disruption_id}</Mono>
                  {onDesk.has(d.disruption_id) && (
                    <span className="rounded bg-pending-soft px-1.5 font-semibold text-amber-800">on your desk</span>
                  )}
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function Solutions({ id, onDesk, onChanged }: { id: string; onDesk: boolean; onChanged: () => void }) {
  const me = useMe();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  const load = useCallback(() => {
    tool<Detail>("show_disruption", { disruption_id: id }).then(
      (d) => {
        setDetail(d);
        setError(null);
      },
      (err: Error) => setError(err.message),
    );
  }, [id]);

  useEffect(load, [load]); // keyed by id in the parent, so each disruption starts fresh
  useLiveListener(useCallback((e) => e.disruption_id === id && load(), [id, load]));

  async function submit(acceptUncovered: boolean) {
    setBusy(true);
    setMessage(null);
    try {
      const out = await tool<{ status: string; contention?: string[] }>("submit_plan", {
        disruption_id: id,
        note,
        accept_uncovered: acceptUncovered,
      });
      setMessage({
        tone: "ok",
        text: `Submitted for approval${out.contention?.length ? `; note: ${out.contention.join(" · ")}` : ""}.`,
      });
      setNote("");
      load();
      onChanged();
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Failed." });
    } finally {
      setBusy(false);
    }
  }

  if (error) return <Notice tone="error">{error}</Notice>;
  if (!detail) return <div className="text-muted"><Spinner /></div>;
  // Only a disruption on this controller's desk can be planned here; the server checks too.
  const canSubmit = me.roles.includes("controller") && detail.status === "OPTIONS_READY" && onDesk;
  const uncoverable = detail.seats.some((s) => s.status !== "options");

  return (
    <div key={id} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="text-base font-semibold">{detail.disruption_id}</Mono>
        <Status value={detail.status} />
        <Link
          href={`/console/${encodeURIComponent(id)}`}
          className="ml-auto flex items-center gap-1 text-xs text-accent hover:underline"
        >
          Full record <ArrowUpRight size={12} />
        </Link>
      </div>
      {detail.summary && (
        <p className="rounded-xl bg-sunken px-3 py-2 text-xs leading-relaxed text-slate-700">{detail.summary}</p>
      )}
      {detail.seats.length === 0 ? (
        <Empty icon={<Cpu size={22} />}>No seats to cover for this disruption.</Empty>
      ) : (
        detail.seats.map((s) => <SeatOptions key={`${s.pairing_id}-${s.role}-${s.crew_out}`} seat={s} />)
      )}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {canSubmit && (
        <div className="flex flex-col gap-2 rounded-xl border border-sky-200 bg-accent-soft/50 p-3">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={500}
            placeholder="Note for the approver (optional)"
            className="rounded-lg border border-line bg-white px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <div className="flex flex-wrap gap-2">
            <Button tone="accent" disabled={busy} onClick={() => submit(false)}>
              <Send size={14} /> Submit the recommended plan
            </Button>
            {uncoverable && (
              <Button tone="secondary" disabled={busy} onClick={() => submit(true)}>
                Submit, accepting uncovered seats
              </Button>
            )}
          </div>
          <p className="text-[11px] text-muted">A different approver must sign it off; approval re-checks every rule.</p>
        </div>
      )}
    </div>
  );
}

export default function ControllerWorkspace() {
  const [feed, setFeed] = useState<FeedItem[] | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    tool<FeedItem[]>("disruptions", { limit: 40 }).then(
      (items) => {
        setFeed(items);
        setError(null);
        setSelected((s) => s ?? items.find((d) => d.status === "OPTIONS_READY")?.disruption_id ?? items[0]?.disruption_id ?? null);
      },
      (err: Error) => setError(err.message),
    );
    tool<QueueItem[]>("my_queue").then(setQueue, () => undefined);
  }, []);

  useEffect(load, [load]);
  useLiveListener(load);

  const onDesk = useMemo(() => new Set(queue.map((q) => q.disruption_id)), [queue]);
  const open = feed?.filter((d) => ["REPORTED", "OPTIONS_READY", "SUBMITTED"].includes(d.status)) ?? [];
  const awaiting = feed?.filter((d) => d.status === "SUBMITTED").length ?? 0;
  const resolved = feed?.filter((d) => ["COMMITTED", "CLOSED"].includes(d.status)).length ?? 0;
  const current = feed?.find((d) => d.disruption_id === selected);

  const suggestions = useMemo(() => {
    const out: string[] = [];
    const crew = typeof current?.details?.crew_id === "string" ? current.details.crew_id : null;
    if (current?.report_type === "crew_unavailable" && crew) {
      out.push(`${crew} is sick. Who covers?`);
      out.push(`How many duty hours does ${crew} have?`);
    }
    if (current?.report_type === "flight_delay" && typeof current.details.flight_id === "string") {
      out.push(`What does the delay to ${current.details.flight_id.split("-")[0]} do to the crew?`);
    }
    out.push("Who is on reserve at BLR on 15 Sep?");
    out.push("Which single flight leg has the most seats at risk if cancelled?");
    return out.slice(0, 4);
  }, [current]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Crew control</h1>
          <p className="text-sm text-muted">Disruptions as they arrive, the assistant, and legal cover ranked by cost.</p>
        </div>
        {queue.length > 0 && (
          <span className="flex items-center gap-1.5 rounded-full bg-pending-soft px-3 py-1 text-xs font-semibold text-amber-800">
            <ClipboardCheck size={13} /> {queue.length} waiting for you
          </span>
        )}
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi label="Open disruptions" value={feed ? open.length : "–"} tone="critical" icon={AlertOctagon} />
        <Kpi label="Waiting for you" value={queue.length} tone="accent" icon={Inbox} />
        <Kpi label="Awaiting approval" value={feed ? awaiting : "–"} tone="pending" icon={ClipboardCheck} />
        <Kpi label="Resolved" value={feed ? resolved : "–"} tone="ok" icon={BadgeCheck} />
      </div>
      <div className="grid gap-4 xl:grid-cols-[minmax(280px,1fr)_minmax(380px,1.25fr)_minmax(360px,1.25fr)]">
        <Panel title="Ingestion feed" icon={<Radio size={14} className="text-critical" />} className="xl:max-h-[calc(100vh-170px)]" bodyClassName="overflow-y-auto p-2">
          <Feed items={feed} selected={selected} onDesk={onDesk} onSelect={setSelected} />
        </Panel>
        <Panel title="Alexa+ voice" icon={<Activity size={14} className="text-accent" />} className="xl:max-h-[calc(100vh-170px)]" bodyClassName="flex flex-col overflow-hidden">
          <AlexaModule suggestions={suggestions} className="flex-1" />
        </Panel>
        <Panel title="Solution engine" icon={<Cpu size={14} className="text-accent" />} className="xl:max-h-[calc(100vh-170px)]" bodyClassName="overflow-y-auto">
          {selected ? (
            <Solutions key={selected} id={selected} onDesk={onDesk.has(selected)} onChanged={load} />
          ) : (
            <Empty icon={<Cpu size={22} />}>Select a disruption to see legal cover, ranked by cost.</Empty>
          )}
        </Panel>
      </div>
    </div>
  );
}
