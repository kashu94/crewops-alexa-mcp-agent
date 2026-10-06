"""Synthesize the ingestion stack locally (no AWS access) and check what it would create."""

import pytest

pytest.importorskip("aws_cdk")

from aws_cdk import App, Environment  # noqa: E402
from aws_cdk.assertions import Match, Template  # noqa: E402
from crewops_infra.data_stack import DataStack  # noqa: E402
from crewops_infra.ingestion_stack import IngestionStack  # noqa: E402

from crewops.config import Settings  # noqa: E402

ENV = Environment(account="111111111111", region="ap-southeast-2")


def synth(stage: str = "dev") -> Template:
    settings = Settings.from_env({"CREWOPS_STAGE": stage})
    app = App()
    data = DataStack(app, "Data", settings=settings, env=ENV)
    ingestion = IngestionStack(app, "Ingestion", settings=settings, table=data.table, env=ENV)
    return Template.from_stack(ingestion)


def test_private_encrypted_bucket_named_per_account():
    t = synth()
    t.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketName": "crewops-dev-rosters-111111111111",
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": Match.object_like({}),
        },
    )


def test_loader_function_runs_our_handler():
    synth().has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "crewops.ingestion.roster_upload.handler",
            "Runtime": "python3.12",
            "Architectures": ["arm64"],
            "Timeout": 60,
            "Environment": {"Variables": {"CREWOPS_STAGE": "dev"}},
        },
    )


def test_trigger_only_for_json_under_uploads():
    (notifications,) = synth().find_resources("Custom::S3BucketNotifications").values()
    (config,) = notifications["Properties"]["NotificationConfiguration"][
        "LambdaFunctionConfigurations"
    ]
    assert config["Events"] == ["s3:ObjectCreated:*"]
    rules = {(r["Name"], r["Value"]) for r in config["Filter"]["Key"]["FilterRules"]}
    assert rules == {("prefix", "uploads/"), ("suffix", ".json")}


def test_loader_can_use_the_table():
    statements = synth().find_resources("AWS::IAM::Policy")
    text = str(statements)
    for action in ("dynamodb:BatchWriteItem", "dynamodb:Scan", "dynamodb:PutItem"):
        assert action in text


def test_non_dev_stage_keeps_the_bucket():
    buckets = synth("demo").find_resources("AWS::S3::Bucket")
    (bucket,) = buckets.values()
    assert bucket["DeletionPolicy"] == "Retain"
