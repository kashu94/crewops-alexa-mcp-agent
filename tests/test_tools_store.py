"""Tools over the live table (moto): open disruptions per desk, and who is already out."""

from datetime import UTC, datetime

import pytest
from test_reports import VALID_DETAILS, report

from crewops.domain import Report, Role
from crewops.ingestion.seed import load_seed
from crewops.store.ops_store import OpsStore
from crewops.store.reports import store_analysis, store_report
from crewops.tools import Caller, CrewOpsTools, dispatch

RECEIVED = datetime(2026, 9, 15, 5, 1, tzinfo=UTC)
CONTROLLER = Caller("u-ctl", frozenset({Role.CONTROLLER}))


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture
def table(ops_table, seed):
    OpsStore(ops_table).put_seed(seed)
    return ops_table


def file(table, report_id, type_, details):
    store_report(table, Report.from_dict(report(type_, details, report_id=report_id)), RECEIVED)


def counts(tools):
    r = dispatch(tools, CONTROLLER, "controller_issue_counts", {})
    assert r.ok, r.error
    return {d["controller"]: d["disruption_ids"] for d in r.result["desks"]}, r.result


def test_open_disruptions_are_counted_at_the_desks_they_touch(table):
    file(table, "r-sick0001", "crew_unavailable", VALID_DETAILS["crew_unavailable"])  # VT-DXC
    file(table, "r-tech0001", "aircraft_technical", VALID_DETAILS["aircraft_technical"])  # DXA
    file(table, "r-stn00001", "station_disruption", VALID_DETAILS["station_disruption"])  # many
    tools = CrewOpsTools.load(table)
    by_desk, raw = counts(tools)
    assert by_desk["Ananya Iyer"] == ["r-stn00001", "r-tech0001"]
    assert by_desk["Rohit Malhotra"] == ["r-sick0001", "r-stn00001"]
    assert "r-stn00001" in by_desk["Divya Rao"]
    assert raw["unassigned"] == []
    assert raw["desks"][0]["open_issues"] == 2


def test_counts_are_read_fresh_and_closed_ones_drop_out(table):
    tools = CrewOpsTools.load(table)
    assert counts(tools)[0]["Rohit Malhotra"] == []
    file(table, "r-sick0001", "crew_unavailable", VALID_DETAILS["crew_unavailable"])
    assert counts(tools)[0]["Rohit Malhotra"] == ["r-sick0001"]  # same tools, new data
    now = datetime(2026, 9, 15, 6, tzinfo=UTC)
    store_analysis(table, "r-sick0001", {"seats": []}, now)
    assert counts(tools)[0]["Rohit Malhotra"] == ["r-sick0001"]  # OPTIONS_READY is still open


def test_a_report_touching_no_aircraft_is_unassigned(table):
    file(
        table,
        "r-sick0002",
        "crew_unavailable",
        {"crew_id": "C-3310", "from_date": "2026-09-15", "reason": "sick"},  # reserve, no pairing
    )
    by_desk, raw = counts(CrewOpsTools.load(table))
    assert raw["unassigned"] == ["r-sick0002"]
    assert not any(by_desk.values())


def test_crew_out_in_an_open_disruption_is_not_offered(table):
    file(
        table,
        "r-sick0003",
        "crew_unavailable",
        {"crew_id": "C-3310", "from_date": "2026-09-15", "reason": "sick"},
    )
    r = dispatch(CrewOpsTools.load(table), CONTROLLER, "find_options", {"crew_id": "C-1042"})
    assert r.ok and r.result["recommended"]["crew_id"] != "C-3310"
    assert "C-3310" not in [o["crew_id"] for o in r.result["options"]]
