"""One question in, one verified answer out.

    route -> check what the question states -> planned tool calls -> follow-ups
          -> (model draft, when one is plugged in) -> verify -> fall back to the template
          -> redact personal data

The planned calls run with the caller's own role checks, so a crew member's question can
only ever reach their own records. A model draft is accepted only if the verifier traces
every claim in it to this turn's tool results; otherwise the deterministic rendering of
those results is the answer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from crewops.agent.entities import Vocabulary
from crewops.agent.intents import Intent
from crewops.agent.pii import redact, redact_text
from crewops.agent.planner import MISSING, ToolCall, false_premise, followup_calls, seed_calls
from crewops.agent.render import render
from crewops.agent.router import Route, Router
from crewops.agent.verifier import Verification, verify
from crewops.tools import Caller, CrewOpsTools, TraceEntry, allowed_tools, dispatch
from crewops.tools.errors import AMBIGUOUS_QUERY, NEEDS_CONFIRMATION

log = logging.getLogger(__name__)

MAX_QUESTION = 1000
FIRST_PERSON_RE = re.compile(r"\b(I|me|my|mine)\b", re.I)
SCOPE = (
    "I can help with crew control: rosters and reserves, duty hours, legality, cover for "
    "a sick call or delay, disruption impact, gates and callout messages. What would you "
    "like to check?"
)
MAX_FOLLOWUP_ROUNDS = 3

ToolRunner = Callable[[str, dict[str, Any]], TraceEntry]
"""(tool name, arguments) -> the recorded call, run for the signed-in caller."""
Drafter = Callable[[str, Route, list[TraceEntry], ToolRunner], str]
"""(question, route, trace so far, run a tool) -> draft answer. The model advisor plugs in
here. Its only way to a tool is the runner it is given, so every call it makes goes through
the caller's role checks and lands in the trace the draft is verified against."""


@dataclass(slots=True)
class Answer:
    question: str
    text: str
    route: Route | None
    trace: list[TraceEntry] = field(default_factory=list)
    verification: Verification | None = None
    source: str = "template"
    """template, model, clarify (a question back) or refused."""
    awaiting: str | None = None
    """confirmation (an id, name or premise to confirm) or detail (something missing)."""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """What leaves the process: personal data removed from everything, at any depth."""
        return redact(
            {
                "question": self.question,
                "text": self.text,
                "source": self.source,
                "awaiting": self.awaiting,
                "route": self.route.to_dict() if self.route else None,
                "verified": self.verification.ok if self.verification else False,
                "verification": self.verification.summary() if self.verification else None,
                "tools": [e.to_dict() | {"args": e.args, "ms": e.ms} for e in self.trace],
                "notes": list(self.notes),
            }
        )


