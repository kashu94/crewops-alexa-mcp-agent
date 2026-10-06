"""The operation in memory, shaped for the rules.

A ``World`` is built once from typed records and then only read. The whole operating week
is small enough that answering from memory beats a database round trip per question. The
store (DynamoDB) loads the records; this module only decides how the engine sees them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from crewops.domain import CostTable, Flight, Pairing, ReserveDuty
from crewops.engine.duty import DutyDay, hours_between
from crewops.engine.rules import CrewState, Duty, RuleBook
from crewops.ingestion.seed import SeedData

POSITIONING_REPORT_BUFFER = timedelta(minutes=15)
"""A positioned crew member reports this long after their positioning flight lands."""


class UnknownEntity(LookupError):
    """An id that does not exist. Callers suggest near matches; the engine never guesses."""

    def __init__(self, kind: str, value: str) -> None:
        super().__init__(f"no {kind} {value!r}")
        self.kind = kind
        self.value = value


@dataclass(frozen=True, slots=True)
class Positioning:
    """How a crew member from another base gets to the first departure."""

    flight_id: str
    from_station: str
    to_station: str
    arrives_utc: datetime
    delay_hours: float
    """How far the pairing must slip so they can report after landing."""


@dataclass(frozen=True, slots=True)
class World:
    crew: Mapping[str, CrewState]
    pairings: Mapping[str, Pairing]
    duty_days: Mapping[str, tuple[DutyDay, ...]]
    flights: Mapping[str, Flight]
    reserves: Mapping[str, tuple[ReserveDuty, ...]]
    costs: CostTable
    book: RuleBook
    snapshot_utc: datetime

    def state(self, crew_id: str) -> CrewState:
        try:
            return self.crew[crew_id]
        except KeyError:
            raise UnknownEntity("crew", crew_id) from None

    def pairing(self, pairing_id: str) -> Pairing:
        try:
            return self.pairings[pairing_id]
        except KeyError:
            raise UnknownEntity("pairing", pairing_id) from None

    def days_of(self, pairing_id: str) -> tuple[DutyDay, ...]:
        self.pairing(pairing_id)
        return self.duty_days[pairing_id]

    def positioning(self, crew: CrewState, day: DutyDay) -> Positioning | None:
        """The same-day flight from the crew member's base that delays the day least.

        None means nobody can get them there that day: a base-rule exclusion.
        """
        best: Positioning | None = None
        for f in self.flights.values():
            if f.date != day.date or f.dep_station != crew.base:
                continue
            if f.arr_station != day.dep_station:
                continue
            new_report = f.arr_utc + POSITIONING_REPORT_BUFFER
            slip = max(0.0, hours_between(day.report_utc, new_report))
            if best is None or (slip, f.arr_utc) < (best.delay_hours, best.arrives_utc):
                best = Positioning(f.flight_id, f.dep_station, f.arr_station, f.arr_utc, slip)
        return best


def build_world(seed: SeedData) -> World:
    flights = {f.flight_id: f for f in seed.flights}

    duty_days: dict[str, tuple[DutyDay, ...]] = {}
    duties: dict[str, list[Duty]] = defaultdict(list)
    for p in seed.pairings:
        days = []
        for day in p.days:
            legs = [flights[fid] for fid in day.flight_ids]
            days.append(
                DutyDay(
                    date=day.date,
                    report_utc=day.report_utc,
                    release_utc=day.release_utc,
                    n_sectors=len(legs),
                    block_hours=round(sum(leg.block_hours for leg in legs), 2),
                    aircraft_type=legs[0].aircraft_type,
                    dep_station=legs[0].dep_station,
                )
            )
        duty_days[p.pairing_id] = tuple(days)
        for seat in p.crew:
            for d in days:
                duties[seat.crew_id].append(
                    Duty(p.pairing_id, d.date, d.report_utc, d.release_utc, d.block_hours)
                )

    clocks = {c.crew_id: c for c in seed.duty_clocks}
    certs: dict[str, list] = defaultdict(list)
    for c in seed.certifications:
        certs[c.crew_id].append((c.cert_type, c.valid_from, c.valid_to))

    crew: dict[str, CrewState] = {}
    for c in seed.crew:
        clock = clocks.get(c.crew_id)
        history = clock.daily_history if clock else ()
        crew[c.crew_id] = CrewState(
            crew_id=c.crew_id,
            name=c.name,
            rank=c.rank,
            base=c.base,
            ratings=c.ratings,
            status=c.status,
            last_rest_ended=clock.last_rest_ended if clock else None,
            history_end=history[-1].date if history else None,
            daily_duty={h.date: h.duty_hours for h in history},
            daily_flight={h.date: h.flight_hours for h in history},
            certs=tuple(certs.get(c.crew_id, ())),
            duties=tuple(sorted(duties.get(c.crew_id, ()), key=lambda d: d.report_utc)),
        )

    reserves: dict[str, list[ReserveDuty]] = defaultdict(list)
    for r in seed.reserves:
        reserves[r.crew_id].append(r)

    return World(
        crew=crew,
        pairings={p.pairing_id: p for p in seed.pairings},
        duty_days=duty_days,
        flights=flights,
        reserves={k: tuple(v) for k, v in reserves.items()},
        costs=seed.costs,
        book=RuleBook.from_rules(seed.rules),
        snapshot_utc=seed.snapshot_utc,
    )
