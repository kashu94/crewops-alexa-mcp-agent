"""The MCP server: tokens, the caller-scoped API, and the real MCP protocol in-process."""

import json
import time

import anyio
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_plans import disruption

from crewops.agent.pipeline import Pipeline
from crewops.agent.service import example_questions
from crewops.auth.identity import AuthError
from crewops.auth.tokens import TokenVerifier, bearer
from crewops.domain import Role
from crewops.engine import build_world
from crewops.ingestion.seed import load_seed
from crewops.mcp.api import ApiError, CrewOpsApi
from crewops.mcp.server import Gate, build_server
from crewops.store.ops_store import OpsStore
from crewops.store.users import get_profile, profile_item, put_profile
from crewops.tools import Caller, CrewOpsTools

ISSUER = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_TEST"
CLIENT = "client-1"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ROHIT = Caller("rohit", frozenset({Role.CONTROLLER}), desk="desk-2")
MEERA = Caller("meera", frozenset({Role.APPROVER}))
CREW = Caller("crew1042", frozenset({Role.CREW}), crew_id="C-1042")


class Keys:
    """Stands in for the pool's published keys."""

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def token(key=KEY, **overrides):
    claims = {
        "token_use": "access",
        "iss": ISSUER,
        "client_id": CLIENT,
        "exp": int(time.time()) + 600,
        "username": "rohit",
        "cognito:groups": ["controller"],
    } | overrides
    return jwt.encode(claims, key, algorithm="RS256")


VERIFIER = TokenVerifier(ISSUER, CLIENT, keys=Keys())


# -- Tokens -------------------------------------------------------------------------------


def test_a_valid_token_gives_its_claims():
    assert VERIFIER.claims(token())["username"] == "rohit"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: token(key=OTHER_KEY), "invalid token"),
        (lambda: token(exp=int(time.time()) - 1), "invalid token"),
        (lambda: token(iss="https://elsewhere"), "invalid token"),
        (lambda: token(client_id="client-2"), "not an access token for this app client"),
        (lambda: token(token_use="id"), "not an access token"),
        (lambda: "not.a.jwt", "invalid token"),
    ],
)
def test_bad_tokens_are_refused(make, message):
    with pytest.raises(AuthError, match=message):
        VERIFIER.claims(make())


def test_bearer_header():
    assert bearer("Bearer abc") == "abc" and bearer("bearer  abc ") == "abc"
    for header in (None, "", "Basic abc", "Bearer "):
        with pytest.raises(AuthError, match="sign in first"):
            bearer(header)


# -- The caller-scoped API ----------------------------------------------------------------


class Live:
    """The answer pipeline over the seed week, rules and templates only."""

    def __init__(self, tools):
        self.pipeline = Pipeline.offline(tools, example_questions())

    def ask(self, question, caller, previous=None):
        return self.pipeline.answer(question, caller, previous), {"calls": 0}


@pytest.fixture
def api(ops_table):
    seed = load_seed()
    OpsStore(ops_table).put_seed(seed)
    disruption(ops_table, "r-sick1042", "C-1042", "2026-09-15", "2026-09-16")
    sent = []
    built = []

    def live():
        built.append(1)
        return Live(CrewOpsTools(build_world(seed), seed))

    a = CrewOpsApi(ops_table, live, sent.append)
    a.sent, a.built = sent, built
    return a


def test_ask_answers_as_the_caller(api):
    out = api.ask(ROHIT, "Who is on reserve at BLR on 15 Sep?")
    assert out["verified"] and "D. Reddy (C-3310)" in out["answer"]
    mine = api.ask(CREW, "How many duty hours do I have?")
    assert "A. Nair (C-1042)" in mine["answer"]
    assert api.ask(CREW, "How many duty hours does C-3310 have?")["answer"] == (
        "That isn't available to your role."
    )


def test_whoami_is_the_verified_caller(api):
    assert api.whoami(CREW) == {
        "user_id": "crew1042",
        "roles": ["crew"],
        "crew_id": "C-1042",
        "desk": None,
    }
    assert api.whoami(ROHIT)["desk"] == "desk-2"


