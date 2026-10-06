"""Candidate search and cost ranking against the seed week."""

import pytest

from crewops.domain import Rank
from crewops.engine import AlreadyStarted, UnknownEntity, build_world
from crewops.engine.options import Strategy, find_options
from crewops.ingestion.seed import load_seed


@pytest.fixture(scope="module")
def world():
    return build_world(load_seed())


@pytest.fixture(scope="module")
def sick_captain(world):
    """Captain C-1042 sick on the two-day pairing P-2291."""
    return find_options(world, "P-2291", Rank.CAPTAIN)


def ranked(result):
    return [(o.crew_id, o.cost_inr, o.delay_hours) for o in result.options]


def test_ranked_by_cost_with_cancellation_last(sick_captain):
    assert ranked(sick_captain) == [
        ("C-3310", 18500, 0.0),
        ("C-1526", 24000, 0.0),
        ("C-3983", 24000, 0.0),
        ("C-5566", 24000, 0.0),
        (None, 1500000, 0.0),
    ]


def test_recommendation_and_precomputed_figures(sick_captain):
    rec = sick_captain.recommended
    assert rec.action == "Assign Captain D. Reddy (C-3310) (reserve callout)"
    assert sick_captain.next_tier_premium_inr == 5500
    assert sick_captain.equal_cost_alternatives == 0
    assert sick_captain.cancellation_multiple == 81
    assert sick_captain.cancellation.name == "Cancel all 6 flights of the pairing"


def test_positioning_option_is_costed_in_full(world):
    result = find_options(world, "P-2213", Rank.CABIN_CREW)
    pos = next(o for o in result.options if o.crew_id == "C-1622")
    assert pos.strategy is Strategy.POSITIONING
    assert pos.cost_breakdown == {"callout": 9500, "positioning": 6500, "delay": 37800}
    assert pos.positioning_flight == "DX402-2026-09-19"
    assert "positioning from DEL" in pos.action
    assert [o.strategy for o in result.strategies] == [
        Strategy.RESERVE_CALLOUT,
        Strategy.DAY_OFF_CALLOUT,
        Strategy.POSITIONING,
        Strategy.CANCEL,
    ]


def test_one_option_per_strategy(sick_captain):
    assert [o.strategy for o in sick_captain.strategies] == [
        Strategy.RESERVE_CALLOUT,
        Strategy.DAY_OFF_CALLOUT,
        Strategy.CANCEL,
    ]


def test_positioning_that_breaks_rest_is_excluded(sick_captain):
    c2210 = next(e for e in sick_captain.excluded if e.crew_id == "C-2210")
    assert c2210.stage == "rested"


def test_pairing_that_already_reported_cannot_be_recrewed(world):
    with pytest.raises(AlreadyStarted, match="P-2201 reported at 2026-09-14 01:30Z"):
        find_options(world, "P-2201", Rank.CAPTAIN)


def test_excluded_carry_stage_and_reason(sick_captain):
    by_id = {e.crew_id: e for e in sick_captain.excluded}
    assert by_id["C-2087"].stage == "within limits"
    assert by_id["C-2087"].reasons[0].startswith("would exceed 60h/7d by 1h20m")
    assert by_id["C-3315"].stage == "qualified"
    assert by_id["C-5837"].stage == "rested"


def test_funnel_adds_up(sick_captain):
    stages = sick_captain.funnel
    assert stages[0].stage == "considered" and stages[-1].stage == "legal"
    assert stages[0].remaining - sum(s.dropped for s in stages) == stages[-1].remaining
    assert stages[-1].remaining == len(sick_captain.crew_options)


def test_incumbents_never_offered(world, sick_captain):
    rostered = set(world.pairing("P-2291").holder(Rank.CAPTAIN))
    offered = {o.crew_id for o in sick_captain.options}
    excluded = {e.crew_id for e in sick_captain.excluded}
    assert not rostered & (offered | excluded)


def test_crew_already_on_the_pairing_cannot_fill_another_seat(world):
    result = find_options(world, "P-2213", Rank.CABIN_CREW)
    already_on_board = set(world.pairing("P-2213").holder(Rank.CABIN_CREW))
    assert {"C-2840", "C-4588"} <= already_on_board
    assert not already_on_board & {o.crew_id for o in result.options}
    assert result.recommended.crew_id == "C-4809"
    assert result.recommended.cost_inr == 9500


def test_seniority_and_risk_shown_but_never_ranked(world, sick_captain):
    day_off = [o for o in sick_captain.options if o.strategy is Strategy.DAY_OFF_CALLOUT]
    assert [o.crew_id for o in day_off] == sorted(o.crew_id for o in day_off)
    assert all(o.seniority is not None and o.risk_score is not None for o in day_off)


def test_policies(sick_captain):
    assert sick_captain.policies["cheapest"].crew_id == "C-3310"
    assert sick_captain.policies["least_delay"].delay_hours == 0.0
    # C-3310 is one of three captains on call at BLR that day, so using them gives up a
    # third of the reserve cover. A day-off callout keeps all of it.
    assert sick_captain.recommended.resilience == 66.7
    assert sick_captain.policies["keeps_most_reserve_cover"].strategy is Strategy.DAY_OFF_CALLOUT


def test_role_accepts_string_and_rejects_unknown(world):
    assert find_options(world, "P-2291", "Captain").recommended.crew_id == "C-3310"
    with pytest.raises(ValueError):
        find_options(world, "P-2291", "Pilot")
    with pytest.raises(UnknownEntity):
        find_options(world, "P-0000", Rank.CAPTAIN)
