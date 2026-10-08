"""The web app's settings file, built from stack outputs."""

from crewops.web_env import lines


def test_settings_come_from_the_stack_outputs():
    out = lines(
        "ap-southeast-2",
        {"UserPoolClientId": "client-1"},
        {"McpRuntimeArn": "arn:aws:bedrock-agentcore:ap-southeast-2:111111111111:runtime/x"},
        {"EventsHttpDomain": "h.example", "EventsRealtimeDomain": "r.example"},
        "crewops",
    )
    assert "CREWOPS_USER_POOL_CLIENT_ID=client-1" in out
    assert "CREWOPS_EVENTS_REALTIME_DOMAIN=r.example" in out
    assert out[-1] == "AWS_PROFILE=crewops"
    assert not any("SECRET" in line or "PASSWORD" in line for line in out)
