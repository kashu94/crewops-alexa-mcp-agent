"""Escalation: a disruption left untouched on a desk is raised with the duty supervisor.

When a disruption reaches a desk (analysed, or a plan sent back), the notifier creates a
one-time EventBridge schedule for ``CREWOPS_ESCALATE_AFTER_MINUTES`` later, which deletes
itself once it has run. When it fires, this Lambda looks again: if the disruption is still
waiting on that desk with nothing done since (no plan submitted, not closed, not sent back
again), it emails the duty supervisor (the "duty_supervisor" alert audience the admin
subscribes an address to) and records an ESCALATED event, which the desk and room speakers
announce. Anything else means it was handled, and nothing happens.

No timer runs while nothing happens: a check exists only for a disruption that reached a
desk, and only until it has run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from crewops.notify.changes import Alert, Change

log = logging.getLogger()
log.setLevel(logging.INFO)

AUDIENCE = "duty_supervisor"
ACTOR = "system:escalation"
STARTS_CLOCK = (("REPORTED", "OPTIONS_READY"), ("SUBMITTED", "OPTIONS_READY"))
"""Changes that leave a disruption waiting on a desk."""


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def schedule_name(prefix: str, disruption_id: str, at: datetime) -> str:
    """A unique schedule name within EventBridge's 64 characters and character set: the
    time and a hash of the id always fit, whatever the stage and id lengths."""
    digest = hashlib.sha256(disruption_id.encode()).hexdigest()[:10]
    fixed = f"{prefix}-esc-{int(at.timestamp())}-{digest}"
    room = 64 - len(fixed) - 1
    safe = re.sub(r"[^0-9A-Za-z_.-]", "-", disruption_id)[: max(room, 0)]
    return f"{fixed}-{safe}" if safe else fixed


def schedule_check(
    scheduler: Any,
    change: Change,
    *,
    prefix: str,
    function_arn: str,
    role_arn: str,
    minutes: int,
    now: datetime,
) -> str | None:
    """A one-time check ``minutes`` from now for a disruption that just reached a desk."""
    if (change.before, change.after) not in STARTS_CLOCK:
        return None
    if change.report_type == "planned_leave":
        return None  # re-crewed weeks ahead: no ten-minute clock
    name = schedule_name(prefix, change.disruption_id, now)
    when = now + timedelta(minutes=minutes)
    scheduler.create_schedule(
        Name=name,
        ScheduleExpression=f"at({when.astimezone(UTC):%Y-%m-%dT%H:%M:%S})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": function_arn,
            "RoleArn": role_arn,
            "Input": json.dumps({"disruption_id": change.disruption_id, "since": _iso(now)}),
        },
    )
    return name


def check(
    table: Any,
    disruption_id: str,
    since: str,
    minutes: int,
    publish: Any,
    now: datetime,
    describe: Any,
    aircraft: Any = lambda _p: None,
) -> str:
    """Escalate if the disruption is still waiting, untouched since ``since``. Returns what
    happened: "escalated", "handled" or "missing"."""
    from crewops.alerts import STAGE
    from crewops.domain import Report
    from crewops.domain.desks import owning_desks
    from crewops.store import plans as plan_store
    from crewops.store.items import from_dynamo
    from crewops.store.reports import get_disruption

    item = get_disruption(table, disruption_id)
    if item is None:
        return "missing"
    if item.get("status") != "OPTIONS_READY":
        return "handled"  # a plan was submitted, or it was resolved
    trail = plan_store.audit_trail(table, disruption_id)
    # Anything after the clock started (a plan submitted and sent back, an earlier
    # escalation) means someone acted, or another check is already on its way.
    if any(
        str(e.get("at") or "") > since and STAGE.get(str(e.get("action")), 0) >= 1 for e in trail
    ):
        return "handled"
    analysis = from_dynamo(item.get("analysis") or {})
    desks = owning_desks(analysis, aircraft) if analysis else []
    on_desk = f"{desks[0].controller}'s desk ({desks[0].desk_id})" if desks else "a desk"
    what = describe(Report.from_dict(from_dynamo(item["report"])))
    # Recorded first: if the email then fails and the check is retried, the retry sees this
    # and stops, so the supervisor is never emailed twice (the speakers still announce it).
    at = _iso(now)
    table.put_item(
        Item={
            "pk": f"DISRUPTION#{disruption_id}",
            "sk": f"AUDIT#{at}#ESCALATED#",
            "entity": "audit_event",
            "disruption_id": disruption_id,
            "action": "ESCALATED",
            "actor": ACTOR,
            "at": at,
            "reason": f"untouched for {minutes} minutes on {on_desk}",
        }
    )
    publish(
        Alert(
            AUDIENCE,
            f"CrewOps: escalated, {disruption_id} untouched for {minutes} minutes"[:100],
            f"Disruption {disruption_id} ({what}) has waited {minutes} minutes on {on_desk} "
            "with no plan submitted. Check the desk is staffed, or plan it from another desk."
            "\n\nYou receive this because a CrewOps administrator subscribed this address to "
            "duty supervisor escalations.",
        )
    )
    return "escalated"


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    import boto3

    from crewops.config import Settings
    from crewops.notify.channels import AlertPublisher
    from crewops.store.ops_store import table_for
    from crewops.tools.port import describe_report

    settings = Settings.from_env()
    table = table_for(settings)
    sns = boto3.Session(region_name=settings.aws_region).client("sns")
    publisher = AlertPublisher(sns, settings.alerts_topic_arn or "")
    outcome = check(
        table,
        str(event["disruption_id"]),
        str(event["since"]),
        settings.escalate_after_minutes,
        publisher.publish,
        datetime.now(UTC),
        describe_report,
    )
    log.info("escalation check for %s: %s", event["disruption_id"], outcome)
    return {"outcome": outcome}
