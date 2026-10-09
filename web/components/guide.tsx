"use client";

import { ChevronLeft, ChevronRight, HelpCircle, X } from "lucide-react";
import { useEffect, useState } from "react";

import type { Role } from "@/lib/roles";

import { useMe } from "./me";
import { Button, cx } from "./ui";

type Step = { title: string; text: string };

const COMMON: Step[] = [
  {
    title: "Alexa+ is in the corner",
    text:
      "The round button bottom-right opens Alexa+ on any page. Ask by voice or text: who covers a sick captain, " +
      "who is on reserve, what a rule says. Every figure comes from the live roster and is checked before it is shown.",
  },
  {
    title: "The (i) icons",
    text: "Hover or tap any (i) for what a term means: delay, reserve cover kept, minutes away, joint plan and more.",
  },
];

const BY_ROLE: Partial<Record<Role, Step[]>> = {
  controller: [
    {
      title: "Crew control",
      text:
        "Disruptions arrive in the feed as they are reported and are analysed within a minute. Each one is given to the " +
        "controller with the fewest open disruptions; 'on your desk' marks yours.",
    },
    {
      title: "Choose cover",
      text:
        "Open a disruption for its full record: the recommendation by cost, delay and reserve cover kept, every legal option " +
        "ranked, and everyone excluded with the rule they failed. Pick an option, or submit the recommendation.",
    },
    {
      title: "Four eyes",
      text:
        "Your plan goes to an approver, never to you. If they send it back, or the roster changes meanwhile, it returns to " +
        "you under 'Sent back to you'.",
    },
  ],
  approver: [
    {
      title: "Plans waiting for approval",
      text:
        "Each plan shows who replaces whom, the cost and any crew another desk also wants. Approving re-checks every rule " +
        "and changes the roster in the same step; sending back needs a reason the controller will see.",
    },
  ],
  reporter: [
    {
      title: "Record a disruption",
      text:
        "Pick the type (sick crew, delay, gate closure...), pick the crew, flight, gate or station from the lists and record " +
        "it. CrewOps works out what it breaks and who must be told, and assigns it to a controller.",
    },
    {
      title: "Your reports and the crew's",
      text:
        "'My reports' shows how each of yours stands: being checked, refused (and why), or handled. 'From crew' shows what " +
        "crew reported about themselves.",
    },
  ],
  crew: [
    {
      title: "Your week",
      text: "Your trips, duty hours against the limits, and any notice for you, such as a certificate about to lapse.",
    },
    {
      title: "Can't fly?",
      text:
        "Tap 'I can't fly' to report yourself unavailable. It reaches the reporting desk and crew control at once, and you " +
        "can follow it under 'My reports'.",
    },
  ],
  auditor: [
    { title: "Read everything", text: "Every disruption, plan, decision and audit trail, read-only." },
  ],
  admin: [
    {
      title: "Rules and alerts",
      text:
        "Tighten legality thresholds (never looser than regulation), read the activity log, and subscribe email addresses to " +
        "a role's or an operational team's alerts.",
    },
  ],
};

function stepsFor(roles: Role[]): Step[] {
  const own = roles.flatMap((r) => BY_ROLE[r] ?? (r === "super_admin" ? (BY_ROLE.admin ?? []) : []));
  return [...own, ...COMMON];
}

const SEEN = "crewops-guide-seen";

export function GuideButton() {
  const me = useMe();
  const [open, setOpen] = useState(false);
  const [i, setI] = useState(0);
  const steps = stepsFor(me.roles);

  useEffect(() => {
    // First visit by this person in this browser: open the guide once.
    let seen: string[] = [];
    try {
      seen = JSON.parse(localStorage.getItem(SEEN) ?? "[]") as string[];
    } catch {
      return; // storage unavailable (private window): the button still works
    }
    if (seen.includes(me.user_id)) return;
    const t = setTimeout(() => {
      setOpen(true);
      try {
        localStorage.setItem(SEEN, JSON.stringify([...seen, me.user_id]));
      } catch {
        // not remembered: it opens again next time, which is harmless
      }
    }, 400); // after the page has drawn
    return () => clearTimeout(t);
  }, [me.user_id]);

  useEffect(() => {
    if (!open) return;
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
      if (e.key === "ArrowRight") setI((n) => Math.min(n + 1, steps.length - 1));
      if (e.key === "ArrowLeft") setI((n) => Math.max(n - 1, 0));
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [open, steps.length]);

  const step = steps[i];
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setI(0);
          setOpen(true);
        }}
        className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm text-muted transition hover:bg-sunken hover:text-foreground"
        title="How CrewOps works"
      >
        <HelpCircle size={16} /> <span className="hidden sm:inline">Guide</span>
      </button>
      {open && step && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-slate-900/30 p-4 backdrop-blur-sm"
          onClick={() => setOpen(false)}
        >
          <div
            role="dialog"
            aria-modal
            aria-label="How CrewOps works"
            className="animate-rise w-full max-w-md rounded-3xl bg-white p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-[0.12em] text-accent">
                Step {i + 1} of {steps.length}
              </span>
              <button className="rounded-lg p-1 text-muted hover:bg-sunken" onClick={() => setOpen(false)} aria-label="Close">
                <X size={16} />
              </button>
            </div>
            <h2 key={step.title} className="animate-rise mt-2 text-xl font-semibold">
              {step.title}
            </h2>
            <p key={`${step.title}-text`} className="animate-rise mt-2 text-sm leading-relaxed text-slate-700">
              {step.text}
            </p>
            <div className="mt-5 flex items-center justify-between">
              <div className="flex gap-1.5">
                {steps.map((s, n) => (
                  <span key={s.title} className={cx("h-1.5 w-5 rounded-full", n === i ? "bg-accent" : "bg-slate-200")} />
                ))}
              </div>
              <div className="flex gap-2">
                <Button type="button" tone="ghost" disabled={i === 0} onClick={() => setI(i - 1)}>
                  <ChevronLeft size={14} /> Back
                </Button>
                {i < steps.length - 1 ? (
                  <Button type="button" tone="accent" onClick={() => setI(i + 1)}>
                    Next <ChevronRight size={14} />
                  </Button>
                ) : (
                  <Button type="button" tone="accent" onClick={() => setOpen(false)}>
                    Done
                  </Button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
