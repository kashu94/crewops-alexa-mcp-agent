"""Blast radius, delay effects and joint plans, pinned to the seed week's known answers."""

from datetime import date

import pytest

from crewops.domain import Rank
from crewops.engine import build_world
from crewops.engine.impact import Seat, delay_effect, joint_plan, pairing_impact
from crewops.ingestion.seed import load_seed


@pytest.fixture(scope="module")
def world():
    return build_world(load_seed())


def test_impact_of_losing_day_one_of_a_two_day_trip(world):
    impact = pairing_impact(world, "P-2291", {date(2026, 9, 15)})
    assert impact.uncovered_flights == ("DX412-2026-09-15", "DX413-2026-09-15", "DX588-2026-09-15")
    assert impact.at_risk_flights == ("DX589-2026-09-16", "DX590-2026-09-16", "DX591-2026-09-16")
    assert impact.passengers == 486


def test_impact_outside_the_trip_is_empty(world):
    impact = pairing_impact(world, "P-2291", {date(2026, 9, 20)})
    assert impact.uncovered_flights == () and impact.passengers == 0


def test_whole_trip_impact_by_default(world):
    assert len(pairing_impact(world, "P-2291").uncovered_flights) == 6


def test_delay_that_breaks_the_duty_limit_for_anyone(world):
    # VT-DXA, 16 Sep, 90 minutes before DX401: the four-leg day runs 12.75h vs 12.0h.
    e = delay_effect(world, "P-2203", date(2026, 9, 16), 1.5)
    assert (e.fdp_hours, e.fdp_limit, e.duty_breaches_limit) == (12.75, 12.0, True)
    assert e.legs_to_drop == ("DX404-2026-09-16",)
    assert e.cancellation_cost_inr == 250_000
    assert len(e.breaches) == 6
    assert all("FDP 12.75h exceeds 12.0h" in b.reasons[0] for b in e.breaches)


def test_delay_within_the_duty_limit_can_still_break_overnight_rest(world):
    # Day one of P-2291 released 3h later leaves 9.5h before day two's 04:00 report.
    e = delay_effect(world, "P-2291", date(2026, 9, 15), 3.0)
    assert not e.duty_breaches_limit and e.legs_to_drop == ()
    assert len(e.breaches) == 6
    assert any("9.5h rest between" in r for b in e.breaches for r in b.reasons)


def test_small_delay_changes_nothing(world):
    e = delay_effect(world, "P-2291", date(2026, 9, 15), 0.25)
    assert e.breaches == () and not e.duty_breaches_limit


@pytest.mark.parametrize(
    ("pairing_id", "hours", "fdp", "limit"),
    [
        # BLR closed 08:00-14:00 on 17 Sep, reopening + 30 min turnaround.
        ("P-2204", 5.75, 17.0, 12.0),
        ("P-2211", 5.75, 17.0, 12.0),
        ("P-2218", 6.0, 15.75, 12.0),
        ("P-2225", 6.5, 14.75, 12.0),
        ("P-2232", 5.75, 11.0, 13.0),
        ("P-2293", 3.25, 12.75, 12.5),
    ],
)
def test_station_closure_delays(world, pairing_id, hours, fdp, limit):
    e = delay_effect(world, pairing_id, date(2026, 9, 17), hours)
    assert (e.fdp_hours, e.fdp_limit) == (fdp, limit)
    assert e.duty_breaches_limit == (fdp > limit)


def test_delay_rejects_bad_input(world):
    with pytest.raises(ValueError, match="positive"):
        delay_effect(world, "P-2291", date(2026, 9, 15), 0)
    with pytest.raises(ValueError, match="no duty day"):
        delay_effect(world, "P-2291", date(2026, 9, 20), 1.0)


def test_joint_plan_for_two_simultaneous_captain_sick_calls(world):
    plan = joint_plan(world, [Seat("P-2205", Rank.CAPTAIN), Seat("P-2212", Rank.CAPTAIN)])
    assert plan.exhaustive and plan.unfilled == ()
    assert plan.total_cost_inr == 42_500
    assert plan.equal_cost_alternatives == 19  # 20 equally cheap plans in all
    crew = [o.crew_id for _, o in plan.assignments]
    assert len(set(crew)) == 2


def test_joint_plan_never_gives_one_person_two_seats(world, monkeypatch):
    import crewops.engine.impact as impact

    monkeypatch.setattr(impact, "JOINT_EXHAUSTIVE_LIMIT", 0)  # force the greedy path
    plan = impact.joint_plan(world, [Seat("P-2205", Rank.CAPTAIN), Seat("P-2212", Rank.CAPTAIN)])
    assert not plan.exhaustive
    crew = [o.crew_id for _, o in plan.assignments]
    assert len(crew) == 2 and len(set(crew)) == 2


def test_joint_plan_lists_seats_that_cannot_be_filled(world):
    plan = joint_plan(world, [Seat("P-2201", Rank.CAPTAIN), Seat("P-2291", Rank.CAPTAIN)])
    assert Seat("P-2201", Rank.CAPTAIN) in plan.unfilled  # already flown before the snapshot
    assert [s.pairing_id for s, _ in plan.assignments] == ["P-2291"]


def test_identical_seats_are_each_filled_by_different_people(world):
    seats = [Seat("P-2204", Rank.CABIN_CREW)] * 3
    plan = joint_plan(world, seats)
    assert len(plan.assignments) == 3 and plan.unfilled == ()
    people = [o.crew_id for _, o in plan.assignments]
    assert len(set(people)) == 3


def test_when_no_plan_fills_every_seat_as_many_as_possible_are_covered(world):
    from crewops.engine.options import find_options

    only_one = find_options(world, "P-2291", Rank.CAPTAIN)
    single = type(only_one)(  # an options result offering one and the same captain twice
        pairing_id=only_one.pairing_id,
        role=only_one.role,
        options=(only_one.options[0], only_one.cancellation),
        strategies=only_one.strategies,
        excluded=only_one.excluded,
        funnel=only_one.funnel,
    )
    seats = [Seat("P-2291", Rank.CAPTAIN), Seat("P-2291", Rank.CAPTAIN)]
    plan = joint_plan(world, seats, [single, single])
    assert len(plan.assignments) == 1 and len(plan.unfilled) == 1
    assert not plan.exhaustive


def test_delay_from_a_later_leg_keeps_the_legs_before_it(world):
    e = delay_effect(world, "P-2204", date(2026, 9, 17), 15.0, from_flight="DX404-2026-09-17")
    assert e.legs_to_drop == ("DX404-2026-09-17",)
