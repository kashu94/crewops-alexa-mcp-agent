"use client";

import {
  AlertTriangle,
  Check,
  ChevronDown,
  Gauge,
  GitMerge,
  Scale,
  ShieldCheck,
  Swords,
  Timer,
  UserX,
  Wallet,
} from "lucide-react";
import { useState } from "react";

import { tool } from "@/lib/client";
import type { LiveOption, Rival, Seat } from "@/lib/types";

import { useMe } from "./me";
import { LegalityCheck } from "./solutions";
import { Button, cx, Mono, rupees, Spinner } from "./ui";

const STRATEGY: Record<string, string> = {
  reserve_callout: "reserve",
  day_off_callout: "day off",
  positioning: "positioning",
  cancel: "cancellation",
};

const POLICY: { key: string; title: string; hint: string; icon: typeof Wallet }[] = [
  { key: "cheapest", title: "Cheapest", hint: "Lowest cost; the engine's recommendation", icon: Wallet },
  { key: "least_delay", title: "Least delay", hint: "Earliest departures, then cost", icon: Timer },
  { key: "keeps_most_reserve_cover", title: "Keeps reserve cover", hint: "Leaves the most reserves for later", icon: ShieldCheck },
];

export function seatKey(seat: Seat): string {
  return `${seat.pairing_id}/${seat.role}/${seat.crew_out ?? "-"}`;
}

const ABSENCES = new Set(["crew_unavailable", "multiple_crew_unavailable", "certification_lapse"]);

/** Passengers booked on the flights this seat leaves uncrewed. */
function passengersProtected(seat: Seat): number {
  return (seat.context?.flights ?? []).filter((f) => f.state === "uncrewed").reduce((n, f) => n + (f.booked ?? 0), 0);
}

