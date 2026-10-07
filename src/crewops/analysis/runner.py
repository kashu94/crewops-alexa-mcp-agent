"""Analyse stored disruptions: load the operation, run ``analyse``, store the result once.

Shared by the stream-triggered Lambda (new reports) and the ``run`` command (reports that
were stored before analysis existed, or need analysing again after a failure).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from crewops.analysis.disruption import analyse
from crewops.domain import DataError, GateSlot, Report, ReportType
from crewops.engine import build_world
from crewops.engine.world import World
from crewops.store.items import from_dynamo
from crewops.store.ops_store import OpsStore
from crewops.store.reports import (
    STATUS_OPTIONS_READY,
    STATUS_REPORTED,
    AnalysisOutcome,
    disruptions_with_status,
    get_disruption,
    store_analysis,
)

log = logging.getLogger(__name__)

OPEN_STATUSES = (STATUS_REPORTED, STATUS_OPTIONS_READY)
"""Disruptions still being worked: their crew are out until resolved."""


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
        if report.report_type is ReportType.CREW_UNAVAILABLE:
            start, end = date.fromisoformat(d["from_date"]), date.fromisoformat(d["to_date"])
            dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        elif report.report_type is ReportType.CERTIFICATION_LAPSE:
            valid_to = date.fromisoformat(d["valid_to"])
            dates = [day for day in duty_dates if day > valid_to]
        else:
            continue
        out.setdefault(d["crew_id"], []).extend((day, report.report_id) for day in dates)
    return {crew: tuple(sorted(set(days))) for crew, days in out.items()}


@dataclass(frozen=True, slots=True)
class Operation:
    """The operation loaded once and reused for every disruption in a batch."""

    world: World
    gates: tuple[GateSlot, ...]

    @classmethod
    def load(cls, table: Any) -> Operation:
        """The roster from the table, plus everyone open disruptions have taken out.

        A crew member reported sick in one disruption must not be offered as cover in
        another, so the crew named by open crew-unavailable and certification-lapse
        reports are marked unavailable on the dates those reports cover.
        """
        seed = OpsStore(table).load_seed()
        world = build_world(seed)
        open_items = [
            item for status in OPEN_STATUSES for item in disruptions_with_status(table, status)
        ]
        return cls(
            world=replace(world, unavailable=unavailable_crew(world, open_items)), gates=seed.gates
        )


@dataclass(frozen=True, slots=True)
class RunResult:
    disruption_id: str
    outcome: str
    """stored, already_analysed, not_reported (status moved on) or missing."""
    summary: str = ""


def analyse_item(table: Any, item: dict[str, Any], operation: Operation) -> RunResult:
    """Analyse one disruption item (as stored, or as a stream image) and store the result."""
    disruption_id = item["disruption_id"]
    if item.get("status") != STATUS_REPORTED:
        return RunResult(disruption_id, "not_reported")
    report = Report.from_dict(from_dynamo(item["report"]))
    analysis = analyse(operation.world, report, operation.gates)
    outcome = store_analysis(table, disruption_id, analysis.to_dict(), datetime.now(UTC))
    if outcome is AnalysisOutcome.STORED:
        log.info("analysed %s: %s", disruption_id, analysis.summary)
    return RunResult(disruption_id, str(outcome), analysis.summary)


def analyse_by_id(table: Any, disruption_id: str, operation: Operation) -> RunResult:
    item = get_disruption(table, disruption_id)
    if item is None:
        return RunResult(disruption_id, "missing")
    return analyse_item(table, item, operation)