class Pipeline:
    def __init__(
        self,
        tools: CrewOpsTools,
        router: Router,
        *,
        drafter: Drafter | None = None,
    ) -> None:
        self.tools = tools
        self.router = router
        self.vocab: Vocabulary = router.vocab
        self.drafter = drafter

    @classmethod
    def offline(cls, tools: CrewOpsTools, examples: Mapping[str, str]) -> Pipeline:
        """Rules and templates only: no embeddings, no model calls."""
        return cls(tools, Router(Vocabulary.from_world(tools.world), examples))

    def answer(self, question: str, caller: Caller) -> Answer:
        question = (question or "").strip()
        if not question:
            return Answer(
                question, "What would you like to know?", None, source="clarify", awaiting="detail"
            )
        if len(question) > MAX_QUESTION:
            return Answer(
                question[:80],
                f"Please ask in under {MAX_QUESTION} characters.",
                None,
                source="refused",
            )

        route = self.router.route(question)
        if (
            caller.crew_id
            and not route.entities.crew_ids
            and not route.entities.names
            and FIRST_PERSON_RE.search(question)
        ):
            # "my duty hours", "am I legal for P-2291": the signed-in crew member themselves.
            route.entities.crew_ids.append(caller.crew_id)
            route.notes.append(f"'I' and 'my' read as the signed-in crew member, {caller.crew_id}")
        allowed = allowed_tools(caller)

        def may_see(crew_id: str) -> bool:
            return "lookup" in allowed or crew_id == caller.crew_id

        if premise := false_premise(question, route.entities, self.tools, self.vocab, may_see):
            return Answer(question, premise, route, source="clarify", awaiting="confirmation")

        notes: list[str] = []
        trace: list[TraceEntry] = []
        planned = seed_calls(route, question, self.vocab, self.tools)
        trace += self._run(planned, caller, allowed, notes)
        for _ in range(MAX_FOLLOWUP_ROUNDS):
            more = followup_calls(route, trace)
            if not more:
                break
            trace += self._run(more, caller, allowed, notes)

        if self.drafter is not None and allowed:

            def run(name: str, args: dict[str, Any]) -> TraceEntry:
                entry = dispatch(self.tools, caller, name, args)
                trace.append(entry)
                return entry

            try:
                draft = self.drafter(question, route, list(trace), run)
                checked = verify(draft, trace)
                if checked.ok:
                    return self._finish(question, draft, route, trace, checked, "model", notes)
                # The unsourced values go to the log only: they may be anything the model wrote.
                log.info("model draft rejected: %s", checked.summary())
                kinds = sorted({c.kind for c in checked.unsupported})
                notes.append(
                    f"model draft rejected: {len(checked.unsupported)} unsourced claim(s) "
                    f"({', '.join(kinds)})"
                )
            except Exception as exc:  # a model failure must never cost the user an answer
                log.exception("drafter failed")
                notes.append(f"model unavailable ({type(exc).__name__}); template used")

        if not trace:
            return Answer(
                question,
                self._nothing_ran(route, planned, allowed),
                route,
                source="clarify",
                awaiting="detail",
                notes=notes,
            )

        text = render(trace)
        checked = verify(text, trace)
        if not checked.ok:  # a template bug: still say only what the tools said, and log it
            log.error("template failed verification: %s", checked.summary())
            notes.append(checked.summary())
        return self._finish(question, text, route, trace, checked, "template", notes)

    # ------------------------------------------------------------------

    def _run(
        self, calls: list[ToolCall], caller: Caller, allowed: frozenset[str], notes: list[str]
    ) -> list[TraceEntry]:
        out = []
        for call in calls:
            if call.name not in allowed:
                notes.append(f"skipped {call.name}: not available to your role")
                continue
            out.append(dispatch(self.tools, caller, call.name, call.args))
        return out

    def _finish(
        self,
        question: str,
        text: str,
        route: Route,
        trace: list[TraceEntry],
        checked: Verification,
        source: str,
        notes: list[str],
    ) -> Answer:
        codes = {e.error_code for e in trace if not e.ok}
        awaiting = None
        if NEEDS_CONFIRMATION in codes:
            awaiting = "confirmation"
        elif AMBIGUOUS_QUERY in codes:
            awaiting = "detail"
        return Answer(question, redact_text(text), route, trace, checked, source, awaiting, notes)

    def _nothing_ran(self, route: Route, planned: list[ToolCall], allowed: frozenset[str]) -> str:
        """Why nothing was checked: never an empty answer, which would sound like "all clear"."""
        if planned and not allowed & {c.name for c in planned}:
            return "Your role doesn't give access to that information."
        ents = route.entities
        if (
            route.decided_by == "default"
            and not planned
            and not (ents.crew_ids or ents.pairing_ids or ents.flight_ids)
        ):
            return SCOPE
        if route.intent in (Intent.FIND_REPLACEMENT, Intent.RANK_OPTIONS) and (
            (ents.pairing_ids or ents.flight_ids or ents.flight_nos) and not ents.roles
        ):
            where = (ents.pairing_ids or ents.flight_ids or ents.flight_nos)[0]
            return (
                f"Which seat needs cover on {where}: Captain, First Officer, Senior Cabin Crew "
                "or Cabin Crew? Or tell me who is out."
            )
        what = route.intent.value.replace("_", " ").lower()
        text = f"I read this as {what}, but couldn't run it."
        if needed := MISSING.get(route.intent):
            text += f" I need {needed}."
        if route.intent is Intent.LOOKUP_CONTROLLERS:
            text = "Ask about the controller desks, or how many open disruptions each has."
        return text + " Nothing was checked, so this doesn't mean all is clear."
