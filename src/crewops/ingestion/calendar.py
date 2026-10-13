"""The operating calendar: the seed week carried forward, week by week, to a last date.

The seed is one hand-built week (12 to 18 October 2026). An operation running through the
judging period needs every week after it, so this repeats it: the same flights at the same
times each week, with passengers booked varying a little, and the crew rotating within
their airline, rank and base so nobody flies the same days for ever.

Each rotated week is checked with the legality engine: every rostered crew member must pass
every rule on every trip, counting the weeks either side. Anyone who would not is swapped
with a colleague of the same airline, rank and base for that week (both must then pass), so
the published roster is legal before any disruption touches it. The same seed always gives
the same calendar.

Trip ids keep their format: P-2291 in the seed week is P-2391 the week after, P-2491 the
next. Flight ids carry the date, as in the seed.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta

from crewops.domain import CrewStatus, Flight, GateSlot, Pairing
from crewops.domain.models import PairingDay, PairingSeat
from crewops.ingestion.seed import SeedData

LAST_DAY = date(2026, 12, 31)
"""The operation runs to here: past the hackathon's judging."""
WEEK = timedelta(days=7)
MAX_REPAIR_ROUNDS = 6


def _pairing_id(seed_id: str, week: int) -> str:
    number = int(seed_id.split("-")[1]) + 100 * week
    if number > 9999:
        raise ValueError(f"trip numbers run out at week {week}")
    return f"P-{number:04d}"


def _flight_id(flight_no: str, on: date) -> str:
    return f"{flight_no}-{on.isoformat()}"


def _booked(flight_id: str, seats: int, booked: int | None) -> int | None:
    """The seed's load, nudged by up to 8% either way per flight and week, never over."""
    if booked is None:
        return None
    nudge = int(hashlib.sha256(flight_id.encode()).hexdigest()[:4], 16) % 17 - 8  # -8..8
    return max(1, min(seats, round(booked * (1 + nudge / 100))))


def _groups(seed: SeedData) -> dict[str, list[str]]:
    """Crew id -> their colleagues of the same airline, rank and base (themselves too)."""
    by_key: dict[tuple, list[str]] = defaultdict(list)
    for c in sorted(seed.crew, key=lambda c: c.crew_id):
        by_key[(c.airline, c.rank, c.base)].append(c.crew_id)
    return {crew_id: members for members in by_key.values() for crew_id in members}


def _fit(seed: SeedData, start: date, end: date) -> set[str]:
    """Crew who may be rostered at all from ``start`` to ``end``: active, with every
    certificate still valid on the last day (a lapsed one is renewed before they fly again,
    which this calendar does not assume)."""
    expires: dict[str, date] = {}
    for c in seed.certifications:
        expires[c.crew_id] = min(expires.get(c.crew_id, c.valid_to), c.valid_to)
    return {
        c.crew_id
        for c in seed.crew
        if c.status == CrewStatus.ACTIVE and expires.get(c.crew_id, end) >= end
    }


def _rotation(seed: SeedData, week: int, start: date) -> dict[str, str]:
    """Seed crew id -> who flies that person's seed-week line in ``week``.

    Within each group (airline, rank, base) the lines with flying or on-call duty go to the
    fit members in turn, shifting along by one person a week; members who may not fly that
    week (on leave, a certificate lapsed) are given none.
    """
    fit = _fit(seed, start, start + timedelta(days=6))
    on_call = {r.crew_id for r in seed.reserves}  # the standing reserve pool: never rotated
    busy = {s.crew_id for p in seed.pairings for s in p.crew} - on_call
    out: dict[str, str] = {}
    seen: set[int] = set()
    for members in _groups(seed).values():
        if id(members) in seen:
            continue
        seen.add(id(members))
        lines = [m for m in members if m in busy]
        able = [m for m in members if m in fit and m not in on_call]
        if len(able) < len(lines):
            raise ValueError(f"too few crew fit to fly in week {week}: {members[0]}'s group")
        for i, line in enumerate(lines):
            out[line] = able[(i + week) % len(able)]
    return out


