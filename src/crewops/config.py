"""Runtime settings, read once from the environment.

Every AWS resource name is derived from a single stage name, so a second
stage (``dev``, ``demo``, ``prod``) never collides with the first and no
resource name is typed twice. Nothing here talks to AWS; this module only
decides *which* resources and models the rest of the package uses.

An empty environment variable counts as unset. ``CREWOPS_GUARDRAIL_ID=``
in a ``.env`` file means "no guardrail yet", not "a guardrail named ''".
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

STAGE_RE = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
"""Lowercase, starts with a letter, 2-20 chars. Resource names such as S3
vector buckets only accept lowercase letters, digits and hyphens."""

TITAN_V2_DIMENSIONS = (256, 512, 1024)
"""Output sizes Titan Text Embeddings V2 supports."""

DEFAULT_ROUTER_MODEL = "anthropic.claude-haiku-4-5"
DEFAULT_ADVISOR_MODEL = "anthropic.claude-sonnet-5-5"
DEFAULT_EXPLAINER_MODEL = "anthropic.claude-haiku-4-5"
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


class ConfigError(ValueError):
    """A setting is present but unusable."""


def _get(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 1) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{key} must be > 0, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    stage: str
    aws_region: str

    router_model_id: str
    advisor_model_id: str
    explainer_model_id: str
    embedding_model_id: str
    embedding_dimensions: int

    guardrail_id: str | None
    guardrail_version: str

    max_tool_iterations: int
    """Hard stop on the advisor's tool loop."""
    response_budget_seconds: float
    """Time the voice path has to answer. Alexa allows 8 s; the rest is margin."""

    # -- Derived resource names ---------------------------------------------

    @property
    def prefix(self) -> str:
        return f"crewops-{self.stage}"

    @property
    def table_name(self) -> str:
        """The single DynamoDB table holding the whole operation."""
        return f"{self.prefix}-ops"

    @property
    def vector_bucket(self) -> str:
        return f"{self.prefix}-vectors"

    @property
    def rules_index(self) -> str:
        return "rules"

    @property
    def intents_index(self) -> str:
        return "intents"

    @property
    def ingest_queue(self) -> str:
        """Live disruption reports land here before the worker stores them."""
        return f"{self.prefix}-ingest"

    @property
    def ingest_dlq(self) -> str:
        return f"{self.prefix}-ingest-dlq"

    @property
    def roster_bucket(self) -> str:
        """Batch roster uploads; an S3 event triggers the loader."""
        return f"{self.prefix}-rosters"

    @property
    def alerts_topic(self) -> str:
        """Outbound alerts to officers. Never an ingestion path."""
        return f"{self.prefix}-alerts"

    @property
    def guardrails_enabled(self) -> bool:
        return self.guardrail_id is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        stage = _get(env, "CREWOPS_STAGE", "dev")
        if not STAGE_RE.match(stage):
            raise ConfigError(
                f"CREWOPS_STAGE must be 2-20 lowercase letters, digits or hyphens, "
                f"starting with a letter; got {stage!r}"
            )

        dims = _int(env, "CREWOPS_EMBEDDING_DIMENSIONS", 512)
        if dims not in TITAN_V2_DIMENSIONS:
            raise ConfigError(
                f"CREWOPS_EMBEDDING_DIMENSIONS must be one of {TITAN_V2_DIMENSIONS}, got {dims}"
            )

        return cls(
            stage=stage,
            aws_region=_get(env, "AWS_REGION") or _get(env, "AWS_DEFAULT_REGION", "us-east-1"),
            router_model_id=_get(env, "CREWOPS_ROUTER_MODEL_ID", DEFAULT_ROUTER_MODEL),
            advisor_model_id=_get(env, "CREWOPS_ADVISOR_MODEL_ID", DEFAULT_ADVISOR_MODEL),
            explainer_model_id=_get(env, "CREWOPS_EXPLAINER_MODEL_ID", DEFAULT_EXPLAINER_MODEL),
            embedding_model_id=_get(env, "CREWOPS_EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL),
            embedding_dimensions=dims,
            guardrail_id=_get(env, "CREWOPS_GUARDRAIL_ID"),
            guardrail_version=_get(env, "CREWOPS_GUARDRAIL_VERSION", "DRAFT"),
            max_tool_iterations=_int(env, "CREWOPS_MAX_TOOL_ITERATIONS", 8),
            response_budget_seconds=_float(env, "CREWOPS_RESPONSE_BUDGET_SECONDS", 6.0),
        )
