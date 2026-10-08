"""The model-free pipeline end to end: plan, call, render, verify, redact."""

import pytest
from agent_fixtures import examples, questions, tools, vocab

from crewops.agent.pipeline import SCOPE, Pipeline
from crewops.agent.planner import false_premise, seed_calls
from crewops.agent.router import Router
from crewops.domain import Role
from crewops.tools import Caller, TraceEntry

CONTROLLER = Caller("u-ctl", frozenset({Role.CONTROLLER}))
NEEDS_A_MODEL = {"Q26", "Q30", "Q38"}
"""Gold questions no planned call answers; the model advisor handles them."""


@pytest.fixture(scope="module")
def pipeline():
    return Pipeline.offline(tools(), examples())


def ask(pipeline, text, caller=CONTROLLER):
    return pipeline.answer(text, caller)


def test_every_gold_answer_is_verified_or_says_what_it_needs(pipeline):
    for q in questions():
        answer = ask(pipeline, q["prompt"])
        if q["question_id"] in NEEDS_A_MODEL:
            assert answer.source == "clarify" and "doesn't mean all is clear" in answer.text
            continue
        assert answer.source == "template", q["question_id"]
        assert answer.verification.ok, (q["question_id"], answer.verification.summary())


@pytest.mark.parametrize(
    ("qid", "fragments"),
    [
        ("Q01", ["12 reserve duties", "D. Reddy (C-3310)", "06:00Z to 18:00Z"]),
        ("Q11", ["S. Kapoor (C-2210)"]),
        ("Q17", ["DX412, DX413 and DX588", "486 passengers"]),
        ("Q18", ["not legal", "RULE-DUTY-02 fails"]),
        ("Q21", ["RULE-REST-04 fails", "9.5h rest"]),
        ("Q22", ["RULE-CERT-06 fails"]),
        ("Q24", ["V. Menon (C-3305) is not legal"]),
        ("Q31", ["D. Reddy (C-3310)", "₹18,500"]),
        ("Q32", ["Cheapest joint plan", "Total ₹42,500"]),
        ("Q33", ["DX404 must come off this crew"]),
        ("Q36", ["acknowledge by 05:15Z"]),
        ("Q37", ["N. Verma (C-3316)", "₹18,500"]),
    ],
)
def test_gold_answers_say_the_right_things(pipeline, qid, fragments):
    prompt = next(q["prompt"] for q in questions() if q["question_id"] == qid)
    text = ask(pipeline, prompt).text
    for fragment in fragments:
        assert fragment in text, (qid, fragment, text)


def test_a_false_premise_is_asked_about_before_anything_runs(pipeline):
    answer = ask(pipeline, "FO C-1042 is sick, who covers?")
    assert answer.source == "clarify" and answer.awaiting == "confirmation"
    assert "is a Captain, not a First Officer" in answer.text and answer.trace == []
    assert "isn't one of our stations" in ask(pipeline, "Flights from BLR to XYZ").text
    assert (
        false_premise("C-2210 (DEL base) on P-2291", _ents("C-2210 (DEL base)"), tools(), vocab())
        is None
    )


def _ents(text):
    from crewops.agent.entities import extract

    return extract(text, vocab())


def test_a_wrong_id_asks_and_never_guesses(pipeline):
    answer = ask(pipeline, "Is C-1024 legal for P-2291?")
    assert answer.awaiting == "confirmation" and "Did you mean C-1042" in answer.text


def test_ambiguous_flight_asks_for_a_date(pipeline):
    answer = ask(pipeline, "Who can cover the captain on DX401?")
    assert answer.awaiting == "detail" and "Which date?" in answer.text


def test_off_topic_gets_the_scope(pipeline):
    assert ask(pipeline, "Tell me a joke").text == SCOPE


def test_crew_see_only_their_own_records(pipeline):
    me = Caller("u-crew", frozenset({Role.CREW}), crew_id="C-1042")
    mine = ask(pipeline, "How many duty hours do I have?", me)
    assert mine.verification.ok and "A. Nair (C-1042)" in mine.text
    other = ask(pipeline, "How many duty hours does C-3310 have?", me)
    assert other.text == "That isn't available to your role."
    assert "D. Reddy" not in other.text
    roster = ask(pipeline, "Who is on reserve at BLR on 15 Sep?", me)
    assert "Your role doesn't give access" in roster.text and roster.trace == []


