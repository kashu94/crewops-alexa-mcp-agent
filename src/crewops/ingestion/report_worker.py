"""Lambda handler: live reports arriving on the SQS ingest queue.

For each message:

1. parse and validate the report (``crewops.domain.reports``);
2. if its id is already stored: the same content is a duplicate delivery (done), different
   content is an id conflict (rejected);
3. check its time is not in the future, and that the roles it states may file it;
4. check every id it names exists in the operation, so nothing is guessed;
5. store it as a REPORTED disruption, exactly once.

A report refused at any step would be refused the same way on every retry, so it is
recorded as a rejection and the message is consumed. Rejection records are bounded in size
and keyed safely, so recording one cannot itself fail on bad input. Only unexpected
failures (DynamoDB unavailable, for example) are returned as batch item failures: SQS
redelivers just those messages, and after the queue's maximum receive count they move to
the dead-letter queue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key

from crewops.config import Settings
from crewops.domain import DataError
from crewops.domain.reports import (
    REPORT_ID_RE,
    Report,
    ReportType,
    authorize,
    check_timing,
    crew_out_of,
)
from crewops.store import schema
from crewops.store.reports import (
    MAX_STORED_REASON,
    StoreOutcome,
    get_disruption,
    same_report,
    store_rejection,
    store_report,
)
from crewops.store.schema import SEPARATOR as S

log = logging.getLogger()
log.setLevel(logging.INFO)


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    """stored, duplicate or rejected."""
    report_id: str | None
    reason: str = ""


def _exists(table: Any, pk: str, sk: str) -> bool:
    found = table.get_item(
        Key={schema.PK: pk, schema.SK: sk},
        ProjectionExpression="#pk",
        ExpressionAttributeNames={"#pk": schema.PK},
    )
    return "Item" in found


def check_references(table: Any, report: Report) -> str | None:
    """None if every id the report names exists, otherwise what is missing."""
    d = report.details
    kind = report.report_type
    for crew_id in crew_out_of(kind, d):
        if not _exists(table, f"CREW{S}{crew_id}", "PROFILE"):
            return f"unknown crew {crew_id}"
    if kind is ReportType.CERTIFICATION_LAPSE and not _exists(
        table, f"CREW{S}{d['crew_id']}", f"CERT{S}{d['cert_type']}"
    ):
        return f"{d['crew_id']} has no {d['cert_type']} certification on record"
    if kind in (ReportType.FLIGHT_DELAY, ReportType.GATE_CHANGE) and not _exists(
        table, f"FLIGHT{S}{d['flight_id']}", "META"
    ):
        return f"unknown flight {d['flight_id']}"
    if kind is ReportType.GATE_CLOSURE:
        found = table.query(
            KeyConditionExpression=Key(schema.PK).eq(f"GATE{S}{d['gate']}"), Limit=1
        )
        if not found.get("Items"):
            return f"unknown gate {d['gate']}"
    if kind is ReportType.AIRCRAFT_TECHNICAL:
        found = table.query(
            IndexName=schema.GSI1,
            KeyConditionExpression=Key(schema.GSI1_PK).eq(f"AIRCRAFT{S}{d['aircraft']}"),
            Limit=1,
        )
        if not found.get("Items"):
            return f"unknown aircraft {d['aircraft']}"
    return None


def handle_message(table: Any, body: str, received: datetime, message_id: str = "local") -> Outcome:
    """Decide and store one message. Raises only for unexpected (retryable) failures."""

    def reject(report_id: str | None, reason: str) -> Outcome:
        reason = reason[:MAX_STORED_REASON]
        store_rejection(table, report_id, reason, body, received, message_id)
        log.warning("rejected report %s: %s", report_id, reason)
        return Outcome("rejected", report_id, reason)

    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        return reject(None, f"not valid JSON: {exc}")
    raw_id = doc.get("report_id") if isinstance(doc, dict) else None
    claimed_id = raw_id if isinstance(raw_id, str) and REPORT_ID_RE.match(raw_id) else None

    try:
        report = Report.from_dict(doc)
    except DataError as exc:
        return reject(claimed_id, str(exc))

    # A resend of a report already stored is answered from the stored copy, before any
    # check whose outcome could have changed since (a crew member removed, say). An id
    # reused for different content is refused rather than silently dropped.
    if existing := get_disruption(table, report.report_id):
        return _resend(existing, report, reject)

    for check in (check_timing(report, received), authorize(report)):
        if check:
            return reject(report.report_id, check)
    if reason := check_references(table, report):
        return reject(report.report_id, reason)

    outcome = store_report(table, report, received)
    if outcome is StoreOutcome.DUPLICATE:
        # Another delivery stored it between our read and our write.
        return _resend(get_disruption(table, report.report_id) or {}, report, reject)
    log.info("report %s %s", report.report_id, outcome)
    return Outcome(str(outcome), report.report_id)


def _resend(existing: dict[str, Any], report: Report, reject) -> Outcome:
    if same_report(existing, report):
        log.info("report %s duplicate", report.report_id)
        return Outcome(str(StoreOutcome.DUPLICATE), report.report_id)
    return reject(report.report_id, "report id already used for a different report")


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    from crewops.store.ops_store import table_for

    table = table_for(Settings.from_env())
    failures = []
    for record in event.get("Records", []):
        try:
            handle_message(table, record["body"], datetime.now(UTC), record["messageId"])
        except Exception:
            log.exception("could not process message %s; it will be retried", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
