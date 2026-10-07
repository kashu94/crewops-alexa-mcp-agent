"""From a report to a decision-ready analysis, for every report type."""

import json

import pytest

from crewops.analysis.disruption import analyse
from crewops.domain import Report
from crewops.engine import build_world
from crewops.ingestion.seed import load_seed


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture(scope="module")
def world(seed):
    return build_world(seed)


def run(world, seed, type_, details):
    report = Report.from_dict(
        {
            "report_id": "r-analysis-1",
            "type": type_,
            "reported_by": {"user_id": "u-1", "roles": ["reporter"]},
            "reported_utc": "2026-09-15T05:00:00Z",
            "details": details,
        }
    )
    analysis = analyse(world, report, seed.gates)
    json.dumps(analysis.to_dict())  # always storable and sendable as JSON
    return analysis


def test_sick_captain(world, seed):
    a = run(
        world,
        seed,
        "crew_unavailable",
        {"crew_id": "C-1042", "from_date": "2026-09-15", "reason": "sick"},
    )
    (seat,) = a.seats
    doc = seat.to_dict()
    assert (doc["pairing_id"], doc["role"], doc["crew_out"]) == ("P-2291", "Captain", "C-1042")
    assert doc["status"] == "options" and doc["recommended"]["crew_id"] == "C-3310"
    assert doc["impact"]["passengers"] == 486
    assert doc["cancellation_multiple"] == 81
    assert len(doc["options"]) <= 5 and doc["legal_options"] == 4
    assert a.joint is None
    assert "D. Reddy (C-3310)" in a.summary and "₹18,500" in a.summary


def test_unavailable_with_no_duty(world, seed):
    a = run(
        world,
        seed,
        "crew_unavailable",
        {"crew_id": "C-3310", "from_date": "2026-09-15", "reason": "sick"},
    )
    assert a.seats == () and "nothing to cover" in a.summary


def test_week_off_needs_a_joint_plan_and_skips_started_trips(world, seed):
    # C-5837 flies P-2201 (14 Sep, already flown), P-2204 (17 Sep) and P-2207 (20 Sep).
    a = run(
        world,
        seed,
        "crew_unavailable",
        {
            "crew_id": "C-5837",
            "from_date": "2026-09-14",
            "to_date": "2026-09-20",
            "reason": "personal",
        },
    )
    statuses = {s.seat.pairing_id: s.to_dict()["status"] for s in a.seats}
    assert statuses["P-2201"] == "already_started"
    assert set(statuses) == {"P-2201", "P-2204", "P-2207"}
    assert a.joint is not None
    covered = {s.pairing_id for s, _ in a.joint.assignments}
    assert covered == {"P-2204", "P-2207"}
    people = [o.crew_id for _, o in a.joint.assignments]
    assert len(set(people)) == len(people)


def test_certification_lapse(world, seed):
    a = run(
        world,
        seed,
        "certification_lapse",
        {"crew_id": "C-5417", "cert_type": "recurrent_training", "valid_to": "2026-09-17"},
    )
    (seat,) = a.seats
    assert seat.seat.pairing_id == "P-2213" and seat.options.recommended.crew_id == "C-4809"


def test_delay_beyond_the_duty_limit(world, seed):
    a = run(world, seed, "flight_delay", {"flight_id": "DX401-2026-09-16", "delay_minutes": 90})
    assert a.seats == ()
    (delay,) = a.delays
    assert delay.legs_to_drop == ("DX404-2026-09-16",)
    assert "DX404 must come off this crew" in a.summary and "₹250,000" in a.summary


def test_delay_that_breaks_overnight_rest_lists_seats(world, seed):
    a = run(world, seed, "flight_delay", {"flight_id": "DX412-2026-09-15", "delay_minutes": 180})
    assert len(a.seats) == 6
    # Whoever covers must also fly the delayed day, so the short overnight hits them too.
    assert all(s.to_dict()["status"] == "no_legal_cover" for s in a.seats)


def test_aircraft_fault_sets_delay_from_first_affected_departure(world, seed):
    a = run(
        world,
        seed,
        "aircraft_technical",
        {
            "aircraft": "VT-DXA",
            "from_utc": "2026-09-16T02:00:00Z",
            "to_utc": "2026-09-16T04:00:00Z",
        },
    )
    (delay,) = a.delays
    assert (delay.pairing_id, delay.delay_hours) == ("P-2203", 1.5)
    assert a.affected_flights == ("DX401-2026-09-16",)