def extend(seed: SeedData, last_day: date = LAST_DAY) -> SeedData:
    """The seed with every following week up to ``last_day`` (trips ending after it are
    left out), crew rotated and checked legal."""
    first = min(f.date for f in seed.flights)
    weeks = (last_day - first).days // 7
    flights = list(seed.flights)
    pairings = list(seed.pairings)
    gates = list(seed.gates)
    # The reserve pool stays the same people, on call on the same weekdays every week they
    # are fit to (one record each, as the seed has).
    reserves = list(seed.reserves)
    seed_reserve_dates = {r.crew_id: r.dates for r in seed.reserves}
    week_of: dict[str, int] = {p.pairing_id: 0 for p in seed.pairings}
    # An aircraft whose seed week does not end where it starts (a two-day rotation over a
    # seven-day week) keeps its rotation going across weeks instead of repeating by weekday.
    rotating = _rotating_tails(seed)
    for week in range(1, weeks + 1):
        shift = WEEK * week
        rotate = _rotation(seed, week, first + shift)
        kept: set[str] = set()
        for p in seed.pairings:
            if p.days[-1].date + shift > last_day or p.aircraft in rotating:
                continue
            new_id = _pairing_id(p.pairing_id, week)
            kept |= set(p.flight_ids)
            pairings.append(
                Pairing(
                    pairing_id=new_id,
                    aircraft=p.aircraft,
                    days=tuple(
                        PairingDay(
                            date=d.date + shift,
                            flight_ids=tuple(
                                _flight_id(fid.rsplit("-", 3)[0], d.date + shift)
                                for fid in d.flight_ids
                            ),
                            report_utc=d.report_utc + shift,
                            release_utc=d.release_utc + shift,
                        )
                        for d in p.days
                    ),
                    crew=tuple(PairingSeat(rotate[s.crew_id], s.role) for s in p.crew),
                )
            )
            week_of[new_id] = week
        for f in seed.flights:
            if f.flight_id not in kept:
                continue
            new_fid = _flight_id(f.flight_no, f.date + shift)
            flights.append(
                replace(
                    f,
                    flight_id=new_fid,
                    date=f.date + shift,
                    dep_utc=f.dep_utc + shift,
                    arr_utc=f.arr_utc + shift,
                    booked=_booked(new_fid, f.seats, f.booked),
                )
            )
        for g in seed.gates:
            if g.flight_id not in kept:
                continue
            gates.append(
                replace(
                    g,
                    pairing_id=_pairing_id(g.pairing_id, week),
                    flight_id=_flight_id(g.flight_id.rsplit("-", 3)[0], g.date + shift),
                    date=g.date + shift,
                    start_utc=g.start_utc + shift,
                    end_utc=g.end_utc + shift,
                )
            )
        week_start = first + shift
        fit = _fit(seed, week_start, week_start + timedelta(days=6))
        for i, r in enumerate(reserves):
            if r.crew_id in fit:
                more = tuple(
                    d + shift for d in seed_reserve_dates[r.crew_id] if d + shift <= last_day
                )
                reserves[i] = replace(r, dates=r.dates + more)
    continued_gates: list[GateSlot] = []
    for tail in rotating:
        more_flights, more_pairings, more_gates, weeks_of = _continued(seed, tail, last_day)
        flights += more_flights
        pairings += more_pairings
        continued_gates += more_gates
        week_of |= weeks_of
    gates = _gated(gates, continued_gates)
    out = replace(
        seed,
        flights=tuple(flights),
        pairings=tuple(pairings),
        gates=tuple(gates),
        reserves=tuple(reserves),
    )
    return _repaired(out, week_of)


def _gated(fixed: list[GateSlot], moving: list[GateSlot]) -> list[GateSlot]:
    """``fixed`` plus ``moving``, each moving slot at its seed gate when that gate is free
    then, else at another gate of the same station that is: a rotation shifted to other
    weekdays must not park at a gate another flight already holds."""
    by_gate: dict[str, list[GateSlot]] = defaultdict(list)
    for g in fixed:
        by_gate[g.gate].append(g)
    station_gates: dict[str, list[str]] = defaultdict(list)
    for gate in sorted(by_gate):
        station_gates[gate.split("-")[0]].append(gate)

    def free(gate: str, slot: GateSlot) -> bool:
        return all(
            slot.end_utc <= other.start_utc or other.end_utc <= slot.start_utc
            for other in by_gate[gate]
        )

    out = list(fixed)
    for slot in sorted(moving, key=lambda g: g.start_utc):
        station = slot.gate.split("-")[0]
        choices = [slot.gate] + [g for g in station_gates[station] if g != slot.gate]
        gate = next((g for g in choices if free(g, slot)), None)
        if gate is None:
            raise ValueError(f"no free gate at {station} for {slot.flight_id}")
        placed = replace(slot, gate=gate)
        by_gate[gate].append(placed)
        out.append(placed)
    return out


def _rotating_tails(seed: SeedData) -> set[str]:
    """Aircraft whose seed week ends somewhere other than where it starts."""
    out = set()
    for tail in {f.aircraft for f in seed.flights}:
        legs = sorted((f for f in seed.flights if f.aircraft == tail), key=lambda f: f.dep_utc)
        if legs and legs[0].dep_station != legs[-1].arr_station:
            out.add(tail)
    return out


