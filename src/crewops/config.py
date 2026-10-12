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

DEFAULT_REGION = "ap-southeast-2"
"""Sydney: the home region of the AWS project this deploys to. Every service we use runs
there: DynamoDB, SQS, SNS, Lambda, S3 Vectors, AgentCore, Bedrock (Nova and Titan)."""

# Amazon Nova on Bedrock. These IDs work from any region that offers them, so changing
# AWS_REGION does not silently break model calls:
# - Nova Micro runs in-region (no profile prefix needed).
# - Nova 2 Lite has no in-region endpoint; the global cross-region profile is required.
# Any Bedrock Converse model with tool calling can replace these through the environment.
DEFAULT_ROUTER_MODEL = "amazon.nova-micro-v1:0"
DEFAULT_ADVISOR_MODEL = "global.amazon.nova-2-lite-v1:0"
DEFAULT_EXPLAINER_MODEL = "global.amazon.nova-2-lite-v1:0"
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


class ConfigError(ValueError):
    """A setting is present but unusable."""


ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _parse_value(value: str, where: str) -> str:
    """``"a b" # note`` -> ``a b``; ``plain # note`` -> ``plain``; ``a=b#c`` -> ``a=b#c``."""
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            raise ConfigError(f"{where}: unterminated quote in {value!r}")
        rest = value[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            raise ConfigError(f"{where}: unexpected text after the quoted value: {rest!r}")
        return value[1:end]
    if " #" in value or "\t#" in value:
        value = re.split(r"\s#", value, maxsplit=1)[0]
    return value.rstrip()


def load_env_file(path: str | os.PathLike[str] = ".env", override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` lines from a ``.env`` file into ``os.environ``.

    Blank lines and ``#`` comments are skipped, an optional ``export`` prefix is allowed,
    spaces around ``=`` and trailing whitespace (including Windows line endings) are
    ignored, a quoted value keeps its spaces, and ``value # note`` drops the note.
    Variables already set in the shell win unless ``override`` is true, so a one-off
    ``CREWOPS_STAGE=demo ...`` on the command line beats the file. A missing file is not
    an error: every setting has a default.

    Returns the values that were applied.
    """
    applied: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return applied

    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE, got {line!r}")
        key, value = match.group(1), _parse_value(match.group(2).strip(), f"{path}:{number}")
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


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


def _guardrail_version(env: Mapping[str, str]) -> str:
    """A guardrail is always pinned to a published version: the editable draft could change
    under a running service without a deploy."""
    version = _get(env, "CREWOPS_GUARDRAIL_VERSION")
    if _get(env, "CREWOPS_GUARDRAIL_ID") and not version:
        raise ConfigError(
            "CREWOPS_GUARDRAIL_VERSION must be set with CREWOPS_GUARDRAIL_ID: use the "
            "guardrail stack's GuardrailVersionNumber output"
        )
    if version and version.upper() == "DRAFT":
        raise ConfigError("CREWOPS_GUARDRAIL_VERSION must be a published version, not DRAFT")
    return version or ""


def _senders(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _get(env, "CREWOPS_QUEUE_SENDERS") or ""
    senders = tuple(s.strip() for s in raw.split(",") if s.strip())
    for arn in senders:
        if not re.match(r"^arn:aws[a-z-]*:iam::\d{12}:(user|role)/[\w+=,.@/-]+$", arn):
            raise ConfigError(f"CREWOPS_QUEUE_SENDERS: {arn!r} is not an IAM user or role ARN")
    return senders


def _redirects(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _get(env, "CREWOPS_ALEXA_REDIRECT_URLS") or ""
    urls = tuple(u.strip() for u in raw.split(",") if u.strip())
    pattern = r"^https://[a-z0-9.-]+\.amazon\.(com|co\.jp)/api/skill/link/[A-Z0-9]+$"
    for url in urls:
        if not re.match(pattern, url):
            raise ConfigError(
                f"CREWOPS_ALEXA_REDIRECT_URLS: {url!r} is not an Alexa account-linking redirect URL"
            )
    return urls


def _events_stage(env: Mapping[str, str]) -> str:
    stage = _get(env, "CREWOPS_ALEXA_EVENTS_STAGE", "development")
    if stage not in ("development", "live"):
        raise ConfigError(f"CREWOPS_ALEXA_EVENTS_STAGE must be development or live, got {stage!r}")
    return stage


def _fraction(env: Mapping[str, str], key: str, default: float) -> float:
    value = _float(env, key, default)
    if value >= 1:
        raise ConfigError(f"{key} must be below 1, got {value}")
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
    router_semantic_floor: float
    """Least cosine similarity for the router to borrow the intent of the closest example
    question. Below it, the question goes to the triage model instead. Set from measured
    paraphrases with ``python -m crewops.agent.calibrate``."""

    user_pool_id: str | None
    """The Cognito pool and app client whose access tokens the MCP server accepts. Set on
    the deployed server by the stack; read from the auth stack by the local commands."""
    user_pool_client_id: str | None
    ingest_queue_url: str | None
    """Where the MCP server sends reports."""
    queue_senders: tuple[str, ...]
    """IAM principal ARNs, besides the MCP server, allowed to send reports straight to the
    queue (an operator testing with ``send_report``). Empty: only the server."""
    memory_id: str | None
    """AgentCore Memory holding each conversation's last turns. Unset: no memory, every
    question stands on its own."""
    alerts_topic_arn: str | None
    """The SNS topic the admin subscribes alert addresses to."""
    events_http_domain: str | None
    """The AppSync Events API's HTTP domain, where live console updates are published."""
    response_budget_seconds: float
    """Time the voice path has to answer. Alexa allows 8 s; the rest is margin."""
    alexa_skill_id: str | None = None
    """The CrewOps Alexa skill's id (amzn1.ask.skill...): requests for any other skill are
    refused. Unset: no Alexa skill is deployed."""
    alexa_redirect_urls: tuple[str, ...] = ()
    """Alexa's account-linking redirect URLs, from the skill's Account Linking page. Set:
    the auth stack adds a hosted sign-in page and an app client for Alexa."""
    alexa_client_id: str | None = None
    """The Cognito app client Alexa signs controllers in with; set on the skill by its stack."""
    alexa_events_stage: str = "development"
    """Where Alexa notifications go: "development" (a skill in development, to its
    developer's own devices) or "live" (a certified skill)."""
    escalate_after_minutes: int = 10
    """How long a disruption may wait untouched on a desk before the duty supervisor is
    emailed and the speakers announce it."""
    escalation_function_arn: str | None = None
    escalation_role_arn: str | None = None
    """The escalation check and the role EventBridge Scheduler runs it with; set on the
    notifier by its stack. Unset: no escalations are scheduled."""
    push_enabled: bool = False
    """Desktop alerts (web push) from the notifier; its stack sets it when the notifier runs
    the MCP package, which carries the cryptography library push needs."""

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
    def event_api_name(self) -> str:
        """AppSync Events API pushing live updates to the console."""
        return f"{self.prefix}-events"

    @property
    def memory_name(self) -> str:
        """AgentCore Memory names allow letters, digits and underscores only."""
        return f"crewops_{self.stage.replace('-', '_')}_memory"

    @property
    def issuer(self) -> str | None:
        """The ``iss`` claim of the pool's tokens."""
        if not self.user_pool_id:
            return None
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.user_pool_id}"

    @property
    def mcp_runtime_name(self) -> str:
        """AgentCore runtime names allow letters, digits and underscores only."""
        return f"crewops_{self.stage.replace('-', '_')}_mcp"

    @property
    def mcp_runtime_role(self) -> str:
        """The MCP server's execution role. Fixed, so the ingest queue's policy can name it
        without depending on the MCP stack."""
        return f"{self.prefix}-mcp-runtime"

    @property
    def user_pool_name(self) -> str:
        """Cognito user pool: who can sign in, and their roles as groups."""
        return f"{self.prefix}-users"

    @property
    def guardrail_name(self) -> str:
        """Bedrock Guardrail screening every question and every answer."""
        return f"{self.prefix}-guardrail"

    @property
    def alexa_parameter_prefix(self) -> str:
        """Parameter Store path of the skill's Login with Amazon client id and secret, which
        send Alexa notifications. Created by hand (a SecureString), never in a file."""
        return f"/{self.prefix}/alexa"

    @property
    def push_parameter_prefix(self) -> str:
        """Parameter Store path of the web push (VAPID) keys: the private key a SecureString,
        the public key a plain parameter the web app reads."""
        return f"/{self.prefix}/push"

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
            aws_region=_get(env, "AWS_REGION") or _get(env, "AWS_DEFAULT_REGION", DEFAULT_REGION),
            router_model_id=_get(env, "CREWOPS_ROUTER_MODEL_ID", DEFAULT_ROUTER_MODEL),
            advisor_model_id=_get(env, "CREWOPS_ADVISOR_MODEL_ID", DEFAULT_ADVISOR_MODEL),
            explainer_model_id=_get(env, "CREWOPS_EXPLAINER_MODEL_ID", DEFAULT_EXPLAINER_MODEL),
            embedding_model_id=_get(env, "CREWOPS_EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL),
            embedding_dimensions=dims,
            guardrail_id=_get(env, "CREWOPS_GUARDRAIL_ID"),
            guardrail_version=_guardrail_version(env),
            max_tool_iterations=_int(env, "CREWOPS_MAX_TOOL_ITERATIONS", 8),
            router_semantic_floor=_fraction(env, "CREWOPS_ROUTER_SEMANTIC_FLOOR", 0.45),
            user_pool_id=_get(env, "CREWOPS_USER_POOL_ID"),
            user_pool_client_id=_get(env, "CREWOPS_USER_POOL_CLIENT_ID"),
            ingest_queue_url=_get(env, "CREWOPS_INGEST_QUEUE_URL"),
            queue_senders=_senders(env),
            memory_id=_get(env, "CREWOPS_MEMORY_ID"),
            alerts_topic_arn=_get(env, "CREWOPS_ALERTS_TOPIC_ARN"),
            events_http_domain=_get(env, "CREWOPS_EVENTS_HTTP_DOMAIN"),
            response_budget_seconds=_float(env, "CREWOPS_RESPONSE_BUDGET_SECONDS", 6.0),
            alexa_skill_id=_get(env, "CREWOPS_ALEXA_SKILL_ID"),
            alexa_redirect_urls=_redirects(env),
            alexa_client_id=_get(env, "CREWOPS_ALEXA_CLIENT_ID"),
            alexa_events_stage=_events_stage(env),
            escalate_after_minutes=_int(env, "CREWOPS_ESCALATE_AFTER_MINUTES", 10),
            escalation_function_arn=_get(env, "CREWOPS_ESCALATION_FUNCTION_ARN"),
            escalation_role_arn=_get(env, "CREWOPS_ESCALATION_ROLE_ARN"),
            push_enabled=_get(env, "CREWOPS_PUSH_ENABLED") == "1",
        )


def shell_exports(path: str | os.PathLike[str] = ".env") -> str:
    """``export`` lines for a POSIX shell: the ``.env`` values plus the resolved region and
    stage. ``scripts/cdk.sh`` evaluates this, so the deploy script and the Python code read
    ``.env`` with one parser and agree on every value, including defaults.
    """
    import shlex

    applied = load_env_file(path)
    settings = Settings.from_env()
    values = {**applied, "AWS_REGION": settings.aws_region, "CREWOPS_STAGE": settings.stage}
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())


if __name__ == "__main__":
    print(shell_exports())
