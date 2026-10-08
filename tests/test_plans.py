"""Plans end to end against moto's DynamoDB: submit, contention, four eyes, re-check, commit."""

from datetime import UTC, datetime

import pytest
from test_reports import report

from crewops.analysis.runner import Operation, analyse_by_id
from crewops.domain import Rank, Report, Role
from crewops.engine import find_options
from crewops.ingestion.seed import load_seed
from crewops.plans.service import PlanError, Plans
from crewops.store import plans as store
from crewops.store.ops_store import OpsStore
from crewops.store.reports import get_disruption, store_report
from crewops.tools import Caller

AT = datetime(2026, 9, 14, 19, 0, tzinfo=UTC)
ROHIT = Caller("rohit", frozenset({Role.CONTROLLER}), desk="desk-2")  # VT-DXC/DXD
ANANYA = Caller("ananya", frozenset({Role.CONTROLLER}), desk="desk-1")
MEERA = Caller("meera", frozenset({Role.APPROVER}))
KARAN = Caller("karan", frozenset({Role.APPROVER}))
PRIYA = Caller("priya", frozenset({Role.ADMIN}))


@pytest.fixture(scope="module")
def seed():
    return load_seed()


@pytest.fixture
def table(ops_table, seed):
    OpsStore(ops_table).put_seed(seed)
    return ops_table


def disruption(table, report_id, crew_id, from_date, to_date=None):
    details = {"crew_id": crew_id, "from_date": from_date, "reason": "sick"}
    if to_date:
        details["to_date"] = to_date
    store_report(table, Report.from_dict(report(report_id=report_id, details=details)), AT)
    assert analyse_by_id(table, report_id, Operation.load(table)).outcome == "stored"


@pytest.fixture
def plans(table):
    disruption(table, "r-sick1042", "C-1042", "2026-09-15", "2026-09-16")  # P-2291, VT-DXC
    return Plans(table)  # the real clock: audit events sort after the analysis's


def crew_of(table, pairing_id):
    item = table.get_item(Key=store.pairing_key(pairing_id))["Item"]
    return {(c["role"], c["crew_id"]) for c in item["crew"]}


# -- Submitting ---------------------------------------------------------------------------


def test_controller_submits_the_recommendation(plans, table):
    out = plans.submit(ROHIT, "r-sick1042", note="reserve is closest")
    plan = out.plan
    assert (
        out.status == "SUBMITTED" and plan["submitted_by"] == "rohit" and plan["desk"] == "desk-2"
    )
    (a,) = plan["assignments"]
    assert (a["crew_id"], a["replaces"], a["cost_inr"]) == ("C-3310", "C-1042", 18500)
    assert get_disruption(table, "r-sick1042")["status"] == "SUBMITTED"
    assert [q["plan_id"] for q in plans.queue(MEERA)] == [plan["plan_id"]]
    actions = [e["action"] for e in store.audit_trail(table, "r-sick1042")]
    assert actions == ["REPORTED", "OPTIONS_READY", "SUBMITTED"]


def test_a_chosen_legal_option_is_used(plans):
    out = plans.submit(ROHIT, "r-sick1042", {"P-2291/Captain": "C-1526"})
    assert out.plan["assignments"][0]["cost_inr"] == 24000


def test_an_illegal_choice_is_refused_with_its_reason(plans):
    with pytest.raises(PlanError) as err:
        plans.submit(ROHIT, "r-sick1042", {"P-2291/Captain": "C-2087"})
    assert err.value.code == "NOT_LEGAL"
    assert "C-2087 is not a legal option" in err.value.message and "60h/7d" in err.value.message


@pytest.mark.parametrize(
    ("caller", "code"),
    [(ANANYA, "FORBIDDEN"), (MEERA, "FORBIDDEN"), (PRIYA, "FORBIDDEN")],
)
def test_only_the_desks_controller_submits(plans, caller, code):
    with pytest.raises(PlanError) as err:
        plans.submit(caller, "r-sick1042")
    assert err.value.code == code


