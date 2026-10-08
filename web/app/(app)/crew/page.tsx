"use client";

import { CalendarDays, Clock3, HeartPulse, MoonStar, Plane, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { useMe } from "@/components/me";
import { Button, clock, cx, day, Empty, Field, Mono, Notice, Panel, Spinner } from "@/components/ui";
import { tool } from "@/lib/client";

type Day = { date: string; flights: string[]; report_utc: string; release_utc: string };
type Roster = {
  crew_id: string;
  name: string | null;
  clock: {
    as_of: string;
    duty_hours_7d: number;
    duty_limit_7d: number;
    flight_hours_28d: number;
    flight_limit_28d: number;
    last_rest_ended: string | null;
  };
  pairings: { pairing_id: string; aircraft: string; role: string; days: Day[] }[];
};

const REASONS = [
  ["sick", "Sick"],
  ["fatigue", "Fatigue"],
  ["personal", "Personal"],
  ["other", "Other"],
] as const;

function Gauge({ label, used, limit }: { label: string; used: number; limit: number }) {
  const pct = Math.min(100, Math.round((used / limit) * 100));
  const tone = pct >= 90 ? "bg-critical" : pct >= 75 ? "bg-pending" : "bg-accent";
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="font-medium text-slate-700">{label}</span>
        <Mono className="text-muted">
          {used}h / {limit}h
        </Mono>
      </div>
      <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-slate-100">
        <div className={cx("h-full rounded-full transition-[width] duration-700", tone)} style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 text-[11px] text-muted">{Math.max(0, +(limit - used).toFixed(2))}h left before the limit</p>
    </div>
  );
}

/** The Monday-to-Sunday week (UTC) containing `asOf`, the operation's current day. */
function weekOf(asOf: string): string[] {
  const first = new Date(`${asOf.slice(0, 10)}T00:00:00Z`);
  const monday = new Date(first);
  monday.setUTCDate(first.getUTCDate() - ((first.getUTCDay() + 6) % 7));
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(monday);
    d.setUTCDate(monday.getUTCDate() + i);
    return d.toISOString().slice(0, 10);
  });
}

