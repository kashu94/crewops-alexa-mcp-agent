"""Batch ingestion: an S3 bucket for roster uploads and the Lambda that loads them.

Dropping a JSON file under ``uploads/`` triggers the loader. It validates the upload against
the current operation and writes it to the table only if there are no errors. Either way it
leaves a report under ``results/``.

The function's code is the ``crewops`` package itself. Its only dependency, boto3, ships with
the Lambda Python runtime, so no build step or Docker is needed.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from constructs import Construct

from crewops.config import Settings
from crewops.ingestion.roster_upload import RESULTS_PREFIX, UPLOADS_PREFIX

from .data_stack import DISPOSABLE_STAGES

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
RETENTION = Duration.days(90)


class IngestionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        settings: Settings,
        table: dynamodb.ITableV2,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)
        disposable = settings.stage in DISPOSABLE_STAGES

        # Bucket names are global, so the account id keeps this one unique.
        self.bucket = s3.Bucket(
            self,
            "RosterBucket",
            bucket_name=f"{settings.roster_bucket}-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(prefix=UPLOADS_PREFIX, expiration=RETENTION),
                s3.LifecycleRule(prefix=RESULTS_PREFIX, expiration=RETENTION),
            ],
            removal_policy=RemovalPolicy.DESTROY if disposable else RemovalPolicy.RETAIN,
            auto_delete_objects=disposable,
        )

        self.loader = lambda_.Function(
            self,
            "RosterLoader",
            description="Validates roster uploads and writes accepted ones to the ops table.",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="crewops.ingestion.roster_upload.handler",
            code=lambda_.Code.from_asset(
                str(SRC_DIR), exclude=["**/__pycache__", "**/__pycache__/**", "**/*.pyc"]
            ),
            memory_size=512,
            timeout=Duration.seconds(60),
            environment={"CREWOPS_STAGE": settings.stage},
            log_group=logs.LogGroup(
                self,
                "RosterLoaderLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )

        table.grant_read_write_data(self.loader)
        self.bucket.grant_read(self.loader, f"{UPLOADS_PREFIX}*")
        self.bucket.grant_put(self.loader, f"{RESULTS_PREFIX}*")
        self.bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(self.loader),
            s3.NotificationKeyFilter(prefix=UPLOADS_PREFIX, suffix=".json"),
        )

        CfnOutput(self, "RosterBucketName", value=self.bucket.bucket_name)
        CfnOutput(self, "RosterLoaderName", value=self.loader.function_name)