UNPLANNED = "Which single flight leg has the most seats at risk if cancelled, and why?"
"""No planned call answers this one, so the drafter is consulted."""
SEATS = {"entity": "flights", "sort_by": "seats", "sort_desc": True, "limit": 3}


def model_pipeline(drafter):
    return Pipeline(tools(), Router(vocab(), examples()), drafter=drafter)


def test_a_verified_model_draft_is_used():
    def drafter(question, route, trace, run, allowed):
        run("lookup", SEATS)
        return "DX401 on 2026-09-14 has the most seats at risk: 162."

    answer = model_pipeline(drafter).answer(UNPLANNED, CONTROLLER)
    assert answer.source == "model" and answer.verification.ok


def test_an_unverified_draft_falls_back():
    def drafter(question, route, trace, run, allowed):
        run("lookup", SEATS)
        return "DX999 has 400 seats at risk."

    answer = model_pipeline(drafter).answer(UNPLANNED, CONTROLLER)
    assert answer.source == "template" and "DX999" not in answer.text
    assert "model draft rejected" in answer.notes[0]


def test_a_failing_model_never_costs_the_answer():
    def drafter(question, route, trace, run, allowed):
        raise TimeoutError

    answer = model_pipeline(drafter).answer(UNPLANNED, CONTROLLER)
    assert answer.source == "clarify" and "TimeoutError" in answer.notes[0]


def test_drafter_tool_calls_count_as_evidence():
    def drafter(question, route, trace, run, allowed):
        extra = run("duty_clock", {"crew_id": "C-3310"})
        return f"C-3310 has {extra.result['duty_hours_7d']} duty hours this week."

    answer = model_pipeline(drafter).answer(UNPLANNED, CONTROLLER)
    assert answer.source == "model" and answer.trace[-1].tool == "duty_clock"


def test_answer_record_is_redacted():
    from crewops.agent.pipeline import Answer

    answer = Answer("q", "Call ops on +91 98765 43210.", None, notes=["mail a@b.example"])
    record = str(answer.to_dict())
    assert "98765" not in record and "a@b.example" not in record


def test_empty_and_long_questions(pipeline):
    assert ask(pipeline, "  ").awaiting == "detail"
    assert ask(pipeline, "x" * 1001).source == "refused"


def test_planned_calls_for_a_cover_question_on_someone_else():
    route = Router(vocab(), examples()).route("Can reserve C-3305 cover P-2291?")
    calls = seed_calls(route, "Can reserve C-3305 cover P-2291?", vocab(), tools())
    assert [c.name for c in calls] == ["check_legality"]


def test_template_handles_a_failed_call():
    from crewops.agent.render import render

    failed = TraceEntry("duty_clock", {}, error_code="UNRESOLVED_ENTITY", error="no crew C-9")
    assert render([failed]) == "no crew C-9"


# -- Regression tests for review findings ----------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Is FO C-2087 on P-2291?",
        "Is C-2087 (DEL base) on anything?",
        "C-2087 is ATR72-rated, right?",
    ],
)
def test_premise_check_reveals_nothing_to_crew_about_others(pipeline, question):
    me = Caller("u-crew", frozenset({Role.CREW}), crew_id="C-1042")
    text = ask(pipeline, question, me).text
    assert "Captain" not in text and "BLR" not in text and "A320" not in text
    reporter = Caller("u-rep", frozenset({Role.REPORTER}))
    assert "Captain" not in ask(pipeline, question, reporter).text


def test_premise_check_still_covers_the_crew_members_own_record(pipeline):
    me = Caller("u-crew", frozenset({Role.CREW}), crew_id="C-1042")
    assert "is a Captain, not a First Officer" in ask(pipeline, "Am I FO C-1042?", me).text


