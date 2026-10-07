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
from crewops.store.items import from_dynamo
from crewops.store.schema import SEPARATOR as S

STATUS_REPORTED = "REPORTED"


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
    """
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
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


def same_report(stored: dict[str, Any], report: Report) -> bool:
    """Whether a stored disruption holds exactly this report (a genuine resend)."""
    return from_dynamo(stored.get("report")) == report.to_dict()


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
