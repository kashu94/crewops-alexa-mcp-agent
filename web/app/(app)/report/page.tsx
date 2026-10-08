"use client";

import {
  Activity,
  AlertOctagon,
  BadgeCheck,
  DoorClosed,
  DoorOpen,
  Inbox,
  Plus,
  RefreshCw,
  Send,
  TowerControl,
  Users,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { Button, clock, cx, day, Empty, Mono, Notice, Panel, Spinner, Status } from "@/components/ui";
import { tool } from "@/lib/client";
import { describe, shownStatus, type MyReport, type Reference } from "@/lib/types";

type Kind =
  | "crew_unavailable"
  | "multiple_crew_unavailable"
  | "certification_lapse"
  | "flight_delay"
  | "gate_closure"
  | "station_disruption"
  | "gate_change"
  | "aircraft_technical";

const KINDS: { kind: Kind; title: string; hint: string; icon: typeof Users }[] = [
  { kind: "crew_unavailable", title: "Sick crew", hint: "One crew member can't fly", icon: AlertOctagon },
  { kind: "multiple_crew_unavailable", title: "Multiple sick crew", hint: "Several out together, covered as one", icon: Users },
  { kind: "certification_lapse", title: "Certification expiry", hint: "A licence, medical or training lapses", icon: BadgeCheck },
  { kind: "flight_delay", title: "Delay", hint: "A departure leaves late", icon: Activity },
  { kind: "gate_closure", title: "Gate closure", hint: "A gate is out of use for a while", icon: DoorClosed },
  { kind: "station_disruption", title: "Station closure", hint: "An airport closes (weather, runway)", icon: TowerControl },
  { kind: "gate_change", title: "Gate change", hint: "Move one departure to another gate", icon: DoorOpen },
  { kind: "aircraft_technical", title: "Aircraft fault", hint: "An aircraft is unavailable", icon: Wrench },
];

const REASONS = ["sick", "fatigue", "personal", "other"];

const input =
  "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm outline-none transition focus:border-accent focus:ring-4 focus:ring-sky-100";

function Label({ text, hint, children }: { text: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="font-medium text-slate-700">{text}</span>
      {children}
      {hint && <span className="text-xs text-muted">{hint}</span>}
    </label>
  );
}

/** A UTC instant from a datetime-local value, as the report wants it. */
const utc = (v: FormDataEntryValue | null) => (v ? `${String(v)}:00Z` : "");
const text = (v: FormDataEntryValue | null) => String(v ?? "").trim();

function CrewChips({ reference, value, onChange }: { reference: Reference; value: string[]; onChange: (v: string[]) => void }) {
  const [typed, setTyped] = useState("");
  const add = () => {
    const id = typed.trim().toUpperCase();
    if (id && !value.includes(id) && value.length < 12) onChange([...value, id]);
    setTyped("");
  };
  const name = (id: string) => reference.crew.find((c) => c.crew_id === id);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <input
          list="crew-list"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          placeholder="C-1042"
          className={input}
        />
        <Button type="button" tone="secondary" onClick={add} disabled={!typed.trim()}>
          <Plus size={14} /> Add
        </Button>
      </div>
      {value.length > 0 && (
        <ul className="flex flex-wrap gap-1.5">
          {value.map((id) => (
            <li key={id} className="inline-flex items-center gap-1.5 rounded-full bg-accent-soft py-1 pl-2.5 pr-1 text-xs text-sky-900">
              <Mono className="font-semibold">{id}</Mono>
              {name(id) && <span className="text-sky-800/80">{name(id)!.name}, {name(id)!.rank}</span>}
              <button
                type="button"
                aria-label={`Remove ${id}`}
                onClick={() => onChange(value.filter((x) => x !== id))}
                className="rounded-full p-0.5 hover:bg-white"
              >
                <X size={12} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Fields({ kind, reference, crewIds, setCrewIds }: { kind: Kind; reference: Reference; crewIds: string[]; setCrewIds: (v: string[]) => void }) {
  const first = reference.week[0] ?? "";
  const last = reference.week[reference.week.length - 1] ?? "";
  const dates = (
    <div className="grid grid-cols-2 gap-3">
      <Label text="From">
        <input name="from_date" type="date" required min={first} max={last} defaultValue={first} className={input} />
      </Label>
      <Label text="Until" hint="Optional: the same day if empty">
        <input name="to_date" type="date" min={first} max={last} className={input} />
      </Label>
    </div>
  );
  const reason = (
    <fieldset className="flex flex-col gap-1.5 text-sm">
      <legend className="mb-1 font-medium text-slate-700">Reason</legend>
      <div className="grid grid-cols-4 gap-2">
        {REASONS.map((r, i) => (
          <label key={r} className="cursor-pointer">
            <input type="radio" name="reason" value={r} defaultChecked={i === 0} className="peer sr-only" />
            <span className="block rounded-lg border border-line px-2 py-2 text-center text-xs capitalize transition peer-checked:border-accent peer-checked:bg-accent-soft peer-checked:font-semibold">
              {r}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
  const span = (
    <div className="grid grid-cols-2 gap-3">
      <Label text="From (UTC)">
        <input name="from_utc" type="datetime-local" required min={`${first}T00:00`} max={`${last}T23:59`} className={input} />
      </Label>
      <Label text="Until (UTC)">
        <input name="to_utc" type="datetime-local" required min={`${first}T00:00`} max={`${last}T23:59`} className={input} />
      </Label>
    </div>
  );
  switch (kind) {
    case "crew_unavailable":
      return (
        <>
          <Label text="Crew member">
            <input name="crew_id" list="crew-list" required placeholder="C-1042" className={input} />
          </Label>
          {dates}
          {reason}
        </>
      );
    case "multiple_crew_unavailable":
      return (
        <>
          <Label text="Crew members" hint="Two to twelve. They are covered together, so none is offered to cover another.">
            <CrewChips reference={reference} value={crewIds} onChange={setCrewIds} />
          </Label>
          {dates}
          {reason}
        </>
      );
    case "certification_lapse":
      return (
        <>
          <Label text="Crew member">
            <input name="crew_id" list="crew-list" required placeholder="C-5417" className={input} />
          </Label>
          <div className="grid grid-cols-2 gap-3">
            <Label text="Certificate">
              <select name="cert_type" required className={input}>
                {reference.cert_types.map((c) => (
                  <option key={c} value={c}>
                    {c.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </Label>
            <Label text="Valid until">
              <input name="valid_to" type="date" required min={first} max={last} className={input} />
            </Label>
          </div>
          <p className="text-xs text-muted">The crew member sees a notice on their CrewOps page; training and crew records are emailed.</p>
        </>
      );
    case "flight_delay":
      return (
        <>
          <Label text="Flight" hint="Number and date, e.g. DX412-2026-09-15">
            <input name="flight_id" list="flight-list" required placeholder="DX412-2026-09-15" className={input} />
          </Label>
          <Label text="Delay (minutes)">
            <input name="delay_minutes" type="number" min={1} max={1440} required defaultValue={60} className={input} />
          </Label>
          <p className="text-xs text-muted">
            Flight information, operations monitoring (who tells the captain), the station, gate control and the next
            departure at the same gate are told.
          </p>
        </>
      );
    case "gate_closure":
      return (
        <>
          <Label text="Gate">
            <select name="gate" required className={input}>
              {reference.gates.map((g) => (
                <option key={g}>{g}</option>
              ))}
            </select>
          </Label>
          {span}
          <Label text="Reason">
            <input name="reason" required maxLength={200} placeholder="Jet bridge fault" className={input} />
          </Label>
        </>
      );
    case "station_disruption":
      return (
        <>
          <Label text="Station">
            <select name="station" required className={input}>
              {reference.stations.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </Label>
          {span}
          <Label text="Reason">
            <input name="reason" required maxLength={200} placeholder="Fog below landing minima" className={input} />
          </Label>
        </>
      );
    case "gate_change":
      return (
        <>
          <Label text="Flight">
            <input name="flight_id" list="flight-list" required placeholder="DX401-2026-09-15" className={input} />
          </Label>
          <Label text="New gate">
            <select name="new_gate" required className={input}>
              {reference.gates.map((g) => (
                <option key={g}>{g}</option>
              ))}
            </select>
          </Label>
        </>
      );
    case "aircraft_technical":
      return (
        <>
          <Label text="Aircraft">
            <select name="aircraft" required className={input}>
              {reference.aircraft.map((a) => (
                <option key={a}>{a}</option>
              ))}
            </select>
          </Label>
          {span}
        </>
      );
  }
}

function detailsOf(kind: Kind, f: FormData, crewIds: string[]): Record<string, unknown> {
  const dates = () => ({ from_date: text(f.get("from_date")), to_date: text(f.get("to_date")) || text(f.get("from_date")) });
  switch (kind) {
    case "crew_unavailable":
      return { crew_id: text(f.get("crew_id")).toUpperCase(), ...dates(), reason: text(f.get("reason")) };
    case "multiple_crew_unavailable":
      return { crew_ids: crewIds, ...dates(), reason: text(f.get("reason")) };
    case "certification_lapse":
      return { crew_id: text(f.get("crew_id")).toUpperCase(), cert_type: text(f.get("cert_type")), valid_to: text(f.get("valid_to")) };
    case "flight_delay":
      return { flight_id: text(f.get("flight_id")).toUpperCase(), delay_minutes: Number(f.get("delay_minutes")) };
    case "gate_closure":
      return { gate: text(f.get("gate")), from_utc: utc(f.get("from_utc")), to_utc: utc(f.get("to_utc")), reason: text(f.get("reason")) };
    case "station_disruption":
      return { station: text(f.get("station")), from_utc: utc(f.get("from_utc")), to_utc: utc(f.get("to_utc")), reason: text(f.get("reason")) };
    case "gate_change":
      return { flight_id: text(f.get("flight_id")).toUpperCase(), new_gate: text(f.get("new_gate")) };
    case "aircraft_technical":
      return { aircraft: text(f.get("aircraft")), from_utc: utc(f.get("from_utc")), to_utc: utc(f.get("to_utc")) };
  }
}

function MyReports({ reports, onRefresh }: { reports: MyReport[] | null; onRefresh: () => void }) {
  return (
    <Panel
      title="My reports"
      icon={<Inbox size={14} className="text-accent" />}
      action={
        <button onClick={onRefresh} className="rounded-md p-1 text-muted hover:bg-sunken hover:text-foreground" title="Refresh">
          <RefreshCw size={14} />
        </button>
      }
    >
      {!reports ? (
        <Spinner />
      ) : reports.length === 0 ? (
        <Empty icon={<Inbox size={22} />}>Nothing reported yet.</Empty>
      ) : (
        <ol className="flex flex-col gap-2">
          {reports.map((r) => (
            <li key={r.disruption_id} className="rounded-xl border border-line px-3 py-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium">{describe(r)}</span>
                <Status value={shownStatus(r.status, r.handled ? "internal" : null)} />
              </div>
              <p className="mt-0.5 text-[11px] text-muted">
                <Mono>{r.disruption_id}</Mono> · {day(r.reported_utc)} <Mono>{clock(r.reported_utc)}</Mono>
              </p>
              <p className="mt-1.5 text-xs text-slate-700">{r.handled ?? r.outcome}</p>
              {r.remark && (
                <p className="mt-1 rounded-lg bg-ok-soft px-2 py-1 text-xs text-emerald-900">
                  Crew control: {r.remark}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

export default function ReportPage() {
  const [reference, setReference] = useState<Reference | null>(null);
  const [reports, setReports] = useState<MyReport[] | null>(null);
  const [kind, setKind] = useState<Kind>("crew_unavailable");
  const [crewIds, setCrewIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadReports = useCallback(() => {
    tool<MyReport[]>("my_reports").then(setReports, () => setReports([]));
  }, []);

  useEffect(() => {
    tool<Reference>("report_reference").then(setReference, (err: Error) => setError(err.message));
    loadReports();
  }, [loadReports]);

  // A new report is analysed within a minute: look again while one is still being read.
  const waiting = useMemo(() => (reports ?? []).some((r) => r.status === "REPORTED"), [reports]);
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(loadReports, 10_000);
    return () => clearInterval(t);
  }, [waiting, loadReports]);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    if (kind === "multiple_crew_unavailable" && crewIds.length < 2) {
      setMessage({ tone: "error", text: "Add at least two crew members." });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const f = new FormData(form);
      const out = await tool<{ report_id: string; note: string }>("report_disruption", {
        report_type: kind,
        details: detailsOf(kind, f, crewIds),
        narrative: text(f.get("narrative")),
      });
      setMessage({ tone: "ok", text: `Recorded as ${out.report_id}. ${out.note}` });
      form.reset();
      setCrewIds([]);
      setTimeout(loadReports, 1500);
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Failed." });
    } finally {
      setBusy(false);
    }
  }

  if (error) return <Notice tone="error">{error}</Notice>;

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Record a disruption</h1>
        <p className="text-sm text-muted">
          CrewOps works out what it breaks, who must be told and how it gets resolved. Crew control takes it from there.
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <Panel>
          {!reference ? (
            <Spinner />
          ) : (
            <form onSubmit={submit} className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" role="radiogroup" aria-label="Type">
                {KINDS.map(({ kind: k, title, hint, icon: Icon }) => (
                  <button
                    key={k}
                    type="button"
                    role="radio"
                    aria-checked={kind === k}
                    onClick={() => {
                      setKind(k);
                      setMessage(null);
                    }}
                    className={cx(
                      "lift flex flex-col items-start gap-1 rounded-xl border p-3 text-left transition",
                      kind === k ? "border-accent bg-accent-soft ring-1 ring-sky-200" : "border-line bg-surface",
                    )}
                  >
                    <Icon size={16} className={kind === k ? "text-accent" : "text-muted"} />
                    <span className="text-sm font-semibold">{title}</span>
                    <span className="text-[11px] leading-snug text-muted">{hint}</span>
                  </button>
                ))}
              </div>

              <div key={kind} className="animate-rise flex flex-col gap-3">
                <Fields kind={kind} reference={reference} crewIds={crewIds} setCrewIds={setCrewIds} />
                <Label text="Anything crew control should know" hint="Optional">
                  <input name="narrative" maxLength={500} className={input} />
                </Label>
              </div>

              {message && <Notice tone={message.tone}>{message.text}</Notice>}
              <Button type="submit" tone="accent" disabled={busy} className="self-start">
                {busy ? <Spinner /> : <Send size={14} />} Record disruption
              </Button>

              <datalist id="crew-list">
                {reference.crew.map((c) => (
                  <option key={c.crew_id} value={c.crew_id}>
                    {c.name}, {c.rank}, {c.base}
                  </option>
                ))}
              </datalist>
              <datalist id="flight-list">
                {reference.flights.map((f) => (
                  <option key={f.flight_id} value={f.flight_id}>
                    {f.from}→{f.to} {f.dep_utc.slice(11, 16)}Z{f.gate ? ` · ${f.gate}` : ""}
                  </option>
                ))}
              </datalist>
            </form>
          )}
        </Panel>
        <MyReports reports={reports} onRefresh={loadReports} />
      </div>
    </div>
  );
}