def _continued(
    seed: SeedData, tail: str, last_day: date
) -> tuple[list[Flight], list[Pairing], list[GateSlot], dict[str, int]]:
    """A two-day rotation continued day by day after the seed week.

    The seed week shows the rotation (day A ends at the night stop, day B returns), starting
    with a B day; it simply alternates on, and each A day with the B day after it is one
    trip, crewed from the seed's two-day trips in turn (rotated by week) and checked legal
    with the rest of the calendar. Trip ids stay four digits: the seed's ids for this
    aircraft plus a hundred a week, in the order the trips start.
    """
    first = min(f.date for f in seed.flights)
    trips = sorted(
        (p for p in seed.pairings if p.aircraft == tail and len(p.days) == 2),
        key=lambda p: p.days[0].date,
    )
    if not trips:
        raise ValueError(f"{tail}: no two-day trip in the seed week to continue")
    template = trips[0]
    by_id = {f.flight_id: f for f in seed.flights}
    gate_of = {g.flight_id: g for g in seed.gates}
    seed_ids = sorted(int(p.pairing_id.split("-")[1]) for p in seed.pairings if p.aircraft == tail)
    # The seed week alternates B, A, B, ...: day i (from the seed's first day) is an A day
    # when i is odd. Continue from the day after the seed week.
    flights: list[Flight] = []
    pairings: list[Pairing] = []
    gates: list[GateSlot] = []
    week_of: dict[str, int] = {}
    started: dict[int, int] = {}
    i = 7
    while True:
        start = first + timedelta(days=i)
        if start + timedelta(days=1) > last_day:
            break
        if i % 2 == 0:
            i += 1
            continue
        week = i // 7
        k = started.get(week, 0)
        started[week] = k + 1
        if k >= len(seed_ids):
            raise ValueError(f"{tail}: more trips in week {week} than ids to give them")
        pairing_id = f"P-{seed_ids[k] + 100 * week:04d}"
        crew_from = trips[k % len(trips)]
        rotate = _rotation(seed, week, first + WEEK * week)
        days = []
        for offset, seed_day in enumerate(template.days):
            day = start + timedelta(days=offset)
            shift = day - seed_day.date
            ids = []
            for fid in seed_day.flight_ids:
                f = by_id[fid]
                new_fid = _flight_id(f.flight_no, day)
                ids.append(new_fid)
                flights.append(
                    replace(
                        f,
                        flight_id=new_fid,
                        date=day,
                        dep_utc=f.dep_utc + shift,
                        arr_utc=f.arr_utc + shift,
                        booked=_booked(new_fid, f.seats, f.booked),
                    )
                )
                if (g := gate_of.get(fid)) is not None:
                    gates.append(
                        replace(
                            g,
                            pairing_id=pairing_id,
                            flight_id=new_fid,
                            date=day,
                            start_utc=g.start_utc + shift,
                            end_utc=g.end_utc + shift,
                        )
                    )
            days.append(
                PairingDay(
                    date=day,
                    flight_ids=tuple(ids),
                    report_utc=seed_day.report_utc + shift,
                    release_utc=seed_day.release_utc + shift,
                )
            )
        pairings.append(
            Pairing(
                pairing_id=pairing_id,
                aircraft=tail,
                days=tuple(days),
                crew=tuple(
                    PairingSeat(rotate.get(s.crew_id, s.crew_id), s.role) for s in crew_from.crew
                ),
            )
        )
        week_of[pairing_id] = week
        i += 1
    return flights, pairings, gates, week_of


def _illegal(seed: SeedData) -> list[tuple[str, str]]:
    """(pairing id, crew id) for every rostered seat whose holder breaks a rule."""
    from crewops.engine import build_world
    from crewops.engine.legality import assess

    world = build_world(seed)
    bad = []
    for p in seed.pairings:
        for s in p.crew:
            if not assess(world, s.crew_id, p.pairing_id).legal:
                bad.append((p.pairing_id, s.crew_id))
    return bad


def _swap(seed: SeedData, week_of: dict[str, int], week: int, a: str, b: str) -> SeedData:
    """``a`` and ``b`` exchange everything they fly in ``week``."""

    def other(crew_id: str) -> str:
        return b if crew_id == a else a if crew_id == b else crew_id

    pairings = tuple(
        replace(p, crew=tuple(PairingSeat(other(s.crew_id), s.role) for s in p.crew))
        if week_of.get(p.pairing_id) == week
        else p
        for p in seed.pairings
    )
    return replace(seed, pairings=pairings)


def _repaired(seed: SeedData, week_of: dict[str, int]) -> SeedData:
    """Swap whoever would fly illegally with a fit colleague who can take their week.

    One illegal seat at a time: a swap is kept only when it leaves fewer illegal seats
    overall (counting the weeks either side), so this always ends. The seed week itself is
    never changed: its exceptions are the demo's.
    """
    groups = _groups(seed)
    first = min(f.date for f in seed.flights)
    on_call = {r.crew_id for r in seed.reserves}  # the reserve pool stays on call

    def bad_of(s: SeedData) -> list[tuple[str, str]]:
        return [(p, c) for p, c in _illegal(s) if week_of.get(p, 0) > 0]

    bad = bad_of(seed)
    while bad:
        pairing_id, crew_id = bad[0]
        week = week_of[pairing_id]
        start = first + WEEK * week
        fit = _fit(seed, start, start + timedelta(days=6))
        for other in groups[crew_id]:
            if other == crew_id or other not in fit or other in on_call:
                continue
            trial = _swap(seed, week_of, week, crew_id, other)
            after = bad_of(trial)
            if len(after) < len(bad):
                seed, bad = trial, after
                break
        else:
            raise ValueError(f"cannot roster {crew_id} on {pairing_id} legally (week {week})")
    return seed
