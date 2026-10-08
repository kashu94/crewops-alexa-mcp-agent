"""Notifications: live console updates (AppSync Events) and admin-managed email alerts (SNS).

* **Notifier.** A Lambda on the table's change stream, filtered to disruption items. Each
  status change is published to the console channel, and the ones that need someone's
  attention are published to the alerts topic, tagged with the role they are for.
* **Live console.** An AppSync Events API. People subscribe with their Cognito sign-in, and
  the namespace's handler admits only the roles that may read disruptions. Only IAM can
  publish, and only the notifier has that right.
* **Alerts.** An SNS topic with no subscribers until an admin adds an address for a role;
  the address must confirm, and each subscription is filtered to its role.

Events and alerts name a disruption and its status, never a person. Everything here is
billed per use: nothing while no disruption changes and nobody is connected.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_appsync as appsync
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from crewops.config import Settings
from crewops.notify.changes import CONSOLE_CHANNEL
from crewops_infra.auth_stack import AuthStack

from .ingestion_stack import _code

HANDLERS = Path(__file__).with_name("console_events.js")
NAMESPACE = CONSOLE_CHANNEL.strip("/").split("/")[0]
STREAM_RETRIES = 3


class NotifyStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        settings: Settings,
        table: dynamodb.ITableV2,
        auth: AuthStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.topic = sns.Topic(
            self,
            "Alerts",
            topic_name=settings.alerts_topic,
            display_name="CrewOps alerts",
            enforce_ssl=True,
        )

        user_pool = appsync.AppSyncAuthorizationType.USER_POOL
        iam = appsync.AppSyncAuthorizationType.IAM
        self.events = appsync.EventApi(
            self,
            "ConsoleEvents",
            api_name=settings.event_api_name,
            authorization_config=appsync.EventApiAuthConfig(
                auth_providers=[
                    appsync.AppSyncAuthProvider(
                        authorization_type=user_pool,
                        cognito_config=appsync.AppSyncCognitoConfig(
                            user_pool=auth.user_pool,
                            app_id_client_regex=auth.client.user_pool_client_id,
                        ),
                    ),
                    appsync.AppSyncAuthProvider(authorization_type=iam),
                ],
                connection_auth_mode_types=[user_pool, iam],
                default_publish_auth_mode_types=[iam],
                default_subscribe_auth_mode_types=[user_pool],
            ),
        )
        self.events.add_channel_namespace(
            "Ops",
            channel_namespace_name=NAMESPACE,
            code=appsync.Code.from_inline(HANDLERS.read_text(encoding="utf-8")),
        )

        self.failed = sqs.Queue(
            self,
            "NotifierDeadLetters",
            queue_name=f"{settings.prefix}-notify-dlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )
        self.notifier = lambda_.Function(
            self,
            "Notifier",
            description="Publishes disruption status changes to the console and alerts.",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="crewops.notify.stream_handler.handler",
            code=_code(),
            memory_size=256,
            timeout=Duration.seconds(30),
            environment={
                "CREWOPS_STAGE": settings.stage,
                "CREWOPS_ALERTS_TOPIC_ARN": self.topic.topic_arn,
                "CREWOPS_EVENTS_HTTP_DOMAIN": self.events.http_dns,
            },
            log_group=logs.LogGroup(
                self,
                "NotifierLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        self.topic.grant_publish(self.notifier)
        self.events.grant_publish(self.notifier)
        self.notifier.add_event_source(
            sources.DynamoEventSource(
                table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=STREAM_RETRIES,
                bisect_batch_on_error=True,
                report_batch_item_failures=True,
                on_failure=sources.SqsDlq(self.failed),
                filters=[
                    lambda_.FilterCriteria.filter(
                        {
                            "dynamodb": {
                                "NewImage": {
                                    "entity": {"S": lambda_.FilterRule.is_equal("disruption")}
                                }
                            }
                        }
                    )
                ],
            )
        )

        CfnOutput(self, "AlertsTopicArn", value=self.topic.topic_arn)
        CfnOutput(self, "EventsHttpDomain", value=self.events.http_dns)
        CfnOutput(self, "EventsRealtimeDomain", value=self.events.realtime_dns)
        CfnOutput(self, "NotifierName", value=self.notifier.function_name)
