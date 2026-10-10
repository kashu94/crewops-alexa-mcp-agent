"""What the MCP server can do for a signed-in caller, independent of the MCP plumbing.

Every operation takes the ``Caller`` made from the request's verified token, never anything
the user typed about themselves. Questions go through the full answer pipeline (rules,
tools, verifier, models); plans through ``crewops.plans`` with its four-eyes rules; reports
onto the ingest queue with the sender's identity set here; the deterministic tools
through the same role-checked dispatch the advisor uses; and alert addresses through
``crewops.notify.subscriptions``, for admins only.

A question asked with a conversation id is answered with that conversation's previous
turn (``agent.context``), read from and written back to AgentCore Memory.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

from crewops.agent.context import Turn
from crewops.agent.memory import ConversationMemory, valid_conversation
from crewops.analysis.runner import UNAVAILABLE_STATUSES
from crewops.domain import DataError, Report, Role, authorize
from crewops.domain.desks import owning_desks
from crewops.domain.reports import OPERATIONAL_REPORTERS, crew_out_of
from crewops.mcp import views
from crewops.notify.subscriptions import AlertError, AlertSubscriptions
from crewops.plans.service import PlanError, Plans
from crewops.plans.service import seat_key as _seat_key
from crewops.plans.service import summary as plan_summary
from crewops.store import plans as plan_store
from crewops.store import rules as rule_store
from crewops.store.items import from_dynamo
from crewops.store.reports import (
    add_suggestion,
    disruptions_with_status,
    filed_by,
    filed_recently,
    get_disruption,
    record_filed,
    rejections_for,
    suggestions_for,
)
from crewops.tools import TOOL_SCHEMAS, Caller, allowed_tools, dispatch
from crewops.tools.errors import INTERNAL

CACHE_SECONDS = 60.0
"""How long one loaded operation answers questions before it is read again. Plan actions
on this server reload it at once; changes made elsewhere show within this time."""
READERS = frozenset({Role.CONTROLLER, Role.APPROVER, Role.AUDITOR, Role.ADMIN, Role.SUPER_ADMIN})
ADMINS = frozenset({Role.ADMIN, Role.SUPER_ADMIN})
FEED_STATUSES = ("REPORTED", "OPTIONS_READY", "SUBMITTED", "COMMITTED", "CLOSED")
OUTCOME = {
    "REPORTED": "Received; crew control is analysing it.",
    "OPTIONS_READY": "Crew control is arranging cover.",
    "SUBMITTED": "A plan is waiting for approval.",
    "COMMITTED": "Cover has been arranged.",
    "CLOSED": "Resolved by crew control.",
}
RESOLVED = ("COMMITTED", "CLOSED")


def _remark(table: Any, disruption_id: str, status: str) -> str | None:
    """What crew control said when it resolved or closed the disruption."""
    if status != "CLOSED":
        return None
    closed = [
        e for e in plan_store.audit_trail(table, disruption_id) if e.get("action") == "CLOSED"
    ]
    return closed[-1].get("reason") if closed else None


def _airline(tools: Any, code: str) -> dict[str, str]:
    a = next((a for a in tools.seed.airlines if a.code == code), None)
    return {"code": code, "name": a.name if a else code, "color": a.color if a else "#64748b"}


def _crew_name(tools: Any, crew_id: str | None) -> str | None:
    crew = tools.world.crew.get(crew_id) if crew_id else None
    return crew.name if crew else None


def _outcome(status: str, handling: Mapping[str, Any]) -> str:
    """How a disruption stands, for whoever reported it."""
    if status == "OPTIONS_READY" and handling.get("kind") == "internal":
        return "Handled; crew control will mark it resolved."
    if status == "OPTIONS_READY" and handling.get("kind") == "decision":
        return "Crew control is deciding what to do with operations."
    return OUTCOME.get(status, status)


def _analysis_view(tools: Any, analysis: Mapping[str, Any]) -> dict[str, Any]:
    """How a disruption is handled beyond its seats: the one-line handling, who is told,
    the flights it moves or delays and the gates that change."""
    late = {leg["flight_id"]: leg["minutes"] for leg in analysis.get("late_departures") or []}
    flights = [*analysis.get("affected_flights", []), *late]
    return {
        "handling": analysis.get("handling") or None,
        "airlines": [_airline(tools, code) for code in analysis.get("airlines") or []],
        "notices": analysis.get("notices") or [],
        "stands": analysis.get("stands") or [],
        "next_at_gate": analysis.get("next_at_gate") or [],
        "flights": views.flight_rows(tools, flights, late),
        "delays": analysis.get("delays") or [],
        "notes": analysis.get("notes") or [],
    }


def _seat(s: Mapping[str, Any]) -> dict[str, Any]:
    """A seat as the console shows it: who is out, the impact and the ranked options."""
    keep = (
        "pairing_id",
        "role",
        "crew_out",
        "status",
        "note",
        "legal_options",
        "excluded",
        "cancellation_cost_inr",
        "cancellation_multiple",
        "delay_hours",
        "delay_date",
    )
    out = {k: s.get(k) for k in keep}
    out["impact"] = s.get("impact", {})
    out["options"] = [
        {
            k: o.get(k)
            for k in (
                "rank",
                "action",
                "strategy",
                "crew_id",
                "name",
                "crew_rank",
                "cost_inr",
                "cost_breakdown",
                "delay_hours",
            )
        }
        for o in s.get("options", [])
    ]
    return out


class ApiError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CrewOpsApi:
    def __init__(
        self,
        table: Any,
        live_factory: Callable[[], Any],
        send: Callable[[dict[str, Any]], None],
        *,
        plans: Plans | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cache_seconds: float = CACHE_SECONDS,
        memory: ConversationMemory | None = None,
        alerts: AlertSubscriptions | None = None,
    ) -> None:
        """``live_factory`` builds the answer pipeline over the current operation
        (``agent.service.build``); ``send`` puts a report on the ingest queue. Without
        ``memory`` every question stands on its own; without ``alerts`` the alert tools
        say alerts are not set up."""
        self.table = table
        self.live_factory = live_factory
        self.send = send
        self.plans = plans or Plans(table)
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._live: Any = None
        self._loaded = 0.0
        self._loading = threading.Lock()
        # The pipeline keeps per-question state (model usage, the advisor's last run), so
        # one question at a time uses it; tool calls run on other threads meanwhile.
        self._asking = threading.Lock()
        self.memory = memory
        self.alerts = alerts
        # A turn is remembered after its answer is returned, never delaying it.
        self._remembering = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory")

    # ------------------------------------------------------------------
    # Questions and tools
    # ------------------------------------------------------------------

    def live(self) -> Any:
        with self._loading:
            if self._live is None or time.monotonic() - self._loaded > self.cache_seconds:
                self._live = self.live_factory()
                self._loaded = time.monotonic()
            return self._live

    def invalidate(self) -> None:
        with self._loading:
            self._live = None

    def ask(self, caller: Caller, question: str, conversation_id: str = "") -> dict[str, Any]:
        """``conversation_id``: the client's id for this conversation (letters, digits, - and
        _). With one, the previous turn may fill in what this question leaves out."""
        if conversation_id and not valid_conversation(conversation_id):
            raise ApiError(
                "INVALID",
                "conversation_id: up to 100 letters, digits, - or _, not starting with - or _",
            )
        remembered = self.memory is not None and bool(conversation_id)
        previous = None
        if remembered:
            previous = self.memory.last_turn(caller.user_id, conversation_id, self.clock())
        live = self.live()
        with self._asking:
            answer, usage = live.ask(question, caller, previous)
        record = answer.to_dict()
        if remembered:
            turn = Turn.of(answer, self.clock())
            self._remembering.submit(
                self.memory.remember, caller.user_id, conversation_id, turn, record["text"]
            )
        return {
            "answer": record["text"],
            "verified": record["verified"],
            "source": record["source"],
            "awaiting": record["awaiting"],
            "intent": (record["route"] or {}).get("intent"),
            "notes": record["notes"],
            "model_calls": usage.get("calls", 0),
        }

    def whoami(self, caller: Caller) -> dict[str, Any]:
        """Who the token says is signed in, as the server sees them."""
        return {
            "user_id": caller.user_id,
            "roles": sorted(r.value for r in caller.roles),
            "crew_id": caller.crew_id,
            "desk": caller.desk,
        }

    def list_tools(self, caller: Caller) -> list[dict[str, Any]]:
        allowed = allowed_tools(caller)
        return [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in TOOL_SCHEMAS
            if t["name"] in allowed
        ]

    def run_tool(self, caller: Caller, name: str, arguments: Mapping[str, Any] | None) -> dict:
        entry = dispatch(self.live().pipeline.tools, caller, name, dict(arguments or {}))
        out = entry.to_dict()
        if entry.error_code == INTERNAL:  # logged by dispatch; the caller sees no internals
            out["error"] = "the tool failed unexpectedly; it has been logged"
        return out

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def report(
        self,
        caller: Caller,
        report_type: str,
        details: Mapping[str, Any],
        narrative: str = "",
    ) -> dict[str, Any]:
        """Check a report now (so the reporter hears about a mistake at once), then queue it.
        Who sent it comes from the token, never from the request."""
        doc = {
            "report_id": f"r-{uuid.uuid4().hex[:12]}",
            "type": report_type,
            "reported_by": {
                "user_id": caller.user_id,
                "roles": sorted(r.value for r in caller.roles),
                "crew_id": caller.crew_id,
            },
            "reported_utc": self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "details": dict(details),
            "narrative": narrative,
        }
        try:
            report = Report.from_dict(doc)
        except DataError as exc:
            raise ApiError("INVALID", str(exc)) from None
        if reason := authorize(report):
            raise ApiError("FORBIDDEN", reason)
        if overlap := self._already_out(report.details, report.report_type.value, report.report_id):
            raise ApiError("DUPLICATE", overlap)
        self.send(report.to_dict())
        record_filed(self.table, report)
        return {
            "report_id": report.report_id,
            "status": "queued",
            "note": "It is checked against the roster and analysed within a minute.",
        }

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def queue(self, caller: Caller) -> list[dict[str, Any]]:
        return self.plans.queue(caller)

    def disruptions(self, caller: Caller, limit: int = 40) -> list[dict[str, Any]]:
        """Recent disruptions in every status, newest report first: the desk's feed."""
        if not caller.roles & READERS:
            raise ApiError("FORBIDDEN", "your role may not read disruptions")
        items = [i for status in FEED_STATUSES for i in disruptions_with_status(self.table, status)]
        tools = self.live().pipeline.tools
        out = []
        for item in items:
            report = from_dynamo(item.get("report") or {})
            analysis = from_dynamo(item.get("analysis") or {})
            out.append(
                {
                    "disruption_id": item["disruption_id"],
                    "status": item["status"],
                    "report_type": report.get("type"),
                    "reported_utc": report.get("reported_utc"),
                    "reported_by": (report.get("reported_by") or {}).get("user_id"),
                    "details": report.get("details", {}),
                    "summary": analysis.get("summary", ""),
                    "handling": (analysis.get("handling") or {}).get("kind"),
                    "airlines": analysis.get("airlines") or [],
                    "desks": [
                        d["desk_id"]
                        for d in views.desks_of_disruption(
                            tools,
                            analysis,
                            [seat["pairing_id"] for seat in analysis.get("seats", [])],
                        )
                    ],
                }
            )
        out.sort(key=lambda d: d.get("reported_utc") or "", reverse=True)
        return out[: max(1, min(int(limit), 100))]

    def show(self, caller: Caller, disruption_id: str) -> dict[str, Any]:
        item = get_disruption(self.table, disruption_id)
        report = from_dynamo((item or {}).get("report") or {})
        own = (report.get("reported_by") or {}).get("user_id") == caller.user_id
        # Someone who may not read it is told the same as when it does not exist, so ids
        # cannot be probed.
        if item is None or not (caller.roles & READERS or own):
            raise ApiError("NOT_FOUND", f"no disruption {disruption_id} that you may read")
        analysis = from_dynamo(item.get("analysis") or {})
        handling = analysis.get("handling") or {}
        if not caller.roles & READERS:
            # The person who filed it hears how it stands, nothing about other crew,
            # replacements, costs or other desks.
            return {
                "disruption_id": disruption_id,
                "status": item["status"],
                "outcome": _outcome(item["status"], handling),
                "report_type": report.get("type"),
                "reported_utc": report.get("reported_utc"),
                "reported_by": caller.user_id,
                "details": report.get("details", {}),
                # Handled outside crew control (gates moved, a delay absorbed): what was
                # done is about flights and gates, never about crew, so they may read it.
                "handled": handling.get("text") if handling.get("kind") == "internal" else None,
                # Crew control's remark on resolving it, which is about what was done.
                "remark": _remark(self.table, disruption_id, item["status"]),
            }
        tools = self.live().pipeline.tools
        seats = [_seat(s) for s in analysis.get("seats", [])]
        for seat in seats:
            seat["context"] = views.seat_context(tools, seat)
            seat["live"] = views.live_options(tools, seat)
            seat["reserves"] = views.reserve_capacity(
                tools, seat["role"], seat["impact"].get("affected_dates") or []
            )
        desks = views.desks_of_disruption(tools, analysis, [s["pairing_id"] for s in seats])
        joint = analysis.get("joint_plan")
        return {
            "disruption_id": disruption_id,
            "status": item["status"],
            "summary": analysis.get("summary", ""),
            "report_type": report.get("type"),
            "reported_utc": report.get("reported_utc"),
            "reported_by": (report.get("reported_by") or {}).get("user_id"),
            "details": report.get("details", {}),
            "desks": desks,
            # Touching no aircraft at all (a closed gate nobody was using): any desk's.
            "yours": bool(caller.desk)
            and Role.CONTROLLER in caller.roles
            and (not desks or any(d["desk_id"] == caller.desk for d in desks)),
            **_analysis_view(tools, analysis),
            # The ranked options each seat had when it was analysed; approval re-checks.
            "seats": seats,
            "joint_plan": joint if joint and joint.get("assignments") else None,
            # Who else wants these crew: other open disruptions and submitted plans.
            "contention": views.contention(
                tools,
                self.table,
                disruption_id,
                [
                    (
                        {
                            o["crew_id"]
                            for o in ((s["live"] or {}).get("options") or s["options"])
                            if o.get("crew_id")
                        },
                        set(s["impact"].get("affected_dates") or []),
                    )
                    for s in seats
                ],
            ),
            # Crew among the options who have since been reported unavailable on these dates:
            # shown struck out, with when, rather than silently dropped.
            "unavailable_now": self._unavailable_now(disruption_id, seats),
            # Other desks' suggestions of cover: advice for the owning desk, which alone
            # may act on it; other readers see it as part of the record.
            "suggestions": [
                {k: s.get(k) for k in ("by", "desk", "seat", "crew_id", "note", "at")}
                | {"name": _crew_name(tools, s.get("crew_id"))}
                for s in suggestions_for(self.table, disruption_id)
            ],
            "plans": [
                {"status": p["status"], **plan_summary(p)}
                for p in plan_store.plans_for(self.table, disruption_id)
            ],
            "audit": [
                {k: e.get(k) for k in ("at", "action", "actor", "plan_id", "reason") if e.get(k)}
                for e in plan_store.audit_trail(self.table, disruption_id)
            ],
        }

    def suggest(
        self, caller: Caller, disruption_id: str, seat: str, crew_id: str, note: str = ""
    ) -> dict[str, Any]:
        """A controller on another desk suggests cover for a seat. It is shown to the owning
        desk; it plans nothing and changes nothing."""
        if Role.CONTROLLER not in caller.roles or not caller.desk:
            raise ApiError("FORBIDDEN", "only a controller with a desk can suggest cover")
        item = get_disruption(self.table, disruption_id)
        if item is None:
            raise ApiError("NOT_FOUND", f"no disruption {disruption_id}")
        if item["status"] != "OPTIONS_READY":
            raise ApiError("WRONG_STATE", f"{disruption_id} is {item['status']}")
        analysis = from_dynamo(item.get("analysis") or {})
        tools = self.live().pipeline.tools
        owners = views.desks_of_disruption(tools, analysis)
        if any(d["desk_id"] == caller.desk for d in owners):
            raise ApiError(
                "INVALID", "this is your own desk's disruption: choose the option instead"
            )
        if seat not in {_seat_key(s) for s in analysis.get("seats", [])}:
            raise ApiError("INVALID", f"no such seat: {seat}")
        if crew_id not in tools.world.crew:
            raise ApiError("INVALID", f"no crew member {crew_id}")
        note = (note or "").strip()[:300]
        add_suggestion(
            self.table,
            disruption_id,
            caller.user_id,
            caller.desk,
            seat,
            crew_id,
            note,
            self.clock(),
        )
        return {
            "status": "suggested",
            "disruption_id": disruption_id,
            "seat": seat,
            "crew_id": crew_id,
        }

    # ------------------------------------------------------------------
    # The crew member's own roster
    # ------------------------------------------------------------------

    def my_roster(self, caller: Caller) -> dict[str, Any]:
        """The signed-in crew member's pairings, duty days and duty clock. Only ever their
        own: the crew id comes from the verified profile, not from the request."""
        if not caller.crew_id:
            raise ApiError("FORBIDDEN", "only crew have a roster")
        tools = self.live().pipeline.tools
        mine = tools.lookup("pairing_crew", {"crew_id": caller.crew_id})["rows"]
        pairings = []
        for row in mine:
            (p,) = tools.lookup("pairings", {"pairing_id": row["pairing_id"]})["rows"]
            pairings.append(
                {
                    "pairing_id": p["pairing_id"],
                    "aircraft": p["aircraft"],
                    "role": row["role"],
                    "days": p["days"],
                }
            )
        return {
            "crew_id": caller.crew_id,
            "name": mine[0]["name"] if mine else None,
            "airline": tools.world.airline_of_crew.get(caller.crew_id),
            "notices": self._crew_notices(caller.crew_id),
            "reported_unavailable": self._reported_out(caller.crew_id),
            "clock": tools.duty_clock(caller.crew_id),
            "pairings": sorted(pairings, key=lambda p: p["days"][0]["date"] if p["days"] else ""),
        }

    def _unavailable_now(
        self, disruption_id: str, seats: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        wanted: dict[str, set[str]] = {}
        for seat in seats:
            dates = set(seat["impact"].get("affected_dates") or [])
            options = [*seat.get("options", []), *((seat.get("live") or {}).get("options") or [])]
            for o in options:
                if o.get("crew_id"):
                    wanted.setdefault(o["crew_id"], set()).update(dates)
        if not wanted:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for status in UNAVAILABLE_STATUSES:
            for item in disruptions_with_status(self.table, status):
                if item["disruption_id"] == disruption_id:
                    continue
                report = from_dynamo(item.get("report") or {})
                kind, d = report.get("type", ""), report.get("details") or {}
                if kind not in ("crew_unavailable", "multiple_crew_unavailable"):
                    continue
                days = {
                    (date.fromisoformat(d["from_date"]) + timedelta(days=i)).isoformat()
                    for i in range(
                        (date.fromisoformat(d["to_date"]) - date.fromisoformat(d["from_date"])).days
                        + 1
                    )
                }
                for crew_id in crew_out_of(kind, d):
                    if crew_id in wanted and wanted[crew_id] & days:
                        out[crew_id] = {
                            "disruption_id": item["disruption_id"],
                            "reported_utc": report.get("reported_utc"),
                            "reason": d.get("reason"),
                            "from_date": d["from_date"],
                            "to_date": d["to_date"],
                        }
        return out

    def _reported_out(self, crew_id: str) -> list[dict[str, Any]]:
        """The periods this crew member is already reported unavailable, so the same days
        are not reported twice."""
        out = []
        for status in UNAVAILABLE_STATUSES:
            for item in disruptions_with_status(self.table, status):
                report = from_dynamo(item.get("report") or {})
                kind, d = report.get("type", ""), report.get("details") or {}
                if kind in ("crew_unavailable", "multiple_crew_unavailable") and crew_id in (
                    crew_out_of(kind, d)
                ):
                    out.append(
                        {
                            "disruption_id": item["disruption_id"],
                            "from_date": d["from_date"],
                            "to_date": d["to_date"],
                            "reason": d.get("reason"),
                            "status": item["status"],
                        }
                    )
        return sorted(out, key=lambda r: r["from_date"])

    def _crew_notices(self, crew_id: str) -> list[dict[str, Any]]:
        """In-app notices addressed to this crew member (a certificate about to lapse),
        newest first, with how the disruption behind each stands."""
        out = []
        for status in FEED_STATUSES:
            for item in disruptions_with_status(self.table, status):
                analysis = from_dynamo(item.get("analysis") or {})
                for n in analysis.get("notices") or []:
                    if n.get("channel") == "in_app" and n.get("about") == crew_id:
                        report = from_dynamo(item.get("report") or {})
                        out.append(
                            {
                                "disruption_id": item["disruption_id"],
                                "status": item["status"],
                                "at": report.get("reported_utc"),
                                "message": n["message"],
                            }
                        )
        return sorted(out, key=lambda n: n["at"] or "", reverse=True)

    # ------------------------------------------------------------------
    # Reporting desk
    # ------------------------------------------------------------------

    def my_reports(self, caller: Caller, scope: str = "mine", limit: int = 30) -> list[dict]:
        """Reports and how each stands, newest first: still being checked, refused (and
        why), or the disruption it became. ``scope`` "mine" is what the caller filed;
        "crew" is what crew filed about themselves, for the reporting desk."""
        limit = max(1, min(int(limit), 100))
        if scope == "crew":
            if not caller.roles & (OPERATIONAL_REPORTERS | READERS):
                raise ApiError("FORBIDDEN", "only the reporting desk and crew control see these")
            filed = [
                f for f in filed_recently(self.table, 300) if "crew" in (f.get("roles") or [])
            ][:limit]
        elif scope == "mine":
            filed = filed_by(self.table, caller.user_id, limit)
        else:
            raise ApiError("INVALID", "scope is mine or crew")
        return [self._filed_state(f) for f in filed]

    def _filed_state(self, filed: Mapping[str, Any]) -> dict[str, Any]:
        report = from_dynamo(filed.get("report") or {})
        out = {
            "disruption_id": filed["report_id"],
            "report_type": report.get("type"),
            "reported_utc": report.get("reported_utc"),
            "reported_by": filed.get("user_id"),
            "details": report.get("details", {}),
            "narrative": report.get("narrative") or "",
            "handled": None,
            "remark": None,
            "desks": [],
        }
        item = get_disruption(self.table, filed["report_id"])
        if item is not None:
            analysis = from_dynamo(item.get("analysis") or {})
            handling = analysis.get("handling") or {}
            # The controller desk it went to: set when it is analysed, so none before.
            desks = owning_desks(analysis, lambda _pairing: None) if analysis else []
            return out | {
                "status": item["status"],
                "desks": [{"desk_id": d.desk_id, "controller": d.controller} for d in desks],
                "outcome": _outcome(item["status"], handling),
                "handled": handling.get("text") if handling.get("kind") == "internal" else None,
                "remark": _remark(self.table, filed["report_id"], item["status"]),
            }
        refused = rejections_for(self.table, filed["report_id"])
        if refused:
            reason = refused[-1].get("reason", "")
            return out | {"status": "REJECTED", "outcome": f"Not accepted: {reason}"}
        return out | {"status": "QUEUED", "outcome": "Received; being checked against the roster."}

    def _already_out(
        self, details: Mapping[str, Any], report_type: str, report_id: str
    ) -> str | None:
        """The days a crew member in this report is already reported unavailable, said as
        the reason to refuse it, or None. Counts open disruptions and reports still in the
        queue (filed, neither stored nor refused yet); a resend of the same report is left
        to the worker, which stores it once."""
        if report_type not in ("crew_unavailable", "multiple_crew_unavailable"):
            return None
        wanted = set(crew_out_of(report_type, details))
        start, end = (
            date.fromisoformat(details["from_date"]),
            date.fromisoformat(details["to_date"]),
        )

        def overlap(other_id: str, other: Mapping[str, Any]) -> str | None:
            if other_id == report_id:
                return None
            d = other.get("details") or {}
            kind = other.get("type", "")
            if kind not in ("crew_unavailable", "multiple_crew_unavailable"):
                return None
            clash = wanted & set(crew_out_of(kind, d))
            if not clash:
                return None
            o_start, o_end = date.fromisoformat(d["from_date"]), date.fromisoformat(d["to_date"])
            if start <= o_end and o_start <= end:
                return (
                    f"{', '.join(sorted(clash))} is already reported unavailable "
                    f"{d['from_date']} to {d['to_date']} ({other_id})"
                )
            return None

        for status in UNAVAILABLE_STATUSES:
            for item in disruptions_with_status(self.table, status):
                report = from_dynamo(item.get("report") or {})
                if why := overlap(str(item["disruption_id"]), report):
                    return why
        # Reports still queued: only a clashing one is looked up, to see whether the worker
        # has stored it (counted above, or closed since) or refused it (no longer counts).
        for row in filed_recently(self.table):
            rid = str(row["report_id"])
            why = overlap(rid, from_dynamo(row.get("report") or {}))
            if (
                why
                and get_disruption(self.table, rid) is None
                and not rejections_for(self.table, rid)
            ):
                return why
        return None

    def reference(self, caller: Caller) -> dict[str, Any]:
        """The crew, flights, aircraft, gates and stations a report can name."""
        if not caller.roles & OPERATIONAL_REPORTERS:
            raise ApiError("FORBIDDEN", "only reporters, controllers and approvers file reports")
        return views.reference(self.live().pipeline.tools)

    # ------------------------------------------------------------------
    # Rule thresholds and system activity
    # ------------------------------------------------------------------

    def thresholds(self, caller: Caller) -> list[dict[str, Any]]:
        if not caller.roles & READERS:
            raise ApiError("FORBIDDEN", "your role may not read the rule settings")
        return rule_store.thresholds(self.table)

    def set_threshold(
        self,
        caller: Caller,
        rule_id: str,
        param: str,
        value: float,
        expected: float,
        reason: str,
    ) -> dict[str, Any]:
        if not caller.roles & ADMINS:
            raise ApiError("FORBIDDEN", "only an admin changes rule thresholds")
        try:
            out = rule_store.set_threshold(
                self.table,
                rule_id,
                param,
                value,
                expected,
                caller.user_id,
                reason,
                self.clock(),
                may_relax=Role.SUPER_ADMIN in caller.roles,
            )
        except rule_store.RuleError as exc:
            raise ApiError(exc.code, exc.message) from None
        self.invalidate()  # answer from the new rules at once on this server
        return out

    def activity(self, caller: Caller, limit: int = 50) -> list[dict[str, Any]]:
        if not caller.roles & ADMINS:
            raise ApiError("FORBIDDEN", "only an admin reads the activity log")
        return rule_store.activity(self.table, max(1, min(int(limit), 200)))

    # ------------------------------------------------------------------
    # Alert addresses (admins only)
    # ------------------------------------------------------------------

    def _alerts(self, call: Callable[[AlertSubscriptions], Any]) -> Any:
        if self.alerts is None:
            raise ApiError("UNAVAILABLE", "alerts are not set up on this server")
        try:
            return call(self.alerts)
        except AlertError as exc:
            raise ApiError(exc.code, exc.message) from None

    def alert_addresses(self, caller: Caller) -> list[dict[str, Any]]:
        return self._alerts(lambda a: a.list(caller))

    def add_alert_address(self, caller: Caller, email: str, role: str) -> dict[str, Any]:
        return self._alerts(lambda a: a.add(caller, email, role))

    def remove_alert_address(self, caller: Caller, subscription_id: str) -> dict[str, Any]:
        return self._alerts(lambda a: a.remove(caller, subscription_id))

    # ------------------------------------------------------------------

    def _plan_action(self, action: Callable[..., Any], *args: Any) -> dict[str, Any]:
        try:
            out = action(*args)
        except PlanError as exc:
            raise ApiError(exc.code, exc.message) from None
        self.invalidate()  # the roster may have changed: answer from the new one
        result = {"status": out.status}
        if "plan_id" in out.plan:
            result |= plan_summary(out.plan)
        if out.warnings:
            result["contention"] = list(out.warnings)
        if out.reasons:
            result["reasons"] = list(out.reasons)
        return result

    def submit(
        self,
        caller: Caller,
        disruption_id: str,
        choices: Mapping[str, str] | None = None,
        note: str = "",
        accept_uncovered: bool = False,
    ) -> dict[str, Any]:
        return self._plan_action(
            self.plans.submit, caller, disruption_id, dict(choices or {}), note, accept_uncovered
        )

    def approve(self, caller: Caller, disruption_id: str, plan_id: str) -> dict[str, Any]:
        return self._plan_action(self.plans.approve, caller, disruption_id, plan_id)

    def reject(self, caller: Caller, disruption_id: str, plan_id: str, reason: str) -> dict:
        return self._plan_action(self.plans.reject, caller, disruption_id, plan_id, reason)

    def withdraw(self, caller: Caller, disruption_id: str, plan_id: str, reason: str = "") -> dict:
        return self._plan_action(self.plans.withdraw, caller, disruption_id, plan_id, reason)

    def close(self, caller: Caller, disruption_id: str, reason: str) -> dict[str, Any]:
        return self._plan_action(self.plans.close, caller, disruption_id, reason)
