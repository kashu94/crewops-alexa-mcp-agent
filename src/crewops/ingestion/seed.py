"""Load the seed operating week from JSON and check it hangs together.

Parsing (``load_seed``) checks each record on its own: types, enums, timestamps.
Validation (``validate``) checks records against each other: every id a pairing names
exists, roles match ranks, nobody is rostered twice at once, legs connect.

Errors mean the data cannot be trusted and loading into DynamoDB must stop. Warnings
are oddities the engine is built to tolerate, reported so they are never a surprise.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from crewops.domain import (
    Certification,
    CostTable,
    Crew,
    DataError,
    DutyClock,
    Flight,
    GateSlot,
    Pairing,
    ReserveDuty,
    RiskSignal,
    RosterException,
    Rule,
)

REPORT_BEFORE_FIRST_DEPARTURE = timedelta(minutes=60)
RELEASE_AFTER_LAST_ARRIVAL = timedelta(minutes=30)
BLOCK_HOURS_TOLERANCE = 0.01

SEED_FILES = (
    "crew.json",
    "flights.json",
    "rosters.json",
    "reserve_pool.json",
    "certifications.json",
    "duty_clocks.json",
    "risk_signals.json",
    "rules.json",
    "costs.json",
    "boarding_gates.json",
)


def default_seed_dir() -> Path:
    """``data/seed`` at the repository root, or under ``CREWOPS_DATA_DIR`` when set (the
    deployed MCP server, whose package keeps the data beside the code)."""
    if root := os.environ.get("CREWOPS_DATA_DIR"):
        return Path(root) / "seed"
    return Path(__file__).resolve().parents[3] / "data" / "seed"


@dataclass(frozen=True, slots=True)
class SeedData:
    crew: tuple[Crew, ...]
    flights: tuple[Flight, ...]
    pairings: tuple[Pairing, ...]
    exceptions: tuple[RosterException, ...]
    reserves: tuple[ReserveDuty, ...]
    certifications: tuple[Certification, ...]
    duty_clocks: tuple[DutyClock, ...]
    risk_signals: tuple[RiskSignal, ...]
    rules: tuple[Rule, ...]
    costs: CostTable
    gates: tuple[GateSlot, ...]

    @property
    def snapshot_utc(self) -> datetime:
        """The "now" the duty clocks were computed at."""
        return max(clock.as_of_utc for clock in self.duty_clocks)


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Issue:
    severity: Severity
    entity: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.entity}: {self.message}"


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise DataError(f"{path.name}: file not found in {path.parent}") from None
    except json.JSONDecodeError as exc:
        raise DataError(f"{path.name}: invalid JSON at line {exc.lineno}: {exc.msg}") from None


def _parse_list[T](rows: Any, name: str, parse: Callable[[Any, str], T]) -> tuple[T, ...]:
    if not isinstance(rows, list):
        raise DataError(f"{name}: expected a list at the top level")
    return tuple(parse(row, f"{name}[{i}]") for i, row in enumerate(rows))


def load_seed(directory: Path | str | None = None) -> SeedData:
    """Parse every seed file. Raises ``DataError`` naming the first bad field."""
    root = Path(directory) if directory is not None else default_seed_dir()
    raw = {name: _read(root / name) for name in SEED_FILES}

    rosters = raw["rosters.json"]
    rules = raw["rules.json"]
    if not isinstance(rosters, dict) or not isinstance(rules, dict):
        raise DataError("rosters.json and rules.json must be objects at the top level")

    return SeedData(
        crew=_parse_list(raw["crew.json"], "crew", Crew.from_dict),
        flights=_parse_list(raw["flights.json"], "flights", Flight.from_dict),
        pairings=_parse_list(rosters.get("pairings"), "pairings", Pairing.from_dict),
        exceptions=_parse_list(
            rosters.get("flagged_exceptions", []), "flagged_exceptions", RosterException.from_dict
        ),
        reserves=_parse_list(raw["reserve_pool.json"], "reserve_pool", ReserveDuty.from_dict),
        certifications=_parse_list(
            raw["certifications.json"], "certifications", Certification.from_dict
        ),
        duty_clocks=_parse_list(raw["duty_clocks.json"], "duty_clocks", DutyClock.from_dict),
        risk_signals=_parse_list(raw["risk_signals.json"], "risk_signals", RiskSignal.from_dict),
        rules=_parse_list(rules.get("rules"), "rules", Rule.from_dict),
        costs=CostTable.from_dict(raw["costs.json"]),
        gates=_parse_list(raw["boarding_gates.json"], "boarding_gates", GateSlot.from_dict),
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _duplicates(ids: Iterable[str]) -> list[str]:
    return sorted(i for i, n in Counter(ids).items() if n > 1)


def validate(seed: SeedData) -> list[Issue]:
    """Cross-record checks. An empty list of errors means the data is safe to load."""
    issues: list[Issue] = []

    def error(entity: str, message: str) -> None:
        issues.append(Issue(Severity.ERROR, entity, message))

    def warn(entity: str, message: str) -> None:
        issues.append(Issue(Severity.WARNING, entity, message))

    for kind, ids in (
        ("crew", [c.crew_id for c in seed.crew]),
        ("flight", [f.flight_id for f in seed.flights]),
        ("pairing", [p.pairing_id for p in seed.pairings]),
        ("rule", [r.rule_id for r in seed.rules]),
        ("duty clock", [c.crew_id for c in seed.duty_clocks]),
        ("risk signal", [r.crew_id for r in seed.risk_signals]),
        ("gate slot for flight", [g.flight_id for g in seed.gates]),
        ("certification", [f"{c.crew_id} {c.cert_type}" for c in seed.certifications]),
        ("reserve duty", [f"{r.crew_id} at {r.base}" for r in seed.reserves]),
        (
            "roster exception",
            [f"{e.crew_id} {e.date} {e.rule_id}" for e in seed.exceptions],
        ),
    ):
        # Each of these is one item in the table; a duplicate would silently overwrite.
        for dup in _duplicates(ids):
            error(dup, f"duplicate {kind}")

    crew = {c.crew_id: c for c in seed.crew}
    flights = {f.flight_id: f for f in seed.flights}

    # -- Pairings -----------------------------------------------------------
    flown_by: dict[str, list[str]] = defaultdict(list)
    duties: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)

    for p in seed.pairings:
        for day in p.days:
            legs = []
            for fid in day.flight_ids:
                flown_by[fid].append(p.pairing_id)
                if fid not in flights:
                    error(p.pairing_id, f"{day.date}: unknown flight {fid}")
                    continue
                legs.append(flights[fid])
            for leg in legs:
                if leg.date != day.date:
                    error(p.pairing_id, f"{leg.flight_id} is dated {leg.date}, not {day.date}")
                if leg.aircraft != p.aircraft:
                    error(p.pairing_id, f"{leg.flight_id} flies {leg.aircraft}, not {p.aircraft}")
            for prev, nxt in zip(legs, legs[1:], strict=False):
                if prev.arr_station != nxt.dep_station:
                    error(
                        p.pairing_id,
                        f"{prev.flight_id} lands at {prev.arr_station} but "
                        f"{nxt.flight_id} departs {nxt.dep_station}",
                    )
                if nxt.dep_utc < prev.arr_utc:
                    error(p.pairing_id, f"{nxt.flight_id} departs before {prev.flight_id} lands")
            if len({leg.aircraft_type for leg in legs}) > 1:
                error(p.pairing_id, f"{day.date}: legs on more than one aircraft type")
            if legs:
                if day.report_utc != legs[0].dep_utc - REPORT_BEFORE_FIRST_DEPARTURE:
                    warn(p.pairing_id, f"{day.date}: report is not 60 min before first departure")
                if day.release_utc != legs[-1].arr_utc + RELEASE_AFTER_LAST_ARRIVAL:
                    warn(p.pairing_id, f"{day.date}: release is not 30 min after last arrival")

        for seat in p.crew:
            member = crew.get(seat.crew_id)
            if member is None:
                error(p.pairing_id, f"unknown crew {seat.crew_id}")
                continue
            if member.rank is not seat.role:
                error(
                    p.pairing_id,
                    f"{seat.crew_id} is a {member.rank} rostered as {seat.role}",
                )
            for day in p.days:
                duties[seat.crew_id].append((day.report_utc, day.release_utc, p.pairing_id))

    for fid, pairings in flown_by.items():
        if len(pairings) > 1:
            error(fid, f"flown by more than one pairing: {', '.join(pairings)}")
    for fid in sorted(set(flights) - set(flown_by)):
        warn(fid, "not covered by any pairing")

    for crew_id, spans in duties.items():
        spans.sort()
        for (_, end_a, pa), (start_b, _, pb) in zip(spans, spans[1:], strict=False):
            if pa != pb and start_b < end_a:
                error(crew_id, f"double-booked: {pa} overlaps {pb}")

    # -- Per-crew records ---------------------------------------------------
    def known(records: Iterable[Any], kind: str) -> None:
        for r in records:
            if r.crew_id not in crew:
                error(r.crew_id, f"{kind} for unknown crew")

    known(seed.reserves, "reserve duty")
    known(seed.certifications, "certification")
    known(seed.duty_clocks, "duty clock")
    known(seed.risk_signals, "risk signal")
    known(seed.exceptions, "roster exception")

    for r in seed.reserves:
        if r.crew_id in crew and crew[r.crew_id].base != r.base:
            error(r.crew_id, f"reserve at {r.base} but based at {crew[r.crew_id].base}")

    clocks = Counter(c.crew_id for c in seed.duty_clocks)
    for crew_id in crew:
        if clocks[crew_id] == 0:
            error(crew_id, "no duty clock")

    certified = {c.crew_id for c in seed.certifications}
    for crew_id in sorted(set(crew) - certified):
        warn(crew_id, "no certifications on file")
    for c in seed.certifications:
        if c.valid_from > c.valid_to:
            warn(c.crew_id, f"{c.cert_type} valid_from {c.valid_from} is after valid_to")

    # -- Flights and gates --------------------------------------------------
    for f in seed.flights:
        elapsed = (f.arr_utc - f.dep_utc).total_seconds() / 3600
        if abs(elapsed - f.block_hours) > BLOCK_HOURS_TOLERANCE:
            warn(f.flight_id, f"block_hours {f.block_hours} but scheduled {elapsed:.2f}h")

    for g in seed.gates:
        flight = flights.get(g.flight_id)
        if flight is None:
            error(g.gate, f"slot for unknown flight {g.flight_id}")
            continue
        if g.pairing_id not in flown_by.get(g.flight_id, []):
            error(g.gate, f"{g.flight_id} is not flown by {g.pairing_id}")
        if g.end_utc != flight.dep_utc:
            warn(g.gate, f"slot for {g.flight_id} does not end at departure")
        if g.aircraft_type != flight.aircraft_type:
            error(g.gate, f"{g.flight_id} aircraft type mismatch")

    return issues


def errors(issues: Iterable[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity is Severity.ERROR]