def test_crew_see_their_own_roster_only(api):
    roster = api.my_roster(CREW)
    assert roster["crew_id"] == "C-1042" and roster["clock"]["crew_id"] == "C-1042"
    assert [p["pairing_id"] for p in roster["pairings"]] == ["P-2291"]
    assert roster["pairings"][0]["role"] == "Captain"
    with pytest.raises(ApiError) as err:
        api.my_roster(ROHIT)
    assert err.value.code == "FORBIDDEN"


def test_rule_thresholds_are_read_by_staff_and_changed_by_admins(api):
    admin = Caller("priya", frozenset({Role.ADMIN}))
    assert {t["param"] for t in api.thresholds(ROHIT)} >= {"min_rest_hours"}
    with pytest.raises(ApiError):
        api.thresholds(CREW)
    with pytest.raises(ApiError) as err:
        api.set_threshold(ROHIT, "RULE-REST-04", "min_rest_hours", 13, 12, "x")
    assert err.value.code == "FORBIDDEN"
    api.live()
    out = api.set_threshold(admin, "RULE-REST-04", "min_rest_hours", 13, 12, "fatigue review")
    assert out["value"] == 13 and api._live is None  # the next answer uses the new rule
    with pytest.raises(ApiError) as err:
        api.set_threshold(admin, "RULE-REST-04", "min_rest_hours", 20, 13, "x")
    assert err.value.code == "INVALID"
    assert api.activity(admin)[0]["action"] == "RULE_CHANGED"
    with pytest.raises(ApiError):
        api.activity(ROHIT)


def test_the_feed_lists_disruptions_for_staff(api):
    feed = api.disruptions(ROHIT)
    assert feed[0]["disruption_id"] == "r-sick1042" and feed[0]["status"] == "OPTIONS_READY"
    assert feed[0]["report_type"] == "crew_unavailable"
    with pytest.raises(ApiError):
        api.disruptions(CREW)


def test_a_disruption_shows_its_ranked_options(api):
    seats = api.show(ROHIT, "r-sick1042")["seats"]
    assert seats and seats[0]["pairing_id"] == "P-2291" and seats[0]["crew_out"] == "C-1042"
    best = seats[0]["options"][0]
    assert best["crew_id"] == "C-3310" and best["cost_inr"] == 18500


def test_reports_carry_the_signed_in_sender(api):
    out = api.report(
        CREW, "crew_unavailable", {"crew_id": "C-1042", "from_date": "2026-09-17", "reason": "sick"}
    )
    (sent,) = api.sent
    assert sent["report_id"] == out["report_id"] and out["status"] == "queued"
    assert sent["reported_by"] == {"user_id": "crew1042", "roles": ["crew"], "crew_id": "C-1042"}
    with pytest.raises(ApiError) as err:
        api.report(
            CREW,
            "crew_unavailable",
            {"crew_id": "C-3310", "from_date": "2026-09-17", "reason": "sick"},
        )
    assert err.value.code == "FORBIDDEN"
    with pytest.raises(ApiError) as err:
        api.report(ROHIT, "crew_unavailable", {"crew_id": "C-1042"})
    assert err.value.code == "INVALID"
    assert len(api.sent) == 1


def test_plans_through_the_api_reload_the_operation(api):
    api.ask(ROHIT, "Who is on reserve at BLR on 15 Sep?")
    submitted = api.submit(ROHIT, "r-sick1042")
    assert submitted["status"] == "SUBMITTED" and api._live is None  # reloaded next time
    with pytest.raises(ApiError) as err:
        api.approve(ROHIT, "r-sick1042", submitted["plan_id"])
    assert err.value.code == "FORBIDDEN"
    done = api.approve(MEERA, "r-sick1042", submitted["plan_id"])
    assert done["status"] == "COMMITTED"
    shown = api.show(MEERA, "r-sick1042")
    assert shown["status"] == "COMMITTED" and shown["audit"][-1]["action"] == "COMMITTED"


