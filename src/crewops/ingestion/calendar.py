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

from crewops.domain import CrewStatus, Pairing
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
    for week in range(1, weeks + 1):
        shift = WEEK * week
        rotate = _rotation(seed, week, first + shift)
        kept: set[str] = set()
        for p in seed.pairings:
            if p.days[-1].date + shift > last_day:
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
    out = replace(
        seed,
        flights=tuple(flights),
        pairings=tuple(pairings),
        gates=tuple(gates),
        reserves=tuple(reserves),
    )
    return _repaired(out, week_of)


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
