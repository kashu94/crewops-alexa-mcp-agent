"""Store live reports as disruptions, exactly once.

A report becomes a disruption item (status REPORTED) plus its first audit event, written in
one transaction that only succeeds if the disruption id is new. A message delivered twice
(SQS guarantees at-least-once delivery) therefore lands once; the second write is refused
by the condition and reported as a duplicate, not an error.

Rejected reports are recorded separately so an admin can see what was refused and why.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from crewops.domain.reports import REPORT_ID_RE, Report
from crewops.store import schema
from crewops.store.items import from_dynamo, to_dynamo
from crewops.store.schema import SEPARATOR as S
from crewops.store.versions import bump_version

STATUS_REPORTED = "REPORTED"
TAKES_CREW_OUT = frozenset({"crew_unavailable", "certification_lapse"})


class StoreOutcome(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def disruption_pk(report_id: str) -> str:
    return f"DISRUPTION{S}{report_id}"


def disruption_item(report: Report, received: datetime) -> dict[str, Any]:
    reported = _iso(report.reported_utc)
    return {
        schema.PK: disruption_pk(report.report_id),
        schema.SK: "META",
        schema.GSI1_PK: f"STATUS{S}{STATUS_REPORTED}",
        schema.GSI1_SK: f"AT{S}{reported}{S}{report.report_id}",
        "entity": "disruption",
        "disruption_id": report.report_id,
        "status": STATUS_REPORTED,
        "report": report.to_dict(),
        "received_utc": _iso(received),
    }


def audit_item(report: Report, received: datetime) -> dict[str, Any]:
    at = _iso(received)
    return {
        schema.PK: disruption_pk(report.report_id),
        schema.SK: f"AUDIT{S}{at}{S}{STATUS_REPORTED}",
        "entity": "audit_event",
        "disruption_id": report.report_id,
        "action": STATUS_REPORTED,
        "actor": report.reported_by.user_id,
        "at": at,
    }


def store_report(table: Any, report: Report, received: datetime) -> StoreOutcome:
    """Write the disruption and its audit event together, only if the id is new.

    Uses the table resource's client, which converts plain Python values itself, so items
    are passed as plain dicts (no floats: report details hold only strings and integers).

    A report that takes a crew member out (unavailable, or a lapsed certificate) also moves
    their roster version on, so a plan being approved for them at the same moment is refused
    and checked again rather than committed on a roster that no longer holds.
    """
    out = report.details.get("crew_id") if report.report_type.value in TAKES_CREW_OUT else None
    bump = []
    if out:
        update = bump_version(out)["Update"]
        bump = [{"Update": {"TableName": table.name, **update}}]
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                *bump,
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": disruption_item(report, received),
                        "ConditionExpression": "attribute_not_exists(#pk)",
                        "ExpressionAttributeNames": {"#pk": schema.PK},
                    }
                },
                {"Put": {"TableName": table.name, "Item": audit_item(report, received)}},
            ]
        )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        reasons = exc.response.get("CancellationReasons") or []
        duplicate = error.get("Code") == "TransactionCanceledException" and any(
            r.get("Code") == "ConditionalCheckFailed" for r in reasons
        )
        if duplicate:
            return StoreOutcome.DUPLICATE
        raise
    return StoreOutcome.STORED


MAX_STORED_PAYLOAD = 4000
MAX_STORED_REASON = 1000


def store_rejection(
    table: Any,
    report_id: str | None,
    reason: str,
    payload: str,
    received: datetime,
    message_id: str,
) -> None:
    """Record a refused message for the admin.

    Keyed by the report id when the message carried a well-formed one, otherwise filed as
    unidentified. The sort key includes the arrival time and the SQS message id, so every
    refused message keeps its own record, and a redelivery of the same message rewrites the
    same item instead of adding another. Reason and payload are cut to a fixed size, so no
    message can produce an item DynamoDB would refuse; the payload is kept as text, never
    parsed, since it may not be valid JSON at all.
    """
    at = _iso(received)
    owner = report_id if report_id and REPORT_ID_RE.match(report_id) else "unidentified"
    table.put_item(
        Item={
            schema.PK: f"REPORT{S}{owner}",
            schema.SK: f"REJECTED{S}{at}{S}{message_id[:100]}",
            "entity": "report_rejection",
            "report_id": owner,
            "message_id": message_id[:100],
            "reason": reason[:MAX_STORED_REASON],
            "payload": payload[:MAX_STORED_PAYLOAD],
            "received_utc": at,
        }
    )


def rejections_for(table: Any, report_id: str) -> list[dict[str, Any]]:
    return table.query(
        KeyConditionExpression=Key(schema.PK).eq(f"REPORT{S}{report_id}")
        & Key(schema.SK).begins_with(f"REJECTED{S}")
    ).get("Items", [])


RESEND_IGNORED_FIELDS = frozenset({"reported_utc"})
"""A sender resending a report usually stamps it again, so the time may differ."""


def same_report(stored: dict[str, Any], report: Report) -> bool:
    """Whether a stored disruption holds this report: a genuine resend, not a reused id.

    Type, sender, details and narrative must match exactly. The report time may differ,
    because a client that resends usually stamps the message again; the first stored copy
    keeps its original time.
    """

    def essence(doc: Any) -> dict[str, Any]:
        doc = dict(doc or {})
        for name in RESEND_IGNORED_FIELDS:
            doc.pop(name, None)
        return doc

    return essence(from_dynamo(stored.get("report"))) == essence(report.to_dict())


def get_disruption(table: Any, report_id: str) -> dict[str, Any] | None:
    item = table.get_item(Key={schema.PK: disruption_pk(report_id), schema.SK: "META"})
    return item.get("Item")


def disruptions_with_status(table: Any, status: str = STATUS_REPORTED) -> list[dict[str, Any]]:
    """Oldest first, through the status index."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "IndexName": schema.GSI1,
        "KeyConditionExpression": Key(schema.GSI1_PK).eq(f"STATUS{S}{status}"),
    }
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


