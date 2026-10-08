"""Lambda handler: disruption status changes on the table's stream.

The event source mapping passes only disruption items; this handler keeps the records
whose status actually changed. Each change is published to the console channel, and the
ones that need someone's attention also go out as an alert to the role concerned, and an
analysed disruption to each operational team it names.

Console updates are best effort: a failure is logged and the console catches up on its
next read. An alert that cannot be sent is a batch item failure, so Lambda retries from
that record (a console event may then be sent twice, which is harmless: it only tells the
console to read again; an alert sent before the failure may also go twice).
"""

from __future__ import annotations

import logging
from typing import Any

from crewops.notify.changes import (
    CONSOLE_CHANNEL,
    alert_for,
    change_of,
    console_event,
    team_alerts,
)

log = logging.getLogger()
log.setLevel(logging.INFO)


def process(records: list[dict[str, Any]], events: Any, alerts: Any, stage: str) -> dict[str, Any]:
    """``events`` publishes to the console (or is None); ``alerts`` sends alerts (or None)."""
    changes = [(r, change_of(r)) for r in records]
    if events is not None:
        payload = [console_event(c) for _, c in changes if c is not None]
        if payload:
            try:
                events.publish(CONSOLE_CHANNEL, payload)
            except Exception:
                log.exception("console update failed for %d change(s)", len(payload))
    for record, change in changes:
        if change is None or alerts is None:
            continue
        role_alert = alert_for(change, stage)
        try:
            outgoing = [*([role_alert] if role_alert else []), *team_alerts(record, change, stage)]
            for alert in outgoing:
                alerts.publish(alert)
                log.info("alert %s sent to %s", change.disruption_id, alert.role)
        except Exception:
            sequence = record["dynamodb"]["SequenceNumber"]
            log.exception("alert for %s failed; it will be retried", change.disruption_id)
            return {"batchItemFailures": [{"itemIdentifier": sequence}]}
    return {"batchItemFailures": []}


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    import boto3

    from crewops.config import Settings
    from crewops.notify.channels import AlertPublisher, EventPublisher

    settings = Settings.from_env()
    session = boto3.Session(region_name=settings.aws_region)
    events = (
        EventPublisher(settings.events_http_domain, settings.aws_region, session.get_credentials())
        if settings.events_http_domain
        else None
    )
    alerts = (
        AlertPublisher(session.client("sns"), settings.alerts_topic_arn)
        if settings.alerts_topic_arn
        else None
    )
    return process(event.get("Records", []), events, alerts, settings.stage)
