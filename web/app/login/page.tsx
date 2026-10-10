"use client";

import { ArrowRight, Lock, LogOut, Plane, ShieldCheck, Sparkles, Zap } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { NightApproach } from "@/components/login-art";
import { Button, Field, Notice, Spinner } from "@/components/ui";
import { signIn, signOut } from "@/lib/client";
import { homeFor, type Me } from "@/lib/roles";

/** Who is signed in in this browser, without the client's sign-in redirect (this is the
 * sign-in page). */
async function signedIn(): Promise<Me | null> {
  const r = await fetch("/api/tools/whoami", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => null);
  if (!r?.ok) return null;
  const data = (await r.json().catch(() => null)) as { result?: Me } | null;
  return data?.result ?? null;
}

const POINTS = [
  { icon: Zap, text: "Disruptions analysed the moment they are reported" },
  { icon: ShieldCheck, text: "Every figure checked against the legality engine" },
  { icon: Sparkles, text: "Ask by voice; four-eyes approval on every roster change" },
];

export default function Login() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [current, setCurrent] = useState<Me | null>(null);

  useEffect(() => {
    signedIn().then(setCurrent);
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await signIn(String(form.get("username") ?? ""), String(form.get("password") ?? ""));
      const me = await signedIn();
      window.location.replace(me ? homeFor(me.roles) : "/login");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <main className="grid flex-1 lg:grid-cols-[1.1fr_1fr]">
      <section className="relative hidden overflow-hidden bg-[#040a18] p-12 text-white lg:flex lg:flex-col lg:justify-between">
        <NightApproach />
        <div className="relative flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl border border-white/15 bg-white/10 backdrop-blur-md">
            <Plane size={18} />
          </span>
          <span className="text-base font-medium tracking-wide text-white/90">CrewOps with Alexa+</span>
        </div>
        <div className="relative max-w-lg">
          <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-sky-200/70">Crew control</p>
          <h1 className="mt-4 text-[2.6rem] font-light leading-[1.12] tracking-tight text-white">
            Every disruption, answered in seconds. <span className="font-semibold">Every answer, proven.</span>
          </h1>
          <ul className="mt-10 flex flex-col gap-3.5">
            {POINTS.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-center gap-3 text-sm text-slate-200/90">
                <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-white/15 bg-white/5 backdrop-blur-sm">
                  <Icon size={13} />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>
        <p className="relative text-[11px] uppercase tracking-[0.22em] text-slate-300/60">
          Deccan Express · Konkan Skyways · Nilgiri Connect
        </p>
      </section>

      <section className="flex items-center justify-center px-4 py-12">
        <div className="animate-rise w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-brand text-white">
              <Plane size={20} />
            </span>
          </div>
          {current && (
            <div className="mb-6 rounded-2xl border border-sky-200 bg-accent-soft p-4">
              <p className="text-sm text-sky-900">
                This browser is signed in as <b>{current.user_id}</b> ({current.roles.join(", ").replace(/_/g, " ")}).
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button type="button" tone="accent" onClick={() => window.location.replace(homeFor(current.roles))}>
                  Continue <ArrowRight size={14} />
                </Button>
                <Button
                  type="button"
                  tone="secondary"
                  onClick={async () => {
                    await signOut().catch(() => undefined);
                    setCurrent(null);
                  }}
                >
                  <LogOut size={14} /> Sign in as someone else
                </Button>
              </div>
            </div>
          )}
          <h2 className="text-2xl font-semibold tracking-tight">Sign in</h2>
          <p className="mt-1 text-sm text-muted">Use the account your administrator created.</p>
          <form onSubmit={submit} className="mt-6 flex flex-col gap-4">
            <Field label="Username" name="username" autoComplete="username" required autoFocus />
            <Field label="Password" name="password" type="password" autoComplete="current-password" required />
            {error && <Notice tone="error">{error}</Notice>}
            <Button type="submit" tone="primary" disabled={busy} className="py-2.5">
              {busy ? <Spinner /> : <Lock size={14} />} Sign in
            </Button>
          </form>
          <p className="mt-6 text-xs text-muted">Each role sees only what it may: the server checks every action.</p>
        </div>
      </section>
    </main>
  );
}
