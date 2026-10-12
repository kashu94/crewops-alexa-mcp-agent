"""Make the right people's Echo chime when a disruption needs them.

Run by the notifier Lambda on the table's stream, after the console and email alerts (a
stream should have no more than two readers, and it already has the analysis and the
notifier):

* analysed (REPORTED -> OPTIONS_READY) or sent back (SUBMITTED -> OPTIONS_READY): the
  controller of the desk it was assigned;
* a plan submitted (OPTIONS_READY -> SUBMITTED): every approver.

Each gets the chime, and a message that makes the skill set a reminder saying what happened,
so the Echo reads the alert out a few seconds later (``crewops.alexa.proactive``).

Only Alexa accounts linked to such a person, that allowed notifications, are sent one. A
failure to notify is logged, not retried: the console and email alerts carry the same news,
and a repeated chime would be worse than a missed one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from crewops.notify.changes import change_of

log = logging.getLogger()
log.setLevel(logging.INFO)


def desk_of(record: dict[str, Any]) -> str | None:
    from boto3.dynamodb.types import TypeDeserializer

    image = (record.get("dynamodb") or {}).get("NewImage") or {}
    analysis = image.get("analysis")
    if not analysis:
        return None
    value = TypeDeserializer().deserialize(analysis)
    return value.get("desk") if isinstance(value, dict) else None


def recipients(record: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The linked Alexa accounts this change is for."""
    change = change_of(record)
    if change is None or change.report_type == "planned_leave":
        return []  # leave is re-crewed weeks ahead: nothing to chime about
    allowed = [link for link in links if link.get("notifications") and link.get("username")]
    if change.after == "SUBMITTED":
        return [link for link in allowed if "approver" in (link.get("roles") or [])]
    if change.after == "OPTIONS_READY" and change.before in ("REPORTED", "SUBMITTED"):
        desk = desk_of(record)
        return [
            link
            for link in allowed
            if desk and "controller" in (link.get("roles") or []) and link.get("desk") == desk
        ]
    return []


def rated(record: dict[str, Any]) -> str | None:
    """The disruption's severity (high, medium or low) from the analysis in the record."""
    from boto3.dynamodb.types import TypeDeserializer

    from crewops.analysis.severity import severity

    image = (record.get("dynamodb") or {}).get("NewImage") or {}
    try:
        analysis = TypeDeserializer().deserialize(image["analysis"])
        report = TypeDeserializer().deserialize(image["report"])
        today = datetime.now(UTC).date()
        return severity(analysis, str(report.get("reported_utc") or ""), today)["level"]
    except Exception:
        return None


def announcement(record: dict[str, Any]) -> str | None:
    """The alert in words, as the Echo says it, the severity first when it matters."""
    from boto3.dynamodb.types import TypeDeserializer

    from crewops.alerts import spoken
    from crewops.analysis.severity import spoken_level
    from crewops.domain import Report
    from crewops.tools.port import describe_report

    change = change_of(record)
    if change is None:
        return None
    image = (record.get("dynamodb") or {}).get("NewImage") or {}
    try:
        report = TypeDeserializer().deserialize(image["report"])
        what = describe_report(Report.from_dict(report))
    except Exception:
        what = f"{change.report_type.replace('_', ' ')}, {change.disruption_id}"
    if change.after == "SUBMITTED":
        text = f"CrewOps: a plan for {what} is waiting for your approval."
    elif change.before == "SUBMITTED":
        text = f"CrewOps: the plan for {what} was sent back to your desk."
    else:
        text = f"CrewOps: new on your desk. {what}. Open CrewOps to plan the cover."
    level = rated(record)
    if level in ("high", "medium") and change.after == "OPTIONS_READY":
        text = text.replace("CrewOps:", f"CrewOps, {spoken_level(level).lower()}:", 1)
    return spoken(text)


def current(links: list[dict[str, Any]], profile: Any) -> list[dict[str, Any]]:
    """Each link with the person's roles and desk as they are now (the team's profiles,
    kept by provisioning), not as they were at their last request. Someone no longer on
    the team gets nothing."""
    out = []
    for link in links:
        found = profile(link.get("username") or "")
        if not found:
            continue
        out.append(link | {"roles": list(found.get("roles") or []), "desk": found.get("desk")})
    return out


def process(
    records: list[dict[str, Any]],
    links: list[dict[str, Any]],
    send: Any,
    now: datetime,
) -> int:
    """``send(link, now, text)`` notifies one account. Returns how many were sent."""
    sent = 0
    for record in records:
        text = announcement(record)
        for link in recipients(record, links):
            try:
                send(link, now, text)
                sent += 1
            except Exception:
                log.exception("Alexa notification to %s failed", link.get("username"))
    return sent


def notify(records: list[dict[str, Any]], settings: Any) -> int:
    """Send the Alexa notifications these stream records call for. Best effort."""
    import boto3

    from crewops.alexa.proactive import MESSAGING_SCOPE, Tokens, message_alert, message_skill
    from crewops.alexa.proactive import send as send_event
    from crewops.store.alexa import links
    from crewops.store.ops_store import table_for
    from crewops.store.users import get_profile

    table = table_for(settings)
    found = current(links(table), lambda u: get_profile(table, u))
    if not any(recipients(r, found) for r in records):
        return 0  # nobody to notify: no credentials fetched
    ssm = boto3.Session(region_name=settings.aws_region).client("ssm")
    prefix = settings.alexa_parameter_prefix
    client_id = ssm.get_parameter(Name=f"{prefix}/client-id")["Parameter"]["Value"]
    secret = ssm.get_parameter(Name=f"{prefix}/client-secret", WithDecryption=True)
    tokens = Tokens(client_id, secret["Parameter"]["Value"])
    messaging = Tokens(client_id, secret["Parameter"]["Value"], scope=MESSAGING_SCOPE)

    def send(link: dict[str, Any], now: datetime, text: str | None) -> None:
        send_event(
            tokens,
            link["api_endpoint"],
            message_alert(link["alexa_user_id"], now),
            settings.alexa_events_stage,
        )
        log.info("Alexa notification sent to %s", link["username"])
        if text:
            # The skill sets a reminder with these words, so the Echo says them.
            try:
                message_skill(
                    messaging,
                    link["api_endpoint"],
                    link["alexa_user_id"],
                    {"announce": text, "locale": link.get("locale") or "en-IN"},
                )
            except Exception:
                log.exception("Alexa announcement to %s failed", link["username"])

    return process(records, found, send, datetime.now(UTC))
