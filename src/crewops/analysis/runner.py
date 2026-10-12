"""Analyse stored disruptions: load the operation, run ``analyse``, store the result once.

Shared by the stream-triggered Lambda (new reports) and the ``run`` command (reports that
were stored before analysis existed, or need analysing again after a failure).
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from crewops.analysis.disruption import analyse
from crewops.domain import DataError, GateSlot, Report, ReportType
from crewops.domain.desks import assign_desk
from crewops.domain.reports import ABSENCE_TYPES, crew_out_of
from crewops.engine import build_world
from crewops.engine.world import World
from crewops.ingestion.seed import SeedData
from crewops.store.items import from_dynamo
from crewops.store.ops_store import OpsStore
from crewops.store.plans import PLAN_COMMITTED, plans_with_status
from crewops.store.reports import (
    STATUS_OPTIONS_READY,
    STATUS_REPORTED,
    AnalysisOutcome,
    disruptions_with_status,
    get_disruption,
    store_analysis,
)

log = logging.getLogger(__name__)

OPEN_STATUSES = (STATUS_REPORTED, STATUS_OPTIONS_READY, "SUBMITTED")
"""Disruptions still being worked: counted at their controllers' desks."""
UNAVAILABLE_STATUSES = (*OPEN_STATUSES, "COMMITTED", "CLOSED")
"""Disruptions whose crew are out on their dates. A committed or closed one still counts:
the crew member is still sick (or still lacks the certificate), whatever was done about
their seats."""


def absences_from(items: list[dict[str, Any]]) -> dict[tuple[str, date], datetime]:
    """(crew id, date) -> when an absence reported from a time starts that day. A whole-day
    absence for the same person and date wins: they are out all day."""
    since: dict[tuple[str, date], datetime | None] = {}
    for item in items:
        try:
            report = Report.from_dict(from_dynamo(item["report"]))
        except (DataError, KeyError, TypeError):
            continue
        if report.report_type not in ABSENCE_TYPES:
            continue
        d = report.details
        first = date.fromisoformat(d["from_date"])
        at = (
            datetime.fromisoformat(d["from_utc"].replace("Z", "+00:00"))
            if d.get("from_utc")
            else None
        )
        for crew_id in crew_out_of(report.report_type, d):
            key = (crew_id, first)
            if at is None or key in since and since[key] is None:
                since[key] = None
            else:
                since[key] = min(at, since.get(key) or at)
    return {k: v for k, v in since.items() if v is not None}


def cancelled_flights(table: Any) -> frozenset[str]:
    """Flights an approved plan cancelled: a cancelled trip's flights, which nobody flies."""
    return frozenset(
        f
        for plan in plans_with_status(table, PLAN_COMMITTED)
        for a in plan.get("assignments", [])
        if a.get("strategy") == "cancel"
        for f in a.get("flights", [])
    )


def unavailable_crew(
    world: World, items: list[dict[str, Any]]
) -> dict[str, tuple[tuple[date, str], ...]]:
    """Crew id -> (date, report id) for every open report that takes someone out."""
    duty_dates = sorted({d.date for days in world.duty_days.values() for d in days})
    out: dict[str, list[tuple[date, str]]] = {}
    for item in items:
        try:
            report = Report.from_dict(from_dynamo(item["report"]))
        except (DataError, KeyError, TypeError):
            log.warning("skipping unreadable disruption %s", item.get("disruption_id"))
            continue
        d = report.details
        if report.report_type in ABSENCE_TYPES:
            start, end = date.fromisoformat(d["from_date"]), date.fromisoformat(d["to_date"])
            dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        elif report.report_type is ReportType.CERTIFICATION_LAPSE:
            valid_to = date.fromisoformat(d["valid_to"])
            dates = [day for day in duty_dates if day > valid_to]
        else:
            continue
        for crew_id in crew_out_of(report.report_type, d):
            out.setdefault(crew_id, []).extend((day, report.report_id) for day in dates)
    return {crew: tuple(sorted(set(days))) for crew, days in out.items()}


ALL_STATUSES = (*OPEN_STATUSES, "COMMITTED", "CLOSED")


def next_desk(table: Any) -> str:
    """The desk the next analysed disruption goes to: the controller with the fewest open
    disruptions, then the fewest overall (so ties take turns), then desk order."""
    open_counts: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for status in ALL_STATUSES:
        for item in disruptions_with_status(table, status):
            desk_id = (from_dynamo(item.get("analysis") or {})).get("desk")
            if desk_id:
                totals[desk_id] += 1
                if status in OPEN_STATUSES:
                    open_counts[desk_id] += 1
    return assign_desk(open_counts, totals).desk_id


def seat_keys(analysis: dict[str, Any]) -> set[tuple[str, str, str]]:
    """The seats an analysis needs covered: trip, role and the crew member who is out."""
    return {
        (s["pairing_id"], s["role"], s["crew_out"])
        for s in analysis.get("seats") or []
        if s.get("crew_out") and s.get("pairing_id") and s.get("role")
    }