def test_reading_a_disruption_needs_a_role_or_being_its_reporter(api):
    with pytest.raises(ApiError) as err:
        api.show(CREW, "r-sick1042")
    assert err.value.code == "NOT_FOUND"  # the same as a missing one: ids cannot be probed
    with pytest.raises(ApiError) as err:
        api.show(ROHIT, "r-nothere1")
    assert err.value.code == "NOT_FOUND"


def test_direct_tools_follow_the_role(api):
    names = {t["name"] for t in api.list_tools(CREW)}
    assert names == {
        "duty_clock",
        "notification_brief",
        "check_legality",
        "explain_rule",
        "search_rules",
    }
    assert api.run_tool(CREW, "lookup", {"entity": "crew"})["error_code"] == "FORBIDDEN"
    out = api.run_tool(ROHIT, "find_options", {"crew_id": "C-1042"})
    assert out["ok"] and out["result"]["recommended"]["crew_id"] == "C-3310"


class Memory:
    """Stands in for AgentCore Memory: one list of turns per (actor, conversation)."""

    def __init__(self):
        self.turns = {}

    def last_turn(self, actor, conversation, now):
        turns = self.turns.get((actor, conversation))
        return turns[-1] if turns else None

    def remember(self, actor, conversation, turn, answer_text):
        self.turns.setdefault((actor, conversation), []).append(turn)


class AtOnce:
    """Runs a memory write straight away instead of after the answer is returned."""

    def submit(self, fn, *args):
        fn(*args)


def test_a_conversation_reads_a_reply_with_its_question(api):
    api.memory, api._remembering = Memory(), AtOnce()
    first = api.ask(ROHIT, "Who can cover P-2291 on 15 Sep?", "conv-1")
    assert first["awaiting"] == "detail"
    reply = api.ask(ROHIT, "Captain", "conv-1")
    assert "Captain seat on P-2291" in reply["answer"]
    assert "read together with the question it answers" in reply["notes"]


def test_conversations_are_kept_apart_by_person_and_id(api):
    api.memory, api._remembering = Memory(), AtOnce()
    api.ask(ROHIT, "Who can cover P-2291 on 15 Sep?", "conv-1")
    assert api.memory.turns.keys() == {("rohit", "conv-1")}
    other = api.ask(MEERA, "Captain", "conv-1")  # someone else, same id: nothing carried
    assert "read together" not in " ".join(other["notes"])
    no_id = api.ask(ROHIT, "Captain")
    assert "read together" not in " ".join(no_id["notes"])
    with pytest.raises(ApiError) as err:
        api.ask(ROHIT, "Captain", "not ok!")
    assert err.value.code == "INVALID"


def test_alert_tools_say_when_alerts_are_not_set_up(api):
    with pytest.raises(ApiError) as err:
        api.alert_addresses(Caller("asha", frozenset({Role.ADMIN})))
    assert err.value.code == "UNAVAILABLE"


def test_alert_tools_are_for_admins(api):
    import boto3

    from crewops.notify.subscriptions import AlertSubscriptions

    sns = boto3.client("sns", region_name="ap-southeast-2")
    topic = sns.create_topic(Name="crewops-test-alerts")["TopicArn"]
    api.alerts = AlertSubscriptions(sns, topic, api.table)
    admin = Caller("asha", frozenset({Role.ADMIN}))
    added = api.add_alert_address(admin, "ops@example.com", "approver")
    assert [a["email"] for a in api.alert_addresses(admin)] == ["ops@example.com"]
    with pytest.raises(ApiError) as err:
        api.remove_alert_address(ROHIT, added["subscription_id"])
    assert err.value.code == "FORBIDDEN"
    assert api.remove_alert_address(admin, added["subscription_id"])["status"] == "removed"


# -- The MCP protocol, in-process ---------------------------------------------------------


def mcp_call(server, headers, tool, arguments):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = server.streamable_http_app()

    async def run():
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers=headers,
        )
        async with server.session_manager.run(), http:
            transport = streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http)
            async with transport as (read, write, _), ClientSession(read, write) as session:
                await session.initialize()
                if tool is None:
                    return [t.name for t in (await session.list_tools()).tools]
                return await session.call_tool(tool, arguments)

    return anyio.run(run)