def test_bad_requests(plans):
    with pytest.raises(PlanError, match="no disruption"):
        plans.submit(ROHIT, "r-missing1")
    with pytest.raises(PlanError, match="no such seat"):
        plans.submit(ROHIT, "r-sick1042", {"P-9999/Captain": "C-3310"})
    plans.submit(ROHIT, "r-sick1042")
    with pytest.raises(PlanError) as err:
        plans.submit(ROHIT, "r-sick1042")
    assert err.value.code == "WRONG_STATE"


# -- Deciding -----------------------------------------------------------------------------


def test_approval_commits_the_roster_in_one_step(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    out = plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    assert out.status == "COMMITTED"
    crew = crew_of(table, "P-2291")
    assert ("Captain", "C-3310") in crew and ("Captain", "C-1042") not in crew
    assert get_disruption(table, "r-sick1042")["status"] == "COMMITTED"
    actions = [e["action"] for e in store.audit_trail(table, "r-sick1042")]
    assert actions[-2:] == ["APPROVED", "COMMITTED"]
    # The roster as stored is what every later answer sees, and the sick crew stays out.
    world = Operation.load(table).world
    assert world.pairings["P-2291"].holder(Rank.CAPTAIN) == ("C-3310",)
    assert "C-1042" in world.unavailable
    with pytest.raises(PlanError) as err:
        plans.approve(KARAN, "r-sick1042", plan["plan_id"])
    assert err.value.code == "WRONG_STATE"


def test_four_eyes(plans):
    both = Caller("rohit", frozenset({Role.CONTROLLER, Role.APPROVER}), desk="desk-2")
    plan = plans.submit(both, "r-sick1042").plan
    for decide in (plans.approve, lambda c, d, p: plans.reject(c, d, p, "no")):
        with pytest.raises(PlanError) as err:
            decide(both, "r-sick1042", plan["plan_id"])
        assert err.value.code == "FOUR_EYES"
    with pytest.raises(PlanError) as err:
        plans.approve(PRIYA, "r-sick1042", plan["plan_id"])
    assert err.value.code == "FORBIDDEN"  # admins configure; they do not decide


def test_reject_sends_it_back_and_a_new_plan_can_follow(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    with pytest.raises(PlanError, match="say why"):
        plans.reject(MEERA, "r-sick1042", plan["plan_id"], "  ")
    out = plans.reject(MEERA, "r-sick1042", plan["plan_id"], "use a day-off captain")
    assert out.status == "REJECTED"
    assert get_disruption(table, "r-sick1042")["status"] == "OPTIONS_READY"
    back = [q for q in plans.queue(ROHIT) if q["kind"] == "sent_back_rejected"]
    assert back[0]["reason"] == "use a day-off captain"
    again = plans.submit(ROHIT, "r-sick1042", {"P-2291/Captain": "C-1526"})
    assert again.status == "SUBMITTED"


def test_only_the_submitter_withdraws(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    other = Caller("divya", frozenset({Role.CONTROLLER}), desk="desk-3")
    with pytest.raises(PlanError) as err:
        plans.withdraw(other, "r-sick1042", plan["plan_id"])
    assert err.value.code == "FORBIDDEN"
    assert plans.withdraw(ROHIT, "r-sick1042", plan["plan_id"]).status == "WITHDRAWN"
    assert get_disruption(table, "r-sick1042")["status"] == "OPTIONS_READY"


# -- Re-check at approval -----------------------------------------------------------------


def test_cover_reported_sick_after_submission_makes_the_plan_stale(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    disruption(table, "r-sick3310", "C-3310", "2026-09-15")
    out = plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    assert out.status == "STALE" and "C-3310 is no longer legal" in out.reasons[0]
    assert ("Captain", "C-1042") in crew_of(table, "P-2291")  # nothing written
    assert get_disruption(table, "r-sick1042")["status"] == "OPTIONS_READY"
    assert [q["kind"] for q in plans.queue(ROHIT)][-1] == "sent_back_stale"


def test_a_roster_changed_after_submission_makes_the_plan_stale(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    item = table.get_item(Key=store.pairing_key("P-2291"))["Item"]
    item["crew"] = [c for c in item["crew"] if c["role"] != "Cabin Crew"][:5] + item["crew"][-1:]
    table.put_item(Item=item)
    out = plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    assert out.status == "STALE" and "crew of P-2291 changed" in out.reasons[0]


def test_a_roster_change_between_check_and_write_is_caught(plans, table, monkeypatch):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    real = store.commit

    def racing(table_, plan_, rosters, versions, actor, at):
        item = table_.get_item(Key=store.pairing_key("P-2291"))["Item"]
        table_.put_item(Item=item | {"crew": item["crew"][:-1]})  # someone else wrote first
        real(table_, plan_, rosters, versions, actor, at)

    monkeypatch.setattr(store, "commit", racing)
    out = plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    assert out.status == "STALE"
    assert get_disruption(table, "r-sick1042")["status"] == "OPTIONS_READY"


# -- Contention ---------------------------------------------------------------------------


def test_no_contention_when_the_dates_do_not_overlap(plans, table):
    disruption(table, "r-sick5566", "C-5566", "2026-09-17", "2026-09-18")  # P-2293, 17-18 Sep
    first = plans.submit(ROHIT, "r-sick1042")  # P-2291, 15-16 Sep, also C-3310
    second = plans.submit(ROHIT, "r-sick5566")
    assert second.plan["assignments"][0]["crew_id"] == first.plan["assignments"][0]["crew_id"]
    assert first.warnings == () and second.warnings == ()


def test_approved_cover_leaves_other_options(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    world = Operation.load(table).world
    other = find_options(world, "P-2291", "First Officer")
    assert "C-3310" not in [o.crew_id for o in other.crew_options]  # a captain, and now busy


# -- The desk command ---------------------------------------------------------------------


def test_desk_command_end_to_end(plans, table, monkeypatch, capsys):
    from crewops.plans import desk
    from crewops.store.users import profile_item, put_profile

    put_profile(table, profile_item("rohit", "Rohit Malhotra", ["controller"], None, "desk-2"))
    put_profile(table, profile_item("meera", "Meera Bhatt", ["approver"], None, None))
    monkeypatch.setattr("crewops.config.load_env_file", lambda: {})
    monkeypatch.setenv("AWS_PROFILE", "crewops")
    monkeypatch.setattr("crewops.store.ops_store.table_for", lambda settings: table)

    assert desk.main(["--as", "rohit", "queue"]) == 0
    assert "[ready_for_plan] r-sick1042" in capsys.readouterr().out
    assert desk.main(["--as", "rohit", "submit", "r-sick1042"]) == 0
    plan_id = capsys.readouterr().out.split()[1].rstrip(",")
    assert desk.main(["--as", "rohit", "approve", "r-sick1042", plan_id]) == 1
    assert "FORBIDDEN" in capsys.readouterr().err
    assert desk.main(["--as", "meera", "approve", "r-sick1042", plan_id]) == 0
    assert f"{plan_id}: COMMITTED" in capsys.readouterr().out
    assert desk.main(["--as", "meera", "show", "r-sick1042"]) == 0
    shown = capsys.readouterr().out
    assert "r-sick1042: COMMITTED" in shown and "COMMITTED by meera" in shown
    with pytest.raises(SystemExit, match="No team member"):
        desk.main(["--as", "nobody", "queue"])


# -- Regression tests for review findings -------------------------------------------------

from crewops.store.reports import store_analysis  # noqa: E402


def crafted(table, report_id, seats, crew_id="C-1042"):
    """A disruption whose analysis is exactly ``seats``: for cases the seed week does not
    produce on its own."""
    details = {"crew_id": crew_id, "from_date": "2026-09-15", "reason": "sick"}
    store_report(table, Report.from_dict(report(report_id=report_id, details=details)), AT)
    store_analysis(table, report_id, {"summary": "crafted", "seats": seats}, AT)


def seat(world, pairing_id, role, crew_out, status="options", skip=()):
    out = {"pairing_id": pairing_id, "role": role, "crew_out": crew_out, "status": status}
    if status == "options":
        options = find_options(world, pairing_id, role).crew_options
        pick = next(o for o in options if o.crew_id not in skip)
        out["recommended"] = {"crew_id": pick.crew_id, "cost_inr": pick.cost_inr}
    return out


def test_a_seat_whose_crew_already_changed_is_not_covered_twice(plans, table):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    world = Operation.load(table).world
    crafted(table, "r-stale0001", [seat(world, "P-2291", "Captain", "C-1042")])
    with pytest.raises(PlanError) as err:
        plans.submit(ROHIT, "r-stale0001", {"P-2291/Captain": "C-1526"})
    assert err.value.code == "NOTHING_TO_COVER"
    assert Operation.load(table).world.pairings["P-2291"].holder(Rank.CAPTAIN) == ("C-3310",)


def test_concurrent_approvals_cannot_double_book(table):
    disruption(table, "r-sick1443", "C-1443", "2026-09-15")
    disruption(table, "r-sick1938", "C-1938", "2026-09-15")
    world = Operation.load(table).world
    common = set.intersection(
        *(
            {o.crew_id for o in find_options(world, pid, "Captain").crew_options}
            for pid in ("P-2216", "P-2209")
        )
    )
    shared = sorted(common)[0]
    ananya_plans = Plans(table)
    first = Plans(table).submit(ROHIT, "r-sick1443", {"P-2216/Captain": shared}).plan
    second = ananya_plans.submit(ANANYA, "r-sick1938", {"P-2209/Captain": shared})
    assert any(first["plan_id"] in w for w in second.warnings)

    def load_then_lose_the_race(t):
        op = Operation.load(t)  # B has read versions and the roster...
        Plans(t).approve(MEERA, "r-sick1443", first["plan_id"])  # ...then A commits first
        return op

    racing = Plans(table, load=load_then_lose_the_race)
    out = racing.approve(KARAN, "r-sick1938", second.plan["plan_id"])
    assert out.status == "STALE"
    duties = Operation.load(table).world.crew[shared].duties
    assert {d.pairing_id for d in duties} >= {"P-2216"} and "P-2209" not in {
        d.pairing_id for d in duties
    }


def test_two_seats_of_one_role_on_one_pairing(table):
    world = Operation.load(table).world
    first = seat(world, "P-2291", "Cabin Crew", "C-4395")
    second = seat(world, "P-2291", "Cabin Crew", "C-4273", skip={first["recommended"]["crew_id"]})
    crafted(table, "r-twocabin", [first, second])
    plans = Plans(table)
    with pytest.raises(PlanError, match="names several seats"):
        plans.submit(ROHIT, "r-twocabin", {"P-2291/Cabin Crew": "C-1021"})
    out = plans.submit(ROHIT, "r-twocabin")
    assert len({a["crew_id"] for a in out.plan["assignments"]}) == 2
    plans.approve(MEERA, "r-twocabin", out.plan["plan_id"])
    crew = Operation.load(table).world.pairings["P-2291"].crew
    assert len(crew) == 6 and len({s.crew_id for s in crew}) == 6
    assert not {"C-4395", "C-4273"} & {s.crew_id for s in crew}


def test_a_disruption_spanning_desks_can_be_planned_by_either(table):
    world = Operation.load(table).world
    crafted(
        table,
        "r-twodesks",
        [seat(world, "P-2291", "Captain", "C-1042"), seat(world, "P-2204", "Captain", "C-5837")],
    )
    plans = Plans(table)
    assert [q["kind"] for q in plans.queue(ANANYA)] == ["ready_for_plan"]
    out = plans.submit(ANANYA, "r-twodesks")
    assert out.status == "SUBMITTED" and len(out.plan["assignments"]) == 2


def test_uncovered_seats_must_be_acknowledged_and_a_hopeless_one_closed(table):
    world = Operation.load(table).world
    covered = seat(world, "P-2291", "Captain", "C-1042")
    hopeless = {
        "pairing_id": "P-2291",
        "role": "First Officer",
        "crew_out": "C-1694",
        "status": "no_legal_cover",
    }
    crafted(table, "r-partial1", [covered, hopeless])
    plans = Plans(table)
    with pytest.raises(PlanError) as err:
        plans.submit(ROHIT, "r-partial1")
    assert err.value.code == "UNCOVERED" and "no legal cover" in err.value.message
    out = plans.submit(ROHIT, "r-partial1", accept_uncovered=True)
    assert out.plan["uncovered"] == [
        {"seat": "P-2291/First Officer/C-1694", "reason": "no legal cover"}
    ]
    crafted(table, "r-hopeless", [hopeless])
    with pytest.raises(PlanError) as err:
        plans.submit(ROHIT, "r-hopeless")
    assert err.value.code == "NOTHING_TO_COVER"
    with pytest.raises(PlanError) as err:
        plans.close(MEERA, "r-hopeless", "cancelled")
    assert err.value.code == "FORBIDDEN"
    assert plans.close(ROHIT, "r-hopeless", "DX589 cancelled").status == "CLOSED"
    assert get_disruption(table, "r-hopeless")["status"] == "CLOSED"


def test_sent_back_plans_leave_the_queue_once_resolved(plans):
    plan = plans.submit(ROHIT, "r-sick1042").plan
    plans.reject(MEERA, "r-sick1042", plan["plan_id"], "try another")
    assert "sent_back_rejected" in [q["kind"] for q in plans.queue(ROHIT)]
    again = plans.submit(ROHIT, "r-sick1042", {"P-2291/Captain": "C-1526"}).plan
    plans.approve(MEERA, "r-sick1042", again["plan_id"])
    assert plans.queue(ROHIT) == []


def test_a_transient_clash_asks_for_a_retry(plans, monkeypatch):
    plan = plans.submit(ROHIT, "r-sick1042").plan

    def clash(*args):
        raise store.Conflict("busy", ("None", "TransactionConflict"))

    monkeypatch.setattr(store, "commit", clash)
    with pytest.raises(PlanError) as err:
        plans.approve(MEERA, "r-sick1042", plan["plan_id"])
    assert err.value.code == "RETRY"
    assert store.get_plan(plans.table, "r-sick1042", plan["plan_id"])["status"] == "SUBMITTED"


def test_a_report_taking_crew_out_moves_their_roster_version(table):
    disruption(table, "r-sick3310", "C-3310", "2026-09-15")
    assert store.read_versions(table, {"C-3310", "C-1526"}) == {"C-3310": 1, "C-1526": 0}


def test_approver_sees_contention_in_the_queue(plans, table):
    disruption(table, "r-sick5566", "C-5566", "2026-09-17", "2026-09-18")
    plans.submit(ROHIT, "r-sick1042")
    (item,) = [q for q in plans.queue(MEERA) if q["kind"] == "to_decide"]
    assert item["warnings"] == []  # P-2291 (15-16) and P-2293 (17-18) do not overlap


def test_a_disruption_with_cover_cannot_be_closed_and_closing_keeps_crew_out(plans, table):
    with pytest.raises(PlanError) as err:
        plans.close(ROHIT, "r-sick1042", "flights cancelled")
    assert err.value.code == "WRONG_STATE" and "submit a plan instead" in err.value.message
    world = Operation.load(table).world
    hopeless = {
        "pairing_id": "P-2291",
        "role": "First Officer",
        "crew_out": "C-1694",
        "status": "no_legal_cover",
    }
    crafted(table, "r-nocover1", [hopeless], crew_id="C-1694")
    plans.close(ROHIT, "r-nocover1", "DX412 cancelled")
    assert "C-1694" in Operation.load(table).world.unavailable  # still sick after closing
    assert world is not None


def test_a_plan_too_large_to_commit_is_refused(table, monkeypatch):
    from crewops.plans import service

    world = Operation.load(table).world
    crafted(table, "r-toolarge", [seat(world, "P-2291", "Captain", "C-1042")])
    monkeypatch.setattr(service, "MAX_SEATS", 0)
    with pytest.raises(PlanError) as err:
        Plans(table).submit(ROHIT, "r-toolarge")
    assert err.value.code == "TOO_LARGE"


def test_a_report_of_several_sick_crew_moves_each_ones_roster_version(table):
    from crewops.store.versions import read_versions

    details = {"crew_ids": ["C-1042", "C-1938"], "from_date": "2026-09-15", "reason": "sick"}
    doc = report("multiple_crew_unavailable", details, report_id="r-sick-both")
    store_report(table, Report.from_dict(doc), AT)
    assert read_versions(table, {"C-1042", "C-1938", "C-3310"}) == {
        "C-1042": 1,
        "C-1938": 1,
        "C-3310": 0,
    }