def same_seat_owner(table: Any, analysis: dict[str, Any]) -> tuple[str | None, list[str]]:
    """An open disruption already needing one of these seats, and its desk. One person out
    on two days of one trip, reported twice, is one seat: whoever covers flies the whole
    trip, so the second report goes to the desk already handling it, as crew control would
    add it to the open case."""
    mine = seat_keys(analysis)
    if not mine:
        return None, []
    desk, ids = None, []
    for status in OPEN_STATUSES:
        for item in disruptions_with_status(table, status):
            theirs = from_dynamo(item.get("analysis") or {})
            if mine & seat_keys(theirs):
                ids.append(str(item["disruption_id"]))
                desk = desk or theirs.get("desk")
    return desk, sorted(ids)


def unavailable_items(table: Any) -> list[dict[str, Any]]:
    return [
        item for status in UNAVAILABLE_STATUSES for item in disruptions_with_status(table, status)
    ]


def open_disruptions(table: Any) -> list[dict[str, Any]]:
    """Every disruption still being worked, oldest status first."""
    return [item for status in OPEN_STATUSES for item in disruptions_with_status(table, status)]


def operation_now() -> datetime | None:
    """The real time in a deployed operation (``CREWOPS_REAL_TIME=1``, set by its stacks),
    so flights before it have flown and reports look ahead from today. Unset (tests, the
    evaluation, local runs) it is None: the seed's own moment, the same every run."""
    return datetime.now(UTC) if os.environ.get("CREWOPS_REAL_TIME") == "1" else None


@dataclass(frozen=True, slots=True)
class Operation:
    """The operation loaded once and reused for every disruption in a batch."""

    world: World
    gates: tuple[GateSlot, ...]
    seed: SeedData | None = None
    """The records the world was built from, for tools that show them as they are."""

    @classmethod
    def load(cls, table: Any, now: datetime | None = None) -> Operation:
        """The roster from the table, plus everyone open disruptions have taken out.

        A crew member reported sick in one disruption must not be offered as cover in
        another, so the crew named by open crew-unavailable and certification-lapse
        reports are marked unavailable on the dates those reports cover. Flights an approved
        plan cancelled are marked cancelled: nobody flies them, or positions on them.
        """
        seed = OpsStore(table).load_seed()
        world = build_world(seed, now or operation_now())
        items = unavailable_items(table)
        return cls(
            world=replace(
                world,
                unavailable=unavailable_crew(world, items),
                unavailable_from=absences_from(items),
                cancelled=cancelled_flights(table),
            ),
            gates=seed.gates,
            seed=seed,
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    disruption_id: str
    outcome: str
    """stored, already_analysed, not_reported (status moved on) or missing."""
    summary: str = ""


def _on_call_then(world: World, report: Report) -> bool:
    """Whether anyone this report takes out is on call (reserve) in its period: then crew
    control must know, since the reserve pool at that base is one smaller."""
    d = report.details
    start, end = date.fromisoformat(d["from_date"]), date.fromisoformat(d["to_date"])
    return any(
        start <= day <= end
        for crew_id in crew_out_of(report.report_type, d)
        for duty in world.reserves.get(crew_id, ())
        for day in duty.dates
    )


def analyse_item(table: Any, item: dict[str, Any], operation: Operation) -> RunResult:
    """Analyse one disruption item (as stored, or as a stream image) and store the result."""
    disruption_id = item["disruption_id"]
    if item.get("status") != STATUS_REPORTED:
        return RunResult(disruption_id, "not_reported")
    report = Report.from_dict(from_dynamo(item["report"]))
    contacts = operation.seed.contacts if operation.seed else ()
    analysis = analyse(operation.world, report, operation.gates, contacts)
    doc = analysis.to_dict()
    record_only = None
    if (
        report.report_type in ABSENCE_TYPES
        and not (doc.get("seats") or doc.get("delays"))
        and not _on_call_then(operation.world, report)
    ):
        # Out on days they fly nothing (a day off): kept on record, so they are not called
        # out then, but there is nothing for a controller to plan, so no desk takes it.
        record_only = "Recorded: no flights to cover on these dates."
        doc["handling"] = {
            "kind": "recorded",
            "text": "Recorded: no flights to cover. Not called out on these days.",
        }
    else:
        owner, same = same_seat_owner(table, doc)
        doc["desk"] = owner or next_desk(table)
        if same:
            doc["notes"] = [
                *doc.get("notes", []),
                f"The same seat is already open in {', '.join(same)}: one cover fills both, "
                "so this goes to the same desk.",
            ]
    outcome = store_analysis(table, disruption_id, doc, datetime.now(UTC), record_only)
    if outcome is AnalysisOutcome.STORED:
        log.info("analysed %s: %s", disruption_id, analysis.summary)
    return RunResult(disruption_id, str(outcome), analysis.summary)


def analyse_by_id(table: Any, disruption_id: str, operation: Operation) -> RunResult:
    item = get_disruption(table, disruption_id)
    if item is None:
        return RunResult(disruption_id, "missing")
    return analyse_item(table, item, operation)
