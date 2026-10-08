"use client";

import { BellRing, Gauge, History, Mail, Save, ScrollText, ShieldAlert, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { useMe } from "@/components/me";
import { Button, clock, cx, day, Empty, Field, Mono, Notice, Panel, Spinner } from "@/components/ui";
import { tool } from "@/lib/client";

type Threshold = {
  rule_id: string;
  param: string;
  label: string;
  unit: string;
  value: number;
  min: number;
  max: number;
  step: number;
  stricter: "lower" | "higher";
  text: string | null;
};

type Event = {
  at: string;
  action: string;
  actor: string;
  disruption_id?: string;
  plan_id?: string;
  rule_id?: string;
  param?: string;
  from?: number;
  to?: number;
  role?: string;
  reason?: string;
};

type Address = { subscription_id: string; email: string; role: string; added_by: string; added_utc: string; status: string };

type Message = { tone: "ok" | "error"; text: string } | null;

function ThresholdRow({ t, onSaved }: { t: Threshold; onSaved: (m: Message) => void }) {
  const [value, setValue] = useState(t.value);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const changed = value !== t.value;
  const regulatory = t.stricter === "lower" ? t.max : t.min;

  async function save() {
    setBusy(true);
    try {
      await tool("set_rule_threshold", { rule_id: t.rule_id, param: t.param, value, expected: t.value, reason });
      onSaved({
        tone: "ok",
        text: `${t.label} is now ${value}${t.unit}. New answers and every approval use it within a minute; existing analyses update when re-analysed.`,
      });
      setReason("");
    } catch (err) {
      onSaved({ tone: "error", text: err instanceof Error ? err.message : "Failed." });
      setValue(t.value);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="rounded-xl border border-line p-4 transition hover:border-sky-200">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">{t.label}</p>
          <Mono className="text-[11px] text-muted">{t.rule_id}</Mono>
        </div>
        <span className={cx("font-mono text-2xl font-semibold tabular-nums", changed ? "text-accent" : "text-slate-900")}>
          {value}
          <span className="text-sm text-muted">{t.unit}</span>
        </span>
      </div>
      <div className="relative mt-3">
        <input
          type="range"
          min={t.min}
          max={t.max}
          step={t.step}
          value={value}
          onChange={(e) => setValue(Number(e.target.value))}
          aria-label={t.label}
          className="w-full"
        />
        <div className="mt-1 flex justify-between text-[11px] text-muted">
          <span>
            {t.min}
            {t.unit}
            {t.stricter === "lower" && " · strictest"}
            {t.stricter === "higher" && " · regulatory"}
          </span>
          <span>
            {t.max}
            {t.unit}
            {t.stricter === "lower" && " · regulatory"}
            {t.stricter === "higher" && " · strictest"}
          </span>
        </div>
      </div>
      {t.text && <p className="mt-2 text-xs text-muted">{t.text}</p>}
      {changed && (
        <div className="animate-rise mt-3 flex flex-col gap-2 rounded-lg bg-accent-soft/60 p-3 sm:flex-row">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={300}
            placeholder={`Why ${t.label.toLowerCase()} changes from ${t.value}${t.unit}`}
            className="min-w-0 flex-1 rounded-lg border border-line bg-white px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <Button tone="accent" disabled={busy || !reason.trim()} onClick={save}>
            {busy ? <Spinner /> : <Save size={14} />} Save
          </Button>
          <Button tone="ghost" onClick={() => setValue(t.value)}>Undo</Button>
        </div>
      )}
      {value === regulatory && t.value !== regulatory && (
        <p className="mt-2 text-[11px] text-muted">Restores the regulatory value.</p>
      )}
    </li>
  );
}

function describeEvent(e: Event): string {
  if (e.action === "RULE_CHANGED") return `${e.rule_id} ${e.param?.replace(/_/g, " ")} ${e.from} → ${e.to}`;
  if (e.action.startsWith("ALERT")) return `${e.action === "ALERT_ADDED" ? "Alert address added" : "Alert address removed"} (${e.role})`;
  return `${e.action.replace(/_/g, " ").toLowerCase()} ${e.disruption_id ?? ""}${e.plan_id ? ` · ${e.plan_id}` : ""}`;
}

export default function Admin() {
  const me = useMe();
  const isAdmin = me.roles.some((r) => r === "admin" || r === "super_admin");
  const [thresholds, setThresholds] = useState<Threshold[] | null>(null);
  const [events, setEvents] = useState<Event[] | null>(null);
  const [addresses, setAddresses] = useState<Address[] | null>(null);
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    const fail = (err: Error) => setMessage({ tone: "error", text: err.message });
    tool<Threshold[]>("rule_thresholds").then(setThresholds, fail);
    tool<Event[]>("activity_log", { limit: 40 }).then(setEvents, fail);
    tool<Address[]>("alert_addresses").then(setAddresses, (err: Error) => {
      setAddresses([]);
      fail(err);
    });
  }, []);

  useEffect(() => {
    if (isAdmin) load();
  }, [isAdmin, load]);

  if (!isAdmin) return <Notice>Only an admin manages rule thresholds and alerts.</Notice>;

  async function run(call: () => Promise<unknown>, done: string, then?: () => void) {
    setBusy(true);
    setMessage(null);
    try {
      await call();
      setMessage({ tone: "ok", text: done });
      then?.();
      load();
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Failed." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Administration</h1>
        <p className="text-sm text-muted">Rule thresholds, system activity and alert addresses. Plan decisions stay with controllers and approvers.</p>
      </div>
      {message && <Notice tone={message.tone}>{message.text}</Notice>}

      <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
        <Panel
          title="Rule thresholds"
          icon={<Gauge size={14} className="text-accent" />}
          action={
            <span className="flex items-center gap-1 text-[11px] text-muted">
              <ShieldAlert size={12} /> Stricter only, never looser than regulation
            </span>
          }
        >
          {!thresholds ? (
            <Spinner />
          ) : (
            <ul className="grid gap-3 md:grid-cols-2">
              {thresholds.map((t) => (
                <ThresholdRow
                  key={`${t.rule_id}-${t.param}-${t.value}`}
                  t={t}
                  onSaved={(m) => {
                    setMessage(m);
                    load();
                  }}
                />
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="System activity" icon={<History size={14} className="text-accent" />} className="xl:max-h-[640px]" bodyClassName="overflow-y-auto p-2">
          {!events ? (
            <div className="p-2"><Spinner /></div>
          ) : events.length === 0 ? (
            <Empty icon={<ScrollText size={22} />}>No activity yet.</Empty>
          ) : (
            <ol className="flex flex-col">
              {events.map((e, i) => (
                <li key={`${e.at}-${i}`} className="flex gap-3 rounded-lg px-2 py-2 text-sm hover:bg-sunken">
                  <Mono className="w-[86px] shrink-0 text-[11px] text-muted">
                    {day(e.at)} {clock(e.at)}
                  </Mono>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-slate-800">{describeEvent(e)}</span>
                    <span className="block truncate text-[11px] text-muted">
                      {e.actor}
                      {e.reason ? ` — ${e.reason}` : ""}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </div>

      <Panel title="Alert addresses" icon={<BellRing size={14} className="text-accent" />}>
        <form
          className="grid gap-3 sm:grid-cols-[1fr_180px_auto] sm:items-end"
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            const f = new FormData(form);
            const email = String(f.get("email") ?? "");
            const role = String(f.get("role") ?? "");
            run(() => tool("add_alert_address", { email, role }), `AWS has emailed ${email} a confirmation link.`, () => form.reset());
          }}
        >
          <Field label="Email address" name="email" type="email" required maxLength={254} />
          <label className="flex flex-col gap-1 text-sm">
            <span className="font-medium text-slate-700">Role</span>
            <select name="role" className="rounded-lg border border-line bg-surface px-3 py-2">
              <option value="controller">Controller</option>
              <option value="approver">Approver</option>
            </select>
          </label>
          <Button disabled={busy}>
            <Mail size={14} /> Add
          </Button>
        </form>
        <p className="mt-2 text-[11px] text-muted">
          One role per address; it receives alerts once it confirms AWS&apos;s email. At most five new addresses a day.
        </p>
        {addresses && addresses.length > 0 && (
          <ul className="mt-4 divide-y divide-line rounded-xl border border-line">
            {addresses.map((a) => (
              <li key={a.subscription_id} className="flex flex-wrap items-center gap-3 px-3 py-2 text-sm">
                <span className="min-w-0 flex-1 truncate">{a.email}</span>
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs capitalize">{a.role}</span>
                <span className="text-xs text-muted">{a.status}</span>
                <button
                  className="rounded-lg p-1.5 text-muted transition hover:bg-critical-soft hover:text-critical"
                  title="Remove"
                  disabled={busy}
                  onClick={() => run(() => tool("remove_alert_address", { subscription_id: a.subscription_id }), `Removed ${a.email}.`)}
                >
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