# --------------------------------------------------------------------------
# Analysis results
# --------------------------------------------------------------------------

STATUS_OPTIONS_READY = "OPTIONS_READY"
ANALYSIS_ACTOR = "system:analysis"


class AnalysisOutcome(StrEnum):
    STORED = "stored"
    ALREADY_ANALYSED = "already_analysed"
    MISSING = "missing"


ANALYSIS_FAILED = "ANALYSIS_FAILED"


def record_analysis_failure(table: Any, disruption_id: str, reason: str, at: datetime) -> None:
    """Note on the disruption's audit trail that it could not be analysed, and why.

    The disruption stays REPORTED, so it can be analysed again (``--pending``) once the
    cause is fixed. Recorded once per attempt time; reasons are cut to a fixed size.
    """
    when = _iso(at)
    table.put_item(
        Item={
            schema.PK: disruption_pk(disruption_id),
            schema.SK: f"AUDIT{S}{when}{S}{ANALYSIS_FAILED}",
            "entity": "audit_event",
            "disruption_id": disruption_id,
            "action": ANALYSIS_FAILED,
            "actor": ANALYSIS_ACTOR,
            "reason": reason[:MAX_STORED_REASON],
            "at": when,
        }
    )


def store_analysis(
    table: Any, disruption_id: str, analysis: dict[str, Any], analysed: datetime
) -> AnalysisOutcome:
    """Attach an analysis to a REPORTED disruption and move it to OPTIONS_READY, once.

    The update and its audit event are one transaction, conditioned on the disruption still
    being REPORTED. A retried or duplicated trigger therefore cannot analyse twice or
    overwrite a later status: the second attempt is refused and reported as already done.
    Floats in the analysis are converted for DynamoDB, which has no float type.
    """
    at = _iso(analysed)
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": {schema.PK: disruption_pk(disruption_id), schema.SK: "META"},
                        "UpdateExpression": (
                            "SET #status = :ready, #gsi1pk = :index, #analysis = :analysis, "
                            "#analysed = :at"
                        ),
                        "ConditionExpression": "#status = :reported",
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#gsi1pk": schema.GSI1_PK,
                            "#analysis": "analysis",
                            "#analysed": "analysed_utc",
                        },
                        "ExpressionAttributeValues": {
                            ":ready": STATUS_OPTIONS_READY,
                            ":reported": STATUS_REPORTED,
                            ":index": f"STATUS{S}{STATUS_OPTIONS_READY}",
                            ":analysis": to_dynamo(analysis),
                            ":at": at,
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {
                            schema.PK: disruption_pk(disruption_id),
                            schema.SK: f"AUDIT{S}{at}{S}{STATUS_OPTIONS_READY}",
                            "entity": "audit_event",
                            "disruption_id": disruption_id,
                            "action": STATUS_OPTIONS_READY,
                            "actor": ANALYSIS_ACTOR,
                            "at": at,
                        },
                    }
                },
            ]
        )
    except ClientError as exc:
        reasons = exc.response.get("CancellationReasons") or []
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
            r.get("Code") == "ConditionalCheckFailed" for r in reasons
        ):
            if get_disruption(table, disruption_id) is None:
                return AnalysisOutcome.MISSING
            return AnalysisOutcome.ALREADY_ANALYSED
        raise
    return AnalysisOutcome.STORED
