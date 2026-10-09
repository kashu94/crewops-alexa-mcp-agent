"use client";

import { ArrowRight, Lock, LogOut, Plane, ShieldCheck, Sparkles, Zap } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { RouteMap } from "@/components/login-art";
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
      <section className="relative hidden overflow-hidden bg-gradient-to-br from-[#0c2340] via-[#0f3a66] to-sky-600 p-12 text-white lg:flex lg:flex-col lg:justify-between">
        <div aria-hidden className="absolute -right-24 -top-24 h-96 w-96 rounded-full bg-sky-400/20 blur-3xl" />
        <div aria-hidden className="absolute -bottom-32 left-10 h-96 w-96 rounded-full bg-indigo-400/20 blur-3xl" />
        <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 w-[48%] opacity-90">
          <RouteMap />
        </div>
        <div className="relative flex items-center gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-xl bg-white/15 backdrop-blur">
            <Plane size={20} />
          </span>
          <span className="text-lg font-semibold">CrewOps with Alexa+</span>
        </div>
        <div className="relative">
          <h1 className="max-w-md text-4xl font-semibold leading-tight tracking-tight">
            Crew control that answers in seconds, and proves every answer.
          </h1>
          <ul className="mt-8 flex flex-col gap-4">
            {POINTS.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-center gap-3 text-sky-100">
                <span className="grid h-8 w-8 place-items-center rounded-lg bg-white/10">
                  <Icon size={16} />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>
        <p className="relative font-mono text-xs text-sky-200/80">BLR · DEL · BOM · HYD · CCU · GOI</p>
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