@pytest.fixture
def server(api):
    put_profile(api.table, profile_item("rohit", "Rohit Malhotra", ["controller"], None, "desk-2"))
    return build_server(api, Gate(VERIFIER, lambda u: get_profile(api.table, u)))


def test_tools_are_listed(server):
    names = mcp_call(server, {}, None, {})
    assert {"ask", "my_queue", "submit_plan", "approve_plan", "crew_tool"} <= set(names)
    assert {"alert_addresses", "add_alert_address", "remove_alert_address"} <= set(names)


def test_a_call_without_a_token_does_nothing(server):
    result = mcp_call(server, {}, "my_queue", {})
    assert result.isError and "UNAUTHORIZED" in result.content[0].text


def test_a_signed_in_call_acts_as_that_user(server):
    headers = {"authorization": f"Bearer {token()}"}
    result = mcp_call(server, headers, "my_queue", {})
    assert not result.isError
    payload = json.loads(result.content[0].text)
    assert payload["disruption_id"] == "r-sick1042" and payload["kind"] == "ready_for_plan"


def test_a_forged_token_does_nothing(server):
    forged = {"authorization": f"Bearer {token(key=OTHER_KEY)}"}
    result = mcp_call(server, forged, "my_queue", {})
    assert result.isError and "invalid token" in result.content[0].text


def test_an_unexpected_failure_reaches_the_caller_without_internals(server, api, monkeypatch):
    def broken(caller):
        raise RuntimeError("table arn:aws:dynamodb:secret-detail")

    monkeypatch.setattr(api, "queue", broken)
    headers = {"authorization": f"Bearer {token()}"}
    result = mcp_call(server, headers, "my_queue", {})
    text = result.content[0].text
    assert result.isError and "INTERNAL:" in text and "secret-detail" not in text


def test_a_failing_tool_reports_no_internals(api, monkeypatch):
    def broken(**_):
        raise RuntimeError("secret-detail")

    monkeypatch.setattr(api.live().pipeline.tools, "find_options", broken)
    out = api.run_tool(ROHIT, "find_options", {"crew_id": "C-1042"})
    assert out["error_code"] == "INTERNAL" and "secret-detail" not in out["error"]


def test_invocation_url_encodes_the_runtime_arn():
    from crewops.mcp.client import invocation_url

    arn = "arn:aws:bedrock-agentcore:ap-southeast-2:111111111111:runtime/crewops_dev_mcp-AbC"
    assert invocation_url("ap-southeast-2", arn) == (
        "https://bedrock-agentcore.ap-southeast-2.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aap-southeast-2%3A111111111111%3Aruntime%2F"
        "crewops_dev_mcp-AbC/invocations?qualifier=DEFAULT"
    )


def test_client_shows_results_as_json():
    from crewops.mcp.client import _parsed

    assert _parsed(['{"answer": "yes"}']) == {"answer": "yes"}
    assert _parsed(['{"a": 1}', '{"b": 2}']) == [{"a": 1}, {"b": 2}]
    assert _parsed(["plain words"]) == "plain words"
    assert _parsed([]) == []
    assert _parsed(['{"a": 1}'], as_list=True) == [{"a": 1}]


def test_the_client_knows_which_tools_return_lists(api):
    import inspect

    from crewops.mcp.client import LIST_TOOLS

    server = build_server(api, Gate(VERIFIER, lambda u: None))
    returns_list = {
        t.name
        for t in server._tool_manager.list_tools()
        if inspect.signature(t.fn).return_annotation in (list, "list")
    }
    assert returns_list == LIST_TOOLS


def test_packaged_entry_point_finds_its_data():
    from crewops.mcp.package import MAIN

    compile(MAIN, "main.py", "exec")
    assert "CREWOPS_DATA_DIR" in MAIN and 'main(host="0.0.0.0")' in MAIN
