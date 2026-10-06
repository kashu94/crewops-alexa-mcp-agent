"""Legality against the seed week, pinned to known answers."""

from datetime import date

import pytest

from crewops.engine import UnknownEntity, assess, build_world
from crewops.ingestion.seed import load_seed


@pytest.fixture(scope="module")
def world():
    return build_world(load_seed())


def details(a):
    return [v.detail for v in a.blocking]


def test_duty_clock_canary(world):
    c = world.state("C-1042")
    assert c.duty_in_window(date(2026, 9, 8), date(2026, 9, 14)) == 20.93


def test_every_rostered_crew_is_legal_except_known_cert_lapse(world):
    illegal = [
        (seat.crew_id, pid)
        for pid, p in world.pairings.items()
        for seat in p.crew
        if not assess(world, seat.crew_id, pid).legal
    ]
    assert illegal == [("C-5417", "P-2213")]


def test_cert_lapse_detail(world):
    a = assess(world, "C-5417", "P-2213")
    assert details(a) == ["recurrent_training expired 2026-09-17, invalid on 2026-09-19"]


def test_reserve_captain_is_clean_cover(world):
    a = assess(world, "C-3310", "P-2291")
    assert a.legal and a.on_reserve and a.positioning is None
    assert len(a.verdicts) == 13  # 7 rules on day one, 6 on day two


def test_duty_breach_quotes_the_overrun(world):
    a = assess(world, "C-2087", "P-2291")
    assert not a.legal
    assert details(a)[0] == "would exceed 60h/7d by 1h20m on 2026-09-15 (total 61.33h)"


def test_reserve_window_must_cover_report(world):
    a = assess(world, "C-3305", "P-2291")
    assert "on-call window 00:00-05:30Z does not cover report at 06:00Z" in details(a)
    assert not a.on_reserve


def test_positioning_from_other_base(world):
    a = assess(world, "C-2210", "P-2291")
    assert a.legal and a.on_reserve
    assert a.positioning.flight_id == "DX402-2026-09-15"
    assert a.departure_slip_hours == 3.0
    assert a.days[0].report_utc.strftime("%H:%M") == "09:00"


def test_downstream_rest_conflict(world):
    a = assess(world, "C-5837", "P-2291")
    assert details(a) == ["only 10.75h rest before P-2204 on 2026-09-17"]


def test_wrong_aircraft_type(world):
    assert details(assess(world, "C-3315", "P-2291")) == ["not rated on A320"]


def test_delay_can_breach_fdp(world):
    p = world.pairing("P-2291")
    captain = p.crew[0].crew_id
    assert assess(world, captain, "P-2291").legal
    late = assess(world, captain, "P-2291", delay_hours=4.0)
    assert any(v.rule_id == "RULE-FDP-01" for v in late.blocking)


def test_unknown_ids_raise(world):
    with pytest.raises(UnknownEntity, match="no crew 'C-1024'"):
        assess(world, "C-1024", "P-2291")
    with pytest.raises(UnknownEntity, match="no pairing"):
        assess(world, "C-1042", "P-0000")


def test_negative_delay_rejected(world):
    with pytest.raises(ValueError):
        assess(world, "C-3310", "P-2291", delay_hours=-1)