/** Who else wants these crew, and the option to plan the competing disruptions together. */
function Competing({ seat, contention }: { seat: Seat; contention: Record<string, Rival[]> }) {
  const me = useMe();
  const options = seat.live?.options ?? [];
  const contended = options.filter((o) => o.crew_id && contention[o.crew_id]?.length);
  const [joint, setJoint] = useState<{ ok: boolean; result?: JointResult; error?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  if (contended.length === 0) return null;

  const others = new Set(contended.flatMap((o) => contention[o.crew_id!].map((r) => r.disruption_id)));
  const reserves = contended.filter((o) => o.strategy === "reserve_callout").length;
  const top = options[0];
  const topRivals = top?.crew_id ? contention[top.crew_id] ?? [] : [];
  // The people out in the competing disruptions, with their trips (one may fly several).
  // Only absences: in a delay the crew member is still flying, so it is not their event.
  const rivalOut = [
    ...new Map(
      contended
        .flatMap((o) => contention[o.crew_id!])
        .filter((r) => r.crew_out && r.kind === "option" && ABSENCES.has(r.report_type ?? ""))
        .map((r) => [`${r.crew_out}/${r.pairing_id}`, { crew_id: r.crew_out as string, pairing_id: r.pairing_id }]),
    ).values(),
  ];

  async function buildJoint() {
    setBusy(true);
    try {
      const events = [...(seat.crew_out ? [{ crew_id: seat.crew_out, pairing_id: seat.pairing_id }] : []), ...rivalOut];
      setJoint(await tool("crew_tool", { name: "joint_plan", arguments: { events } }));
    } catch (err) {
      setJoint({ ok: false, error: err instanceof Error ? err.message : "Failed." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="animate-rise rounded-2xl border border-amber-200 bg-gradient-to-b from-amber-50 to-white p-4">
      <h3 className="flex items-center gap-2 font-semibold text-amber-900">
        <AlertTriangle size={16} /> Competing for the same crew
      </h3>
      <p className="mt-1 text-sm text-amber-900/80">
        {topRivals.length > 0 && top ? (
          <>
            {top.crew_rank} <Mono>{top.crew_id}</Mono> is rank 1 here and also wanted by{" "}
            {topRivals.map((r) => r.disruption_id).join(", ")}.{" "}
          </>
        ) : null}
        {options.length - contended.length} of your {options.length} legal options are uncontended.
      </p>
      <ul className="mt-3 flex flex-col gap-2">
        {contended.map((o) => (
          <li key={o.crew_id} className="rounded-xl border border-amber-100 bg-white px-3 py-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Mono className="font-semibold">{o.crew_id}</Mono>
              <span>{o.name}</span>
              <span className="rounded-full border border-line px-2 py-0.5 text-[11px]">Rank {o.rank} here</span>
              {o.strategy === "reserve_callout" && (
                <span className="rounded-full border border-amber-300 px-2 py-0.5 text-[11px] text-amber-800">Reserve</span>
              )}
              <span className="ml-auto font-mono">{rupees(o.cost_inr)}</span>
            </div>
            <p className="mt-1 text-xs text-muted">
              Also wanted by{" "}
              {contention[o.crew_id!].map((r, i) => (
                <span key={`${r.disruption_id}-${r.pairing_id}-${r.kind}-${r.rank ?? r.plan_id}`}>
                  {i > 0 && ", "}
                  <b className="text-slate-700">{r.desks.map((d) => d.controller).join(" / ")}</b>
                  &apos;s desk on <Mono>{r.pairing_id}</Mono> ({r.disruption_id},{" "}
                  {r.kind === "plan" ? "in a plan awaiting approval" : `their rank ${r.rank}`})
                </span>
              ))}
            </p>
          </li>
        ))}
      </ul>
      <ul className="mt-3 list-disc pl-5 text-xs text-amber-900/80">
        <li>
          {contended.length} of {options.length} legal options are also wanted by {others.size} other open
          disruption{others.size === 1 ? "" : "s"}.
        </li>
        {reserves > 0 && <li>{reserves} contended option(s) are reserve callouts, which draw on the same scarce buffer.</li>}
        <li>Advisory only: nothing is reserved, and approval refuses an assignment that clashes with one already committed.</li>
      </ul>
      {me.roles.includes("controller") && rivalOut.length > 0 && (
        <div className="mt-4 rounded-xl border border-line bg-white p-3">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <GitMerge size={15} className="text-accent" /> Resolve these together
          </p>
          <p className="mt-1 text-xs text-muted">
            A joint plan searches every legal combination for all the competing seats and returns the cheapest whole
            answer, instead of whoever submits first taking the cheap option.
          </p>
          <Button type="button" tone="secondary" className="mt-2" disabled={busy} onClick={buildJoint}>
            {busy ? <Spinner /> : <GitMerge size={14} />} Build a joint plan
          </Button>
          {joint && <JointView joint={joint} />}
        </div>
      )}
    </section>
  );
}

type JointResult = {
  assignments?: { pairing_id: string; role: string; crew_id: string; name: string; cost_inr: number }[];
  unfilled?: { pairing_id: string; role: string }[];
  total_cost_inr?: number;
};

function JointView({ joint }: { joint: { ok: boolean; result?: JointResult; error?: string } }) {
  if (!joint.ok || !joint.result) return <p className="mt-2 text-xs text-critical">{joint.error ?? "No joint plan."}</p>;
  const r = joint.result;
  return (
    <div className="animate-rise mt-3 rounded-lg bg-indigo-50/60 p-3 text-xs">
      <ul className="flex flex-col gap-1">
        {r.assignments?.map((a) => (
          <li key={`${a.pairing_id}-${a.role}-${a.crew_id}`} className="flex justify-between gap-2">
            <span>
              <Mono>{a.pairing_id}</Mono> {a.role}: <b>{a.name}</b> <Mono>{a.crew_id}</Mono>
            </span>
            <Mono>{rupees(a.cost_inr)}</Mono>
          </li>
        ))}
        {r.unfilled?.map((u) => (
          <li key={`${u.pairing_id}-${u.role}`} className="text-red-800">
            <Mono>{u.pairing_id}</Mono> {u.role}: no legal cover in the joint plan
          </li>
        ))}
      </ul>
      {r.total_cost_inr !== undefined && <p className="mt-2 font-semibold">Total {rupees(r.total_cost_inr)}</p>}
      <p className="mt-1 text-muted">
        Each desk submits its own seat; choose the joint plan&apos;s person for yours below.
      </p>
    </div>
  );
}

/** Plain words about the chosen option, from the engine's own figures. */
function Narrative({ seat, option }: { seat: Seat; option: LiveOption }) {
  const options = seat.live?.options ?? [];
  const cheapest = options[0];
  const extra = cheapest ? option.cost_inr - cheapest.cost_inr : 0;
  const reservesLeft =
    options.filter((o) => o.strategy === "reserve_callout").length - (option.strategy === "reserve_callout" ? 1 : 0);
  const flights = (seat.context?.flights ?? []).filter((f) => f.state === "uncrewed");
  const runnerUp = options.find((o) => o.crew_id !== option.crew_id);
  const passengers = passengersProtected(seat);
  return (
    <section className="animate-rise rounded-2xl border border-line bg-surface p-4">
      <p className="text-[15px] leading-relaxed">
        Assign <b>{option.name}</b> <Mono>{option.crew_id}</Mono> ({STRATEGY[option.strategy] ?? option.strategy}).{" "}
        {option.delay_hours > 0
          ? `The first departure leaves about ${option.delay_hours}h late; `
          : `All ${flights.length} leg(s) operate on time, `}
        protecting {passengers.toLocaleString("en-IN")} booked passengers. Cost {rupees(option.cost_inr)}.{" "}
        {option.strategy === "reserve_callout"
          ? reservesLeft > 0
            ? `Uses a reserve; ${reservesLeft} other reserve ${seat.role}(s) among the legal options stay free.`
            : `Uses the only reserve among the legal options.`
          : reservesLeft > 0
            ? `Uses no reserve: all ${reservesLeft} reserve ${seat.role}(s) among the legal options stay free.`
            : "Uses no reserve."}
      </p>
      <ul className="mt-3 list-disc pl-5 text-sm text-slate-700">
        {extra > 0 && cheapest && (
          <li>
            {rupees(extra)} more than the cheapest legal option ({cheapest.name}, {rupees(cheapest.cost_inr)}).
          </li>
        )}
        {extra > 0 && cheapest?.strategy === "reserve_callout" && option.strategy !== "reserve_callout" && (
          <li>The premium keeps a reserve free: the cheapest option uses one.</li>
        )}
        {option.reachability_minutes != null && <li>Reachable in {option.reachability_minutes} minutes from {option.base}.</li>}
        {runnerUp && (
          <li>
            Runner-up: {runnerUp.name} <Mono>{runnerUp.crew_id}</Mono> ({STRATEGY[runnerUp.strategy] ?? runnerUp.strategy}) at{" "}
            {rupees(runnerUp.cost_inr)}.
          </li>
        )}
      </ul>
      <dl className="mt-4 grid grid-cols-2 gap-3 border-t border-line pt-3 sm:grid-cols-4">
        {[
          ["Total cost", rupees(option.cost_inr)],
          ["Max delay", `${option.delay_hours}h`],
          ["Passengers protected", passengers.toLocaleString("en-IN")],
          ["Reserve cover kept", option.resilience != null ? `${option.resilience}/100` : "—"],
        ].map(([k, v]) => (
          <div key={k}>
            <dt className="text-xs text-muted">{k}</dt>
            <dd className="font-mono text-lg font-semibold">{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function OptionRow({
  option,
  pairingId,
  selected,
  recommended,
  rivals,
  onSelect,
}: {
  option: LiveOption;
  pairingId: string;
  selected: boolean;
  recommended: boolean;
  rivals: Rival[];
  onSelect?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <li
      className={cx(
        "rounded-xl border transition",
        selected ? "border-emerald-300 bg-emerald-50/70" : "border-transparent bg-sunken hover:border-sky-200",
      )}
    >
      <div className="flex items-center gap-3 px-3 py-2.5">
        {onSelect ? (
          <button
            type="button"
            onClick={onSelect}
            aria-pressed={selected}
            title={selected ? "Chosen" : "Choose this option"}
            className={cx(
              "grid h-5 w-5 shrink-0 place-items-center rounded-full border transition",
              selected ? "border-emerald-500 bg-emerald-500 text-white" : "border-slate-300 bg-white hover:border-accent",
            )}
          >
            {selected && <Check size={12} strokeWidth={3} />}
          </button>
        ) : null}
        <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="flex min-w-0 flex-1 items-center gap-2 text-left">
          <span className="font-medium">{option.name}</span>
          <span className="truncate text-xs text-muted">
            {option.crew_rank} · {option.base} · {STRATEGY[option.strategy] ?? option.strategy}
            {option.reachability_minutes != null ? ` · ${option.reachability_minutes} min away` : ""}
          </span>
          {recommended && <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10px] font-semibold text-sky-800">recommended</span>}
          {rivals.length > 0 && (
            <span className="flex items-center gap-1 rounded-full bg-pending-soft px-2 py-0.5 text-[10px] font-semibold text-amber-800">
              <Swords size={10} /> wanted by {rivals.length}
            </span>
          )}
        </button>
        <Mono className="text-sm">{rupees(option.cost_inr)}</Mono>
        <Mono className="w-10 text-right text-xs text-muted">{option.delay_hours}h</Mono>
        <ChevronDown size={14} className={cx("text-muted transition", open && "rotate-180")} />
      </div>
      {open && option.crew_id && (
        <div className="border-t border-line px-3 pb-3 pt-2">
          <LegalityCheck crewId={option.crew_id} pairingId={pairingId} strategy={option.strategy} delay={option.delay_hours} />
        </div>
      )}
    </li>
  );
}

/** A seat's whole decision: competition, recommendations, the chosen option, every
 * legal option and every excluded candidate. */
export function SeatWorkspace({
  seat,
  contention,
  chosen,
  recommendedId,
  onChoose,
}: {
  seat: Seat;
  contention: Record<string, Rival[]>;
  chosen: string | null;
  recommendedId: string | null;
  onChoose?: (crewId: string) => void;
}) {
  const [showExcluded, setShowExcluded] = useState(false);
  const live = seat.live;
  const coverable = seat.status === "options";
  if (!live || live.options.length === 0) {
    return (
      <p className="rounded-xl bg-critical-soft px-3 py-2 text-sm text-red-900">
        {seat.status === "already_started"
          ? "This trip has already started; it can no longer be re-crewed."
          : coverable
            ? "No legal cover any more: something changed since the analysis. Re-run the analysis."
            : "No legal cover for this seat."}{" "}
        {seat.note}
      </p>
    );
  }
  const selectedId = chosen ?? recommendedId;
  const selected = live.options.find((o) => o.crew_id === selectedId) ?? live.options[0];
  const recommendedListed = !recommendedId || live.options.some((o) => o.crew_id === recommendedId);
  const considered = live.funnel[0]?.remaining;
  const cancel = live.cancellation;

  return (
    <div className="flex flex-col gap-4">
      {!coverable && (
        <p className="rounded-xl bg-pending-soft px-3 py-2 text-sm text-amber-900">
          The analysis found no legal cover here, but the roster or rules have changed since. These options are
          shown for information; re-run the analysis to plan with them.
        </p>
      )}
      {coverable && !recommendedListed && (
        <p className="rounded-xl bg-pending-soft px-3 py-2 text-sm text-amber-900">
          The joint plan&apos;s pick <Mono>{recommendedId}</Mono> is not in the current option list. Submitting the
          recommendation re-checks it; or choose another option below.
        </p>
      )}
      <Competing seat={seat} contention={contention} />

      <div>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="flex items-center gap-2 font-semibold">
            <Scale size={16} className="text-accent" /> Recommendations
          </h3>
          {seat.reserves && seat.reserves.length > 0 && (
            <span className="text-xs text-muted">
              Reserve {seat.role}s on call: {seat.reserves.map((r) => `${r.base} ${r.on_call}`).join(" · ")}
            </span>
          )}
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          {POLICY.map(({ key, title, hint, icon: Icon }) => {
            const o = live.policies[key];
            if (!o) return null;
            const active = o.crew_id === selected.crew_id;
            return (
              <button
                key={key}
                type="button"
                disabled={!onChoose}
                onClick={() => o.crew_id && onChoose?.(o.crew_id)}
                className={cx(
                  "lift rounded-2xl border p-4 text-left disabled:cursor-default",
                  active ? "border-emerald-300 bg-emerald-50/70 ring-1 ring-emerald-200" : "border-line bg-surface",
                )}
                title={hint}
              >
                <span className="flex items-center justify-between">
                  <span className="flex items-center gap-1.5 text-sm font-semibold">
                    <Icon size={14} className="text-accent" /> {title}
                  </span>
                  {active && <Check size={15} className="text-emerald-600" />}
                </span>
                <span className="mt-2 block font-medium">
                  {o.name} <Mono className="text-xs text-sky-700">{o.crew_id}</Mono>
                </span>
                <span className="block text-xs text-muted">{STRATEGY[o.strategy] ?? o.strategy}</span>
                <dl className="mt-3 grid grid-cols-[1fr_auto] gap-y-1 text-xs">
                  <dt className="text-muted">Cost</dt>
                  <dd className="font-mono">{rupees(o.cost_inr)}</dd>
                  <dt className="text-muted">Delay</dt>
                  <dd className="font-mono">{o.delay_hours}h</dd>
                  <dt className="text-muted">Reserve cover kept</dt>
                  <dd className="font-mono">{o.resilience ?? "—"}/100</dd>
                </dl>
              </button>
            );
          })}
        </div>
      </div>

      <Narrative seat={seat} option={selected} />

      <section className="rounded-2xl border border-line bg-surface p-4">
        <h3 className="mb-3 flex items-center gap-2 font-semibold">
          <Gauge size={16} className="text-accent" /> All legal options
          <span className="text-xs font-normal text-muted">
            {live.options.length} shown of {live.legal_options} legal{considered ? `, ${considered} considered` : ""}
          </span>
        </h3>
        <ol className="flex max-h-[520px] flex-col gap-1.5 overflow-y-auto pr-1">
          {live.options.map((o) => (
            <OptionRow
              key={`${o.rank}-${o.crew_id}`}
              option={o}
              pairingId={seat.pairing_id}
              selected={o.crew_id === selected.crew_id}
              recommended={o.crew_id === recommendedId}
              rivals={(o.crew_id && contention[o.crew_id]) || []}
              onSelect={onChoose && o.crew_id ? () => onChoose(o.crew_id!) : undefined}
            />
          ))}
          {cancel && (
            <li className="flex items-center gap-3 rounded-xl bg-critical-soft/60 px-3 py-2.5 text-sm text-red-900">
              <span className="flex-1 font-medium">
                Cancel {(seat.impact?.uncovered_flights ?? []).length || "the"} flight
                {(seat.impact?.uncovered_flights ?? []).length === 1 ? "" : "s"}
              </span>
              <Mono>{rupees(cancel.cost_inr)}</Mono>
              <span className="text-xs">{passengersProtected(seat).toLocaleString("en-IN")} booked passengers rebooked</span>
            </li>
          )}
        </ol>

        <div className="mt-4 border-t border-line pt-3">
          <button
            type="button"
            onClick={() => setShowExcluded((v) => !v)}
            aria-expanded={showExcluded}
            className="flex w-full items-center justify-between text-sm font-semibold"
          >
            <span className="flex items-center gap-2">
              <UserX size={15} className="text-muted" /> Excluded candidates
              <span className="text-xs font-normal text-muted">{live.excluded_count ?? live.excluded.length}</span>
            </span>
            <ChevronDown size={15} className={cx("text-muted transition", showExcluded && "rotate-180")} />
          </button>
          {showExcluded && (
            <div className="animate-rise mt-3 flex flex-col gap-3">
              <p className="text-xs text-muted">Considered and rejected, so it is clear nobody was overlooked.</p>
              <ol className="flex flex-wrap items-center gap-1.5 text-[11px]">
                {live.funnel.map((f, i) => (
                  <li key={f.stage} className="flex items-center gap-1.5">
                    {i > 0 && <span className="text-muted">→</span>}
                    <span className="rounded-full bg-sunken px-2 py-0.5" title={f.reason || undefined}>
                      {f.stage} <b>{f.remaining}</b>
                    </span>
                  </li>
                ))}
              </ol>
              <ul className="flex max-h-80 flex-col gap-2 overflow-y-auto pr-1">
                {live.excluded.map((e) => (
                  <li key={e.crew_id} className="rounded-lg px-2 py-1.5 text-sm hover:bg-sunken">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{e.name}</span>
                      <Mono className="text-xs text-muted">{e.crew_id}</Mono>
                      <span className="text-xs text-muted">ruled out at: {e.stage}</span>
                      <span className="ml-auto flex gap-1">
                        {(e.rule_ids ?? []).map((r) => (
                          <Mono key={r} className="rounded-full border border-red-200 px-2 py-0.5 text-[10px] text-red-700">
                            ✕ {r}
                          </Mono>
                        ))}
                      </span>
                    </div>
                    <p className="text-xs text-slate-600">{(e.reasons ?? []).join("; ")}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
