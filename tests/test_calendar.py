"""The operating calendar to 31 December: the seed week carried forward, rotated, legal."""

from datetime import UTC, date, datetime

import pytest

from crewops.agent.entities import Vocabulary, extract_dates
from crewops.engine import build_world
from crewops.ingestion.calendar import LAST_DAY, _illegal, extend
from crewops.ingestion.seed import Severity, load_seed, validate


@pytest.fixture(scope="module")
def calendar():
    return extend(load_seed())


def test_every_week_to_the_end_of_december_is_rostered_and_legal(calendar):
    seed = load_seed()
    days = sorted({f.date for f in calendar.flights})
    assert days[0] == date(2026, 10, 12) and days[-1] <= LAST_DAY
    assert (LAST_DAY - days[-1]).days < 7
    assert len(calendar.flights) > 10 * len(seed.flights)
    assert not [i for i in validate(calendar) if i.severity is Severity.ERROR]
    # Only the seed week's deliberate exception (a lapsed certificate) breaks a rule.
    assert _illegal(calendar) == [("P-2213", "C-5417")]
    # Trip ids keep their format, a hundred on each week.
    ids = {p.pairing_id for p in calendar.pairings}
    assert {"P-2291", "P-2391", "P-2491"} <= ids
    assert all(p.days[-1].date <= LAST_DAY for p in calendar.pairings)


def test_crew_rotate_and_the_reserve_pool_stays(calendar):
    def crew_of(pairing_id):
        return {s.crew_id for p in calendar.pairings if p.pairing_id == pairing_id for s in p.crew}

    assert crew_of("P-2291") != crew_of("P-2391")  # someone else flies it the next week
    flying = {s.crew_id for p in calendar.pairings for s in p.crew}
    # Everyone fit to fly does, some weeks: not the reserve pool, nor those on leave or
    # with a lapsed certificate.
    assert len(flying) >= 120
    pool = {r.crew_id: r for r in calendar.reserves}
    assert len(pool) == len(load_seed().reserves)
    assert len(pool["C-3310"].dates) > 70  # on call every day, one record
    later = {s.crew_id for p in calendar.pairings if int(p.pairing_id[2:]) >= 2300 for s in p.crew}
    assert not later & set(pool)  # after the seed week, the reserve pool stays on call
    assert extend(load_seed()) == calendar  # the same seed always gives the same calendar


def test_now_moves_with_the_clock_and_questions_look_ahead(calendar):
    later = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    world = build_world(calendar, later)
    assert world.snapshot_utc == later
    assert build_world(calendar).snapshot_utc == load_seed().snapshot_utc  # tests stay fixed
    vocab = Vocabulary.from_world(world)
    assert extract_dates("who flies on the 15th?", vocab) == ["2026-11-15"]
    assert extract_dates("who flies on the 5th?", vocab) == ["2026-12-05"]
    assert extract_dates("reserves on Friday", vocab) == ["2026-11-13"]
    assert extract_dates("what is happening today", vocab) == ["2026-11-10"]
