"use client";

import { BadgeCheck, Mic, RotateCcw, Send, Sparkles, Volume2, VolumeX } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { speak, tool } from "@/lib/client";
import { makeRecognition, type Recognition } from "@/lib/speech";

import { cx } from "./ui";

type Answer = {
  answer: string;
  verified: boolean;
  source: string;
  awaiting: string | null;
  intent: string | null;
  notes: string[];
  model_calls: number;
};

type Turn = { id: number; question: string; answer?: Answer; error?: string };

const SOURCE: Record<string, string> = {
  template: "From the engine",
  explained: "Reworded, every fact kept",
  model: "Model answer, verified",
  clarify: "Question back",
  refused: "Refused",
};

function newConversation() {
  return `web-${crypto.randomUUID()}`;
}

/** Fourteen bars that breathe while CrewOps listens or thinks. */
function Waveform({ active, tone }: { active: boolean; tone: "listen" | "think" }) {
  return (
    <div className="flex h-10 items-center justify-center gap-[5px]" aria-hidden>
      {Array.from({ length: 14 }).map((_, i) => (
        <span
          key={i}
          className={cx(
            "h-full w-[4px] rounded-full transition-colors",
            active ? "wave-bar" : "scale-y-[0.18]",
            tone === "listen" ? "bg-sky-400" : "bg-indigo-400",
          )}
          style={{ animationDelay: `${(i % 7) * 90}ms`, animationDuration: `${780 + (i % 4) * 120}ms` }}
        />
      ))}
    </div>
  );
}

