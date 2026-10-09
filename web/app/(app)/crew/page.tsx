"use client";

import { BellRing, CalendarDays, Clock3, HeartPulse, MoonStar, Plane, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { AIRLINES } from "@/components/airline";
import { useMe } from "@/components/me";
import { Button, clock, cx, day, Empty, Field, Mono, Notice, Panel, Spinner, Status } from "@/components/ui";
import { tool } from "@/lib/client";

type Day = { date: string; flights: string[]; report_utc: string; release_utc: string };
type Roster = {
  crew_id: string;
  airline?: string | null;
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
  notices?: { disruption_id: string; status: string; at: string | null; message: string }[];
  reported_unavailable?: { disruption_id: string; from_date: string; to_date: string; reason: string | null; status: string }[];
};

type Filed = { disruption_id: string; status: string; outcome: string; reported_utc: string | null; details: Record<string, unknown> };

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

function ReportSheet({
  crewId,
  taken,
  onClose,
}: {
  crewId: string;
  taken: NonNullable<Roster["reported_unavailable"]>;
  onClose: (text?: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [from, setFrom] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function send(form: FormData) {
    setBusy(true);
    setError(null);
    const from = String(form.get("from") ?? "");
    const to = String(form.get("to") ?? "") || from;
    if (to < from) {
      setError("'Until' must be on or after 'From'.");
      setBusy(false);
      return;
    }
    const clash = taken.find((t) => from <= t.to_date && t.from_date <= to);
    if (clash) {
      setError(`You're already reported unavailable ${clash.from_date} to ${clash.to_date}.`);
      setBusy(false);
      return;
    }
    try {
      const out = await tool<{ report_id: string; note: string }>("report_disruption", {
        report_type: "crew_unavailable",
        details: {
          crew_id: crewId,
          from_date: from,
          to_date: to,
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
            <Field label="From" name="from" type="date" required autoFocus value={from} onChange={(e) => setFrom(e.target.value)} />
            <Field label="Until" name="to" type="date" hint="Optional: the same day if empty" min={from || undefined} />
          </div>
          {taken.length > 0 && (
            <p className="rounded-lg bg-sunken px-3 py-2 text-xs text-muted">
              Already reported:{" "}
              {taken.map((t) => (t.from_date === t.to_date ? t.from_date : `${t.from_date} to ${t.to_date}`)).join("; ")}
            </p>
          )}
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
          <Field label="Anything to tell the reporter?" name="narrative" maxLength={500} hint="Optional" />
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
  const [filed, setFiled] = useState<Filed[]>([]);

  const reload = useCallback(() => {
    if (!me.crew_id) return;
    tool<Roster>("my_roster").then(setRoster, (err: Error) => setError(err.message));
    tool<Filed[]>("my_reports").then(setFiled, () => undefined);
  }, [me.crew_id]);
  useEffect(reload, [reload]);

  // A report is checked within a minute: look again while one is still being checked.
  const checking = filed.some((f) => f.status === "QUEUED" || f.status === "REPORTED");
  useEffect(() => {
    if (!checking) return;
    const t = setInterval(reload, 10_000);
    return () => clearInterval(t);
  }, [checking, reload]);

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
          {roster.airline && <> · {AIRLINES[roster.airline]?.name ?? roster.airline}</>}
        </p>
        <button
          onClick={() => setSheet(true)}
          className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl bg-white/95 py-2.5 text-sm font-semibold text-red-700 shadow transition hover:bg-white"
        >
          <HeartPulse size={16} /> I can&apos;t fly — report unavailable
        </button>
      </div>

      {message && <Notice tone="ok">{message}</Notice>}

      {(roster.notices ?? []).length > 0 && (
        <section aria-label="Notices for you" className="flex flex-col gap-2">
          {(roster.notices ?? []).map((n) => (
            <div
              key={n.disruption_id}
              className="animate-rise flex gap-3 rounded-2xl border border-amber-200 bg-gradient-to-br from-amber-50 to-white p-4"
            >
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-white text-amber-600 shadow-sm">
                <BellRing size={17} />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-semibold text-amber-900">Action needed</p>
                <p className="mt-0.5 text-sm leading-relaxed text-amber-950/90">{n.message}</p>
                <p className="mt-1 text-[11px] text-amber-900/60">
                  {day(n.at)} · <Mono>{n.disruption_id}</Mono>
                </p>
              </div>
            </div>
          ))}
        </section>
      )}

      {filed.length > 0 && (
        <Panel title="My reports" icon={<HeartPulse size={14} className="text-accent" />}>
          <ul className="flex flex-col gap-2">
            {filed.slice(0, 5).map((f) => (
              <li key={f.disruption_id} className="rounded-xl border border-line px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">
                    {String(f.details.from_date ?? "")}
                    {f.details.to_date && f.details.to_date !== f.details.from_date ? ` to ${String(f.details.to_date)}` : ""}
                  </span>
                  <Status value={f.status} />
                </div>
                <p className={cx("mt-1 text-xs", f.status === "REJECTED" ? "text-critical" : "text-muted")}>{f.outcome}</p>
              </li>
            ))}
          </ul>
        </Panel>
      )}

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
          taken={roster.reported_unavailable ?? []}
          onClose={(text) => {
            setSheet(false);
            if (text) {
              setMessage(text);
              setTimeout(reload, 1500);
            }
          }}
        />
      )}
    </div>
  );
}