@pytest.mark.parametrize(
    "question", ["My duty hours?", "What are my duty hours on Friday?", "Am I legal for P-2291?"]
)
def test_first_person_crew_questions(pipeline, question):
    me = Caller("u-crew", frozenset({Role.CREW}), crew_id="C-1042")
    answer = ask(pipeline, question, me)
    assert "C-1042" in answer.text and answer.verification.ok, answer.text


def test_drafter_cannot_add_unrecorded_evidence():
    seen = {}

    def drafter(question, route, trace, run, allowed):
        seen["forbidden"] = run("lookup", {"entity": "crew"}).error_code
        return "C-1042 is sick."

    me = Caller("u-crew", frozenset({Role.CREW}), crew_id="C-1042")
    p = Pipeline(tools(), Router(vocab(), examples()), drafter=drafter)
    p.answer("Which crew have 45 or more duty hours in the 7 days ending 2026-09-15?", me)
    assert seen["forbidden"] == "FORBIDDEN"


def test_controller_profile_answer_verifies(pipeline):
    answer = ask(pipeline, "Show C-1042 duty hours, certifications and risk")
    assert answer.verification.ok, answer.verification.summary()


def test_every_paraphrase_and_off_topic_answer_verifies(pipeline):
    from agent_fixtures import paraphrases

    data = paraphrases()
    for item in data["paraphrases"] + data["off_topic"]:
        answer = ask(pipeline, item["prompt"])
        if answer.source == "template":
            assert answer.verification.ok, (item["id"], answer.verification.summary())


@pytest.mark.parametrize("question", ["Play some relaxing music.", "Set an alarm for six."])
def test_off_topic_never_looks_up_people(pipeline, question):
    answer = ask(pipeline, question)
    assert answer.text == SCOPE and answer.trace == []


def test_second_round_planner_findings(pipeline):
    reserves = ask(pipeline, "Who is on reserve at BLR between 15 and 17 Sep?")
    assert reserves.trace[0].args["filters"]["dates"] == {"gte": "2026-09-15", "lte": "2026-09-17"}
    seat = ask(pipeline, "Who is legal for the Captain seat on P-2291?")
    assert seat.trace[0].tool == "find_options" and "D. Reddy (C-3310)" in seat.text
    atr = ask(pipeline, "List ATR 72 first officers based at BLR")
    assert atr.trace[0].args["filters"]["ratings"] == "ATR72"
    me = Caller("u-crew", frozenset({Role.CREW}), crew_id="C-1042")
    friday = ask(pipeline, "What are my duty hours on Friday?", me)
    assert "as of 2026-09-18" in friday.text
    assert (
        "isn't one of our stations" not in ask(pipeline, "Flights in GMT on 16 Sep from BLR").text
    )


@pytest.mark.parametrize(
    "question", ["How much is a ticket from Delhi to London?", "Book me a hotel room in Mumbai"]
)
def test_unrecognised_questions_with_a_city_get_the_scope(pipeline, question):
    answer = ask(pipeline, question)
    assert answer.text == SCOPE and answer.trace == []


def test_third_round_routing(pipeline):
    assert "Which seat needs cover on DX412" in ask(pipeline, "Who covers DX412 on 16 Sep?").text
    assert ask(pipeline, "Did DX412 fly on Monday?").trace[0].tool == "lookup"
    assert (
        ask(pipeline, "Show flights in GMT on 16 Sep from BLR").trace[0].args["entity"] == "flights"
    )
    assert "A. Nair (C-1042)" in ask(pipeline, "C-1042?").text


def test_the_model_never_picks_the_seat_to_cover():
    called = []

    def drafter(question, route, trace, run, allowed):
        called.append(question)
        return "The cheapest legal option for the Captain seat is C-3310."

    answer = model_pipeline(drafter).answer("Who can cover P-2291 on 15 Sep?", CONTROLLER)
    assert answer.source == "clarify" and answer.awaiting == "detail"
    assert answer.text.startswith("Which seat needs cover on P-2291") and not called


def test_cover_options_read_as_a_sentence(pipeline):
    prompt = next(q["prompt"] for q in questions() if q["question_id"] == "Q31")
    text = ask(pipeline, prompt).text
    assert "the cheapest legal choice is to assign Captain D. Reddy (C-3310)" in text
    assert "Assign " not in text