export function AlexaModule({
  suggestions,
  className,
}: {
  suggestions: string[];
  className?: string;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [typed, setTyped] = useState("");
  const [heard, setHeard] = useState("");
  const [listening, setListening] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [voice, setVoice] = useState(true);
  const [conversation, setConversation] = useState(newConversation);
  const conversationRef = useRef(conversation);
  const recognition = useRef<Recognition | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const clipUrl = useRef<string | null>(null);
  const [canListen, setCanListen] = useState(false);

  useEffect(() => {
    conversationRef.current = conversation;
  }, [conversation]);

  useEffect(() => {
    recognition.current = makeRecognition();
    setCanListen(recognition.current !== null);
    return () => {
      audio.current?.pause();
      if (clipUrl.current) URL.revokeObjectURL(clipUrl.current);
    };
  }, []);

  async function ask(question: string) {
    const text = question.trim();
    if (!text || thinking) return;
    const id = Date.now();
    setTurns((t) => [...t, { id, question: text }]);
    setThinking(true);
    try {
      const answer = await tool<Answer>("ask", { question: text, conversation_id: conversationRef.current });
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, answer } : x)));
      if (voice) {
        const clip = await speak(answer.answer);
        if (clip) {
          audio.current?.pause();
          if (clipUrl.current) URL.revokeObjectURL(clipUrl.current);
          clipUrl.current = URL.createObjectURL(clip);
          audio.current = new Audio(clipUrl.current);
          audio.current.play().catch(() => undefined);
        }
      }
    } catch (err) {
      const error = err instanceof Error ? err.message : "Something went wrong.";
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, error } : x)));
    } finally {
      setThinking(false);
    }
  }

  function listen() {
    const r = recognition.current;
    if (!r || listening) return;
    audio.current?.pause();
    let finalText = "";
    r.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interim += result[0].transcript;
      }
      setHeard(finalText || interim);
    };
    r.onerror = () => setListening(false);
    r.onend = () => {
      setListening(false);
      setHeard("");
      if (finalText) ask(finalText);
    };
    setListening(true);
    r.start();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    ask(typed);
    setTyped("");
  }

  const last = [...turns].reverse().find((t) => t.answer || t.error);
  const busy = listening || thinking;

  return (
    <div className={cx("flex min-h-0 flex-col gap-4", className)}>
      {/* The speaker */}
      <div className="relative overflow-hidden rounded-2xl border border-sky-100 bg-gradient-to-b from-white via-sky-50/60 to-white p-5">
        <div className="flex items-start gap-4">
          <button
            onClick={listen}
            disabled={!canListen || thinking}
            title={canListen ? "Talk to CrewOps" : "Voice input needs Chrome or Edge; type below instead"}
            className="group relative grid h-20 w-20 shrink-0 place-items-center rounded-full disabled:cursor-not-allowed"
          >
            {busy && (
              <>
                <span className="animate-ring absolute inset-0 rounded-full border-2 border-sky-300" />
                <span className="animate-ring absolute inset-0 rounded-full border-2 border-sky-300 [animation-delay:800ms]" />
              </>
            )}
            <span
              className={cx(
                "relative grid h-20 w-20 place-items-center rounded-full border-4 shadow-lg transition",
                listening
                  ? "border-sky-400 bg-sky-500 text-white"
                  : thinking
                    ? "border-indigo-300 bg-indigo-500 text-white"
                    : "border-sky-200 bg-white text-sky-600 group-hover:border-sky-400 group-hover:shadow-sky-200",
                !canListen && "opacity-60",
              )}
            >
              {thinking ? <Sparkles size={26} /> : <Mic size={26} />}
            </span>
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-semibold uppercase tracking-[0.12em] text-sky-700">
                {listening ? "Listening" : thinking ? "Checking the roster and the rules" : "Alexa+ · CrewOps"}
              </span>
              <div className="flex items-center gap-1">
                <button
                  className="rounded-md p-1.5 text-muted transition hover:bg-white hover:text-foreground"
                  title={voice ? "Spoken answers on" : "Spoken answers off"}
                  onClick={() => setVoice((v) => !v)}
                >
                  {voice ? <Volume2 size={15} /> : <VolumeX size={15} />}
                </button>
                <button
                  className="rounded-md p-1.5 text-muted transition hover:bg-white hover:text-foreground"
                  title="New conversation"
                  onClick={() => {
                    setTurns([]);
                    setConversation(newConversation());
                  }}
                >
                  <RotateCcw size={15} />
                </button>
              </div>
            </div>
            <Waveform active={busy} tone={listening ? "listen" : "think"} />
            <div key={last?.id ?? "idle"} className="animate-rise min-h-[3.5rem]">
              {listening ? (
                <p className="text-lg text-sky-700">{heard || "Go ahead…"}</p>
              ) : last?.answer ? (
                <p className="line-clamp-5 text-[15px] leading-relaxed text-slate-800" title={last.answer.answer}>
                  {last.answer.answer}
                </p>
              ) : last?.error ? (
                <p className="text-sm text-critical">{last.error}</p>
              ) : (
                <p className="text-sm text-muted">Tap the microphone and ask, or pick a question below.</p>
              )}
              {last?.answer?.awaiting && !listening && (
                <p className="mt-2 text-xs font-medium text-sky-700">Just answer — I remember what you asked.</p>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Suggested questions */}
      <div className="flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button
            key={s}
            onClick={() => ask(s)}
            disabled={thinking}
            className="lift rounded-full border border-line bg-surface px-3 py-1.5 text-left text-xs text-slate-700 disabled:opacity-50"
          >
            “{s}”
          </button>
        ))}
      </div>

      <form onSubmit={submit} className="flex gap-2">
        <input
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder="Ask CrewOps anything about the roster…"
          maxLength={1000}
          className="min-w-0 flex-1 rounded-xl border border-line bg-surface px-3.5 py-2.5 text-sm outline-none transition focus:border-accent focus:ring-4 focus:ring-sky-100"
        />
        <button
          disabled={thinking || !typed.trim()}
          className="grid h-10 w-10 place-items-center rounded-xl bg-accent text-white shadow-sm transition hover:bg-sky-700 disabled:opacity-40"
          title="Ask"
        >
          <Send size={16} />
        </button>
      </form>

      {/* Voice log */}
      <ol className="flex min-h-0 flex-col gap-2 overflow-y-auto">
        {[...turns].reverse().map((t) => (
          <li key={t.id} className="animate-rise rounded-xl border border-line bg-sunken/60 p-3 text-sm">
            <p className="font-medium text-slate-800">{t.question}</p>
            {t.answer && (
              <>
                <p className="mt-1.5 whitespace-pre-line text-slate-700">{t.answer.answer}</p>
                <div className="mt-2 flex flex-wrap gap-1.5 text-[11px]">
                  <span
                    className={cx(
                      "inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium",
                      t.answer.verified ? "bg-ok-soft text-emerald-700" : "bg-slate-100 text-slate-600",
                    )}
                  >
                    {t.answer.verified && <BadgeCheck size={12} />}
                    {t.answer.verified ? "Verified" : "No facts stated"}
                  </span>
                  <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">
                    {SOURCE[t.answer.source] ?? t.answer.source}
                  </span>
                  {t.answer.intent && (
                    <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">
                      {t.answer.intent.replace(/_/g, " ").toLowerCase()}
                    </span>
                  )}
                </div>
                {t.answer.notes.length > 0 && (
                  <details className="mt-1.5 text-xs text-muted">
                    <summary className="cursor-pointer">How this was answered</summary>
                    <ul className="mt-1 list-disc pl-5">
                      {t.answer.notes.map((n) => (
                        <li key={n}>{n}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </>
            )}
            {t.error && <p className="mt-1.5 text-critical">{t.error}</p>}
          </li>
        ))}
      </ol>
    </div>
  );
}