function ReportSheet({ crewId, onClose }: { crewId: string; onClose: (text?: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function send(form: FormData) {
    setBusy(true);
    setError(null);
    const from = String(form.get("from") ?? "");
    try {
      const out = await tool<{ report_id: string; note: string }>("report_disruption", {
        report_type: "crew_unavailable",
        details: {
          crew_id: crewId,
          from_date: from,
          to_date: String(form.get("to") ?? "") || from,
          reason: form.get("reason"),
        },
        narrative: String(form.get("narrative") ?? ""),
      });
      onClose(`Reported (${out.report_id}). ${out.note}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed.");
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/30 backdrop-blur-sm sm:items-center" onClick={() => onClose()}>
      <div
        role="dialog"
        aria-modal
        aria-label="Report unavailable"
        className="animate-rise w-full max-w-md rounded-t-3xl bg-surface p-5 shadow-2xl sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <HeartPulse size={18} className="text-critical" /> Report unavailable
          </h2>
          <button className="rounded-lg p-1.5 text-muted hover:bg-sunken" onClick={() => onClose()} aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            send(new FormData(e.currentTarget));
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <Field label="From" name="from" type="date" required autoFocus />
            <Field label="Until" name="to" type="date" hint="Optional" />
          </div>
          <fieldset className="flex flex-col gap-1.5 text-sm">
            <legend className="mb-1 font-medium text-slate-700">Reason</legend>
            <div className="grid grid-cols-4 gap-2">
              {REASONS.map(([value, label], i) => (
                <label key={value} className="cursor-pointer">
                  <input type="radio" name="reason" value={value} defaultChecked={i === 0} className="peer sr-only" />
                  <span className="block rounded-lg border border-line px-2 py-2 text-center text-xs transition peer-checked:border-accent peer-checked:bg-accent-soft peer-checked:font-semibold">
                    {label}
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          <Field label="Anything crew control should know" name="narrative" maxLength={500} hint="Optional" />
          {error && <Notice tone="error">{error}</Notice>}
          <Button type="submit" tone="danger" disabled={busy}>
            {busy && <Spinner />} Tell crew control
          </Button>
          <p className="text-center text-[11px] text-muted">Cover is worked out automatically; a controller and an approver decide it.</p>
        </form>
      </div>
    </div>
  );
}

export default function Crew() {
  const me = useMe();
  const [roster, setRoster] = useState<Roster | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sheet, setSheet] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (me.crew_id) tool<Roster>("my_roster").then(setRoster, (err: Error) => setError(err.message));
  }, [me.crew_id]);

  const duties = useMemo(() => {
    const map = new Map<string, { pairing: string; aircraft: string; day: Day }>();
    roster?.pairings.forEach((p) => p.days.forEach((d) => map.set(d.date, { pairing: p.pairing_id, aircraft: p.aircraft, day: d })));
    return map;
  }, [roster]);

  if (!me.crew_id) return <Notice>This view is for crew members. Your account has no crew record.</Notice>;
  if (error) return <Notice tone="error">{error}</Notice>;
  if (!roster) return <div className="text-muted"><Spinner /></div>;

  const week = weekOf(roster.clock.as_of);

  return (
    <div className="mx-auto flex max-w-md flex-col gap-4">
      <div className="animate-rise rounded-3xl bg-gradient-to-br from-[#0c2340] to-sky-700 p-5 text-white shadow-lg">
        <p className="text-xs uppercase tracking-[0.14em] text-sky-200">Good to see you</p>
        <h1 className="mt-1 text-2xl font-semibold">{roster.name ?? me.user_id}</h1>
        <p className="mt-0.5 text-sm text-sky-100">
          <Mono>{roster.crew_id}</Mono> · {roster.pairings[0]?.role ?? "Crew"}
        </p>
        <button
          onClick={() => setSheet(true)}
          className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl bg-white/95 py-2.5 text-sm font-semibold text-red-700 shadow transition hover:bg-white"
        >
          <HeartPulse size={16} /> I can&apos;t fly — report unavailable
        </button>
      </div>

      {message && <Notice tone="ok">{message}</Notice>}

      <Panel title="This week" icon={<CalendarDays size={14} className="text-accent" />}>
        <ol className="grid grid-cols-7 gap-1.5 text-center">
          {week.map((date) => {
            const duty = duties.get(date);
            const wd = new Date(`${date}T00:00:00Z`).toLocaleDateString("en-IN", { weekday: "short", timeZone: "UTC" });
            return (
              <li
                key={date}
                className={cx(
                  "flex flex-col items-center gap-1 rounded-xl py-2 text-[11px]",
                  duty ? "bg-accent text-white shadow-sm" : "bg-sunken text-muted",
                )}
                title={duty ? `${duty.pairing} on ${duty.aircraft}` : "Off"}
              >
                <span className="uppercase opacity-80">{wd}</span>
                <span className="text-base font-semibold">{Number(date.slice(8, 10))}</span>
                {duty ? <Plane size={12} /> : <MoonStar size={12} />}
              </li>
            );
          })}
        </ol>
      </Panel>

      <Panel title="My flights" icon={<Plane size={14} className="text-accent" />}>
        {roster.pairings.length === 0 ? (
          <Empty icon={<MoonStar size={22} />}>No trips rostered.</Empty>
        ) : (
          <ol className="flex flex-col gap-3">
            {roster.pairings.flatMap((p) =>
              p.days.map((d) => (
                <li key={`${p.pairing_id}-${d.date}`} className="lift rounded-xl border border-line p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold">{day(d.date)}</span>
                    <span className="text-xs text-muted">
                      <Mono>{p.pairing_id}</Mono> · <Mono>{p.aircraft}</Mono>
                    </span>
                  </div>
                  <p className="mt-1 flex items-center gap-1.5 text-xs text-muted">
                    <Clock3 size={12} /> Report <Mono>{clock(d.report_utc)}</Mono> · Release <Mono>{clock(d.release_utc)}</Mono>
                  </p>
                  <p className="mt-2 flex flex-wrap gap-1.5">
                    {d.flights.map((f) => (
                      <Mono key={f} className="rounded-md bg-accent-soft px-1.5 py-0.5 text-[11px] text-sky-800">
                        {f.split("-")[0]}
                      </Mono>
                    ))}
                  </p>
                </li>
              )),
            )}
          </ol>
        )}
      </Panel>

      <Panel title="Duty limits" icon={<Clock3 size={14} className="text-accent" />}>
        <div className="flex flex-col gap-4">
          <Gauge label="Duty, last 7 days" used={roster.clock.duty_hours_7d} limit={roster.clock.duty_limit_7d} />
          <Gauge label="Flying, last 28 days" used={roster.clock.flight_hours_28d} limit={roster.clock.flight_limit_28d} />
          <p className="text-[11px] text-muted">
            As of {day(roster.clock.as_of)}; last rest ended {day(roster.clock.last_rest_ended)} <Mono>{clock(roster.clock.last_rest_ended)}</Mono>.
          </p>
        </div>
      </Panel>

      {sheet && (
        <ReportSheet
          crewId={roster.crew_id}
          onClose={(text) => {
            setSheet(false);
            if (text) setMessage(text);
          }}
        />
      )}
    </div>
  );
}
