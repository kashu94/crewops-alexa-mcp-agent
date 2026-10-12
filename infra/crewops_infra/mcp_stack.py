"""The MCP server on AgentCore Runtime, and the rule that only it may file reports.

* **Runtime.** The code package built by ``python -m crewops.mcp.package``, on Python 3.12,
  speaking MCP (stateless streamable HTTP). No container image, so no Docker.
* **Sign-in.** AgentCore's authorizer admits only access tokens from the team's Cognito
  pool and web client, and passes the ``Authorization`` header through so the server can
  verify the token again and act as that user.
* **Memory.** An AgentCore Memory, short-term events only, kept for seven days: each
  conversation's previous turn, so a follow-up question can be read with it.
* **Permissions.** Read and write the operations table, send to the ingest queue, call the
  configured Nova and Titan models, screen with the guardrail, query the search indexes,
  write and read conversation events, and manage the alert topic's subscriptions (the
  admin-only alert tools). Nothing else.
* **Role.** A fixed name (``crewops-<stage>-mcp-runtime``): the ingest queue's policy, in
  the ingestion stack, refuses reports from every principal but this role and the operator
  ARNs listed in ``CREWOPS_QUEUE_SENDERS``.

Billed per second while a session is active; nothing while unused. Idle sessions end
after five minutes.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from crewops.config import Settings
from crewops.search.vectors import INDEXES
from crewops_infra.auth_stack import AuthStack
from crewops_infra.guardrail_stack import GuardrailStack
from crewops_infra.notify_stack import NotifyStack

PACKAGE = Path(__file__).resolve().parents[2] / "build" / "mcp_server"
GEO_PREFIXES = ("global.", "apac.", "au.", "us.", "eu.")
MEMORY_DAYS = 7
"""The shortest retention AgentCore Memory allows; a turn is only used for 15 minutes."""


def model_arns(model_id: str, region: str, account: str) -> list[str]:
    """What InvokeModel must be allowed on for one model id: the foundation model in this
    region, or, for a cross-region inference profile, the profile and the model wherever
    the profile may route."""
    if model_id.startswith(GEO_PREFIXES):
        base = model_id.split(".", 1)[1]
        return [
            f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
            f"arn:aws:bedrock:*::foundation-model/{base}",
            f"arn:aws:bedrock:::foundation-model/{base}",
        ]
    return [f"arn:aws:bedrock:{region}::foundation-model/{model_id}"]


class McpStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        settings: Settings,
        table: dynamodb.ITableV2,
        queue: sqs.IQueue,
        auth: AuthStack,
        guardrail: GuardrailStack,
        notify: NotifyStack,
        package: Path = PACKAGE,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        region, account = self.region, self.account

        # The same trust the construct gives its own role, but with a name the queue can use.
        role = iam.Role(
            self,
            "ExecutionRole",
            role_name=settings.mcp_runtime_role,
            description="Execution role for the CrewOps MCP server",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{self.partition}:bedrock-agentcore:{region}:"
                        f"{account}:runtime/{settings.mcp_runtime_name}*"
                    },
                },
            ),
            max_session_duration=Duration.hours(8),
        )
        self.memory = agentcore.Memory(
            self,
            "Conversations",
            memory_name=settings.memory_name,
            description="CrewOps conversations: each one's previous turn",
            expiration_duration=Duration.days(MEMORY_DAYS),
        )

        self.runtime = agentcore.Runtime(
            self,
            "McpServer",
            runtime_name=settings.mcp_runtime_name,
            description="CrewOps MCP server",
            execution_role=role,
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_code_asset(
                path=str(package),
                runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
                entrypoint=["main.py"],
            ),
            protocol_configuration=agentcore.ProtocolType.MCP,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                auth.user_pool, [auth.client]
            ),
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=["Authorization"]
            ),
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.minutes(5),
                max_lifetime=Duration.hours(2),
            ),
            environment_variables={
                "CREWOPS_STAGE": settings.stage,
                "CREWOPS_REAL_TIME": "1",
                "CREWOPS_USER_POOL_ID": auth.user_pool.user_pool_id,
                "CREWOPS_USER_POOL_CLIENT_ID": auth.client.user_pool_client_id,
                "CREWOPS_INGEST_QUEUE_URL": queue.queue_url,
                "CREWOPS_MEMORY_ID": self.memory.memory_id,
                "CREWOPS_ALERTS_TOPIC_ARN": notify.topic.topic_arn,
                "CREWOPS_GUARDRAIL_ID": guardrail.guardrail.attr_guardrail_id,
                "CREWOPS_GUARDRAIL_VERSION": guardrail.version.attr_version,
                "CREWOPS_ROUTER_MODEL_ID": settings.router_model_id,
                "CREWOPS_ADVISOR_MODEL_ID": settings.advisor_model_id,
                "CREWOPS_EXPLAINER_MODEL_ID": settings.explainer_model_id,
                "CREWOPS_EMBEDDING_MODEL_ID": settings.embedding_model_id,
                "CREWOPS_EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions),
                "CREWOPS_ROUTER_SEMANTIC_FLOOR": str(settings.router_semantic_floor),
                "CREWOPS_RESPONSE_BUDGET_SECONDS": str(settings.response_budget_seconds),
                "CREWOPS_MAX_TOOL_ITERATIONS": str(settings.max_tool_iterations),
            },
        )

        table.grant_read_write_data(self.runtime)
        queue.grant_send_messages(self.runtime)
        models = {
            settings.router_model_id,
            settings.advisor_model_id,
            settings.explainer_model_id,
            settings.embedding_model_id,
        }
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=sorted({a for m in models for a in model_arns(m, region, account)}),
            )
        )
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.guardrail.attr_guardrail_arn],
            )
        )
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3vectors:QueryVectors", "s3vectors:GetVectors"],
                resources=[
                    f"arn:aws:s3vectors:{region}:{account}:bucket/{settings.vector_bucket}"
                    f"/index/{name}"
                    for name in INDEXES
                ],
            )
        )

        self.memory.grant_write(self.runtime)
        self.memory.grant_read_short_term_memory(self.runtime)
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(actions=["sns:Subscribe"], resources=[notify.topic.topic_arn])
        )
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sns:Unsubscribe", "sns:GetSubscriptionAttributes"],
                # Subscription ARNs are the topic's plus an id; both forms are allowed.
                resources=[notify.topic.topic_arn, f"{notify.topic.topic_arn}:*"],
            )
        )

        CfnOutput(self, "McpRuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "MemoryId", value=self.memory.memory_id)