def test_station_closure(world, seed):
    a = run(
        world,
        seed,
        "station_disruption",
        {
            "station": "BLR",
            "from_utc": "2026-09-17T08:00:00Z",
            "to_utc": "2026-09-17T14:00:00Z",
            "reason": "fuel-farm incident",
        },
    )
    assert len(a.affected_flights) == 13
    by_pairing = {d.pairing_id: (d.delay_hours, d.fdp_hours) for d in a.delays}
    assert by_pairing["P-2204"] == (5.75, 17.0)
    assert by_pairing["P-2232"] == (5.75, 11.0)


def test_station_closure_with_no_flights(world, seed):
    a = run(
        world,
        seed,
        "station_disruption",
        {
            "station": "GOI",
            "from_utc": "2026-09-14T19:00:00Z",
            "to_utc": "2026-09-14T20:00:00Z",
            "reason": "x",
        },
    )
    assert a.delays == () and "no rostered flights affected" in a.summary


def test_gate_change_clash_and_free_gate(world, seed):
    clash = run(world, seed, "gate_change", {"flight_id": "DX401-2026-09-15", "new_gate": "BLR-G1"})
    assert "held by DX451" in clash.summary and clash.notes
    free = run(world, seed, "gate_change", {"flight_id": "DX401-2026-09-15", "new_gate": "BLR-G9"})
    assert "the gate is free" in free.summary


def test_gate_change_for_flight_without_slot(world, seed):
    a = run(world, seed, "gate_change", {"flight_id": "DX999-2026-09-15", "new_gate": "BLR-G1"})
    assert "no boarding slot on record" in a.summary


# -- Regression tests for review findings ----------------------------------------------


def test_joint_plan_agrees_with_per_seat_answers_under_a_delay(world, seed):
    # 3h on DX412 breaks the overnight rest for every rostered crew member, and for anyone
    # who would cover them: no seat has legal cover, so the joint plan assigns nobody.
    a = run(world, seed, "flight_delay", {"flight_id": "DX412-2026-09-15", "delay_minutes": 180})
    assert len(a.seats) == 6
    assert all(s.to_dict()["status"] == "no_legal_cover" for s in a.seats)
    assert a.joint is None or a.joint.assignments == ()


def test_summary_states_pairing_cancellation_once_and_names_the_cause(world, seed):
    a = run(world, seed, "flight_delay", {"flight_id": "DX412-2026-09-15", "delay_minutes": 180})
    assert a.summary.count("cancelling P-2291") == 1
    assert "rest between 2026-09-15 and 2026-09-16" in a.summary


def test_mid_day_delay_drops_only_legs_from_the_late_flight_on(world, seed):
    # 15h on DX404 (the day's last leg): DX401-DX403 flew on time, so only DX404 goes.
    a = run(world, seed, "flight_delay", {"flight_id": "DX404-2026-09-17", "delay_minutes": 900})
    (delay,) = a.delays
    assert delay.legs_to_drop == ("DX404-2026-09-17",)
    assert delay.cancellation_cost_inr == 250_000


def test_aircraft_window_ending_seconds_after_departure_does_not_crash(world, seed):
    a = run(
        world,
        seed,
        "aircraft_technical",
        {
            "aircraft": "VT-DXA",
            "from_utc": "2026-09-16T02:00:00Z",
            "to_utc": "2026-09-16T02:30:10Z",
        },
    )
    (delay,) = a.delays
    assert delay.delay_hours == round(1 / 60, 2)  # rounded up to one minute, not to zero


def test_station_closure_slips_from_the_earliest_affected_leg(world, seed):
    a = run(
        world,
        seed,
        "station_disruption",
        {
            "station": "BLR",
            "from_utc": "2026-09-17T08:00:00Z",
            "to_utc": "2026-09-17T14:00:00Z",
            "reason": "x",
        },
    )
    p2204 = next(d for d in a.delays if d.pairing_id == "P-2204")
    # DX401 (02:30-05:15) flew before the closure, so it is kept.
    assert "DX401-2026-09-17" not in p2204.legs_to_drop


def test_stored_output_is_capped_and_counts_what_it_left_out(world, seed, monkeypatch):
    import crewops.analysis.disruption as module

    monkeypatch.setattr(module, "MAX_FLIGHTS", 5)
    a = run(
        world,
        seed,
        "station_disruption",
        {
            "station": "BLR",
            "from_utc": "2026-09-17T08:00:00Z",
            "to_utc": "2026-09-17T14:00:00Z",
            "reason": "x",
        },
    )
    doc = a.to_dict()
    assert len(doc["affected_flights"]) == 5
    assert doc["omitted"] == {"affected_flights": 8}
