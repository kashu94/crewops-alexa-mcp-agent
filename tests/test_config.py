import pytest

from crewops.config import ConfigError, Settings


def test_defaults_without_environment():
    s = Settings.from_env({})
    assert s.stage == "dev"
    assert s.aws_region == "us-east-1"
    assert s.advisor_model_id == "us.amazon.nova-2-lite-v1:0"
    assert s.router_model_id == "us.amazon.nova-micro-v1:0"
    assert s.explainer_model_id == "us.amazon.nova-2-lite-v1:0"
    assert s.embedding_dimensions == 512
    assert s.max_tool_iterations == 8
    assert s.guardrails_enabled is False


def test_resource_names_derive_from_stage():
    s = Settings.from_env({"CREWOPS_STAGE": "demo"})
    assert s.table_name == "crewops-demo-ops"
    assert s.vector_bucket == "crewops-demo-vectors"
    assert s.ingest_queue == "crewops-demo-ingest"
    assert s.ingest_dlq == "crewops-demo-ingest-dlq"
    assert s.roster_bucket == "crewops-demo-rosters"
    assert s.alerts_topic == "crewops-demo-alerts"


def test_overrides_are_applied():
    s = Settings.from_env(
        {
            "AWS_REGION": "ap-south-1",
            "CREWOPS_ADVISOR_MODEL_ID": "us.amazon.nova-pro-v1:0",
            "CREWOPS_EMBEDDING_DIMENSIONS": "1024",
            "CREWOPS_GUARDRAIL_ID": "gr-123",
            "CREWOPS_RESPONSE_BUDGET_SECONDS": "5.5",
        }
    )
    assert s.aws_region == "ap-south-1"
    assert s.advisor_model_id == "us.amazon.nova-pro-v1:0"
    assert s.embedding_dimensions == 1024
    assert s.guardrails_enabled is True
    assert s.response_budget_seconds == 5.5


def test_falls_back_to_aws_default_region():
    assert Settings.from_env({"AWS_DEFAULT_REGION": "eu-west-1"}).aws_region == "eu-west-1"


def test_empty_values_count_as_unset():
    s = Settings.from_env({"CREWOPS_STAGE": "", "CREWOPS_GUARDRAIL_ID": "  "})
    assert s.stage == "dev"
    assert s.guardrail_id is None


@pytest.mark.parametrize("stage", ["Dev", "1dev", "d", "dev_1", "x" * 21])
def test_rejects_bad_stage(stage):
    with pytest.raises(ConfigError, match="CREWOPS_STAGE"):
        Settings.from_env({"CREWOPS_STAGE": stage})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CREWOPS_EMBEDDING_DIMENSIONS", "768"),
        ("CREWOPS_MAX_TOOL_ITERATIONS", "0"),
        ("CREWOPS_MAX_TOOL_ITERATIONS", "eight"),
        ("CREWOPS_RESPONSE_BUDGET_SECONDS", "-1"),
        ("CREWOPS_RESPONSE_BUDGET_SECONDS", "soon"),
    ],
)
def test_rejects_bad_numbers(key, value):
    with pytest.raises(ConfigError, match=key):
        Settings.from_env({key: value})


def test_settings_are_immutable():
    s = Settings.from_env({})
    with pytest.raises(AttributeError):
        s.stage = "prod"


def test_load_env_file_applies_values_without_overriding_shell(tmp_path, monkeypatch):
    from crewops.config import load_env_file

    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "\n"
        "CREWOPS_STAGE=demo\n"
        "export AWS_REGION='eu-west-1'\n"
        'CREWOPS_GUARDRAIL_ID="gr-1"\n'
        "CREWOPS_MAX_TOOL_ITERATIONS=5  # inline comment\n"
        "AWS_PROFILE=fromfile\n"
    )
    for key in (
        "CREWOPS_STAGE",
        "AWS_REGION",
        "CREWOPS_GUARDRAIL_ID",
        "CREWOPS_MAX_TOOL_ITERATIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_PROFILE", "fromshell")

    applied = load_env_file(env)

    import os

    assert os.environ["CREWOPS_STAGE"] == "demo"
    assert os.environ["AWS_REGION"] == "eu-west-1"
    assert os.environ["CREWOPS_GUARDRAIL_ID"] == "gr-1"
    assert os.environ["CREWOPS_MAX_TOOL_ITERATIONS"] == "5"
    assert os.environ["AWS_PROFILE"] == "fromshell"
    assert "AWS_PROFILE" not in applied
    assert Settings.from_env().stage == "demo"


def test_load_env_file_missing_is_fine(tmp_path):
    from crewops.config import load_env_file

    assert load_env_file(tmp_path / "nope.env") == {}


def test_load_env_file_rejects_garbage(tmp_path):
    from crewops.config import load_env_file

    bad = tmp_path / ".env"
    bad.write_text("this is not a setting\n")
    with pytest.raises(ConfigError, match=r"\.env:1"):
        load_env_file(bad)
