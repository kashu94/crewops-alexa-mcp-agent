"""Rule thresholds an admin may tighten, and the record of every change.

The legality rules' numbers live in the operations table (``CONFIG / RULE#id``), so the
engine reads whatever is stored there the next time it loads the operation (the MCP server
within a minute, the analysis on its next run). Four numbers are adjustable:

==================  ======================  =========  ====================================
Rule                Parameter               Range      Meaning
==================  ======================  =========  ====================================
RULE-FDP-01         base_fdp_hours          9 - 13     Flight duty period limit, 2 sectors
RULE-DUTY-02        max_duty_hours          40 - 60    Duty hours in 7 days
RULE-FLT-03         max_flight_hours        60 - 100   Block hours in 28 days
RULE-REST-04        min_rest_hours          12 - 16    Rest between release and report
==================  ======================  =========  ====================================

Each range stops at the regulatory value the operation was built with, so a change can only
make the rules stricter, or restore them: no setting makes an illegal duty legal. A change
names the value it expects to replace (two admins cannot overwrite each other unseen), must
give a reason, and rewrites the rule's sentence so explanations quote the new number. It
is recorded as an audit event (``CONFIG / AUDIT#ts#RULE_CHANGED#rule#id``).

The analysis stored for a disruption keeps the options it found under the old rules until it
is analysed again; approval always re-checks against the rules as they are then.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from crewops.store import schema
from crewops.store.items import from_dynamo
from crewops.store.schema import SEPARATOR as S

CONFIG = "CONFIG"
MAX_REASON = 300


@dataclass(frozen=True, slots=True)
class Threshold:
    rule_id: str
    param: str
    label: str
    unit: str
    minimum: float
    maximum: float
    """The regulatory value: the loosest the rule may be."""
    step: float
    stricter: str
    """"lower" when a smaller number is stricter (a limit), "higher" for a minimum."""
    text: Callable[[dict[str, Any]], str]


def _g(value: Any) -> str:
    return f"{float(value):g}"


THRESHOLDS: tuple[Threshold, ...] = (
    Threshold(
        "RULE-FDP-01",
        "base_fdp_hours",
        "Flight duty period (2 sectors)",
        "h",
        9,
        13,
        0.5,
        "lower",
        lambda p: (
            f"Max flight duty period {_g(p['base_fdp_hours'])}h, reduced "
            f"{_g(p.get('reduction_per_extra_sector_hours', 0.5))}h per sector beyond the 2nd."
        ),
    ),
    Threshold(
        "RULE-DUTY-02",
        "max_duty_hours",
        "Duty hours in 7 days",
        "h",
        40,
        60,
        1,
        "lower",
        lambda p: (
            f"Max {_g(p['max_duty_hours'])} duty hours in any 7 consecutive calendar days "
            "(inclusive of duty date)."
        ),
    ),
    Threshold(
        "RULE-FLT-03",
        "max_flight_hours",
        "Flight hours in 28 days",
        "h",
        60,
        100,
        1,
        "lower",
        lambda p: (
            f"Max {_g(p['max_flight_hours'])} flight (block) hours in any 28 consecutive "
            "calendar days."
        ),
    ),
    Threshold(
        "RULE-REST-04",
        "min_rest_hours",
        "Minimum rest",
        "h",
        12,
        16,
        0.5,
        "higher",
        lambda p: f"Min {_g(p['min_rest_hours'])}h rest between release and next report.",
    ),
)
BY_KEY = {(t.rule_id, t.param): t for t in THRESHOLDS}


class RuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rule_key(rule_id: str) -> dict[str, str]:
    return {schema.PK: CONFIG, schema.SK: f"RULE{S}{rule_id}"}


def thresholds(table: Any) -> list[dict[str, Any]]:
    """Every adjustable threshold with its current value and allowed range."""
    out = []
    for t in THRESHOLDS:
        item = table.get_item(Key=rule_key(t.rule_id), ConsistentRead=True).get("Item")
        params = from_dynamo(item.get("params") or {}) if item else {}
        out.append(
            {
                "rule_id": t.rule_id,
                "param": t.param,
                "label": t.label,
                "unit": t.unit,
                "value": params.get(t.param),
                "min": t.minimum,
                "max": t.maximum,
                "step": t.step,
                "stricter": t.stricter,
                "text": from_dynamo(item).get("text") if item else None,
            }
        )
    return out


def set_threshold(
    table: Any,
    rule_id: str,
    param: str,
    value: float,
    expected: float,
    actor: str,
    reason: str,
    at: datetime,
) -> dict[str, Any]:
    """Change one threshold from ``expected`` to ``value``, with the rule's sentence and an
    audit event, in one transaction."""
    t = BY_KEY.get((rule_id, param))
    if t is None:
        raise RuleError("INVALID", f"{rule_id} {param} is not adjustable")
    reason = (reason or "").strip()
    if not reason:
        raise RuleError("INVALID", "say why the threshold changes")
    if len(reason) > MAX_REASON:
        raise RuleError("INVALID", f"keep the reason under {MAX_REASON} characters")
    for number in (value, expected):
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise RuleError("INVALID", "the value and the value it replaces must be numbers")
    if not t.minimum <= value <= t.maximum:
        raise RuleError(
            "INVALID",
            f"{t.label} must stay between {t.minimum:g} and {t.maximum:g}{t.unit}; "
            "rules can be made stricter, never looser than regulation",
        )
    if round((value - t.minimum) / t.step, 6) % 1:
        raise RuleError("INVALID", f"{t.label} moves in steps of {t.step:g}{t.unit}")
    item = table.get_item(Key=rule_key(rule_id), ConsistentRead=True).get("Item")
    if item is None:
        raise RuleError("NOT_FOUND", f"no rule {rule_id}")
    params = from_dynamo(item.get("params") or {})
    params[param] = value
    when = _iso(at)
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": rule_key(rule_id),
                        "UpdateExpression": "SET #params.#p = :value, #text = :text",
                        "ConditionExpression": "#params.#p = :expected",
                        "ExpressionAttributeNames": {
                            "#params": "params",
                            "#p": param,
                            "#text": "text",
                        },
                        "ExpressionAttributeValues": {
                            ":value": Decimal(str(value)),
                            ":expected": Decimal(str(expected)),
                            ":text": t.text(params),
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {
                            schema.PK: CONFIG,
                            # The id keeps two changes in the same second apart.
                            schema.SK: (
                                f"AUDIT{S}{when}{S}RULE_CHANGED{S}{rule_id}{S}"
                                f"{uuid.uuid4().hex[:8]}"
                            ),
                            "entity": "audit_event",
                            "action": "RULE_CHANGED",
                            "actor": actor,
                            "at": when,
                            "rule_id": rule_id,
                            "param": param,
                            "from": Decimal(str(expected)),
                            "to": Decimal(str(value)),
                            "reason": reason,
                        },
                        "ConditionExpression": "attribute_not_exists(#pk)",
                        "ExpressionAttributeNames": {"#pk": schema.PK},
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise RuleError(
                "CONFLICT", f"{t.label} is no longer {expected:g}{t.unit}; reload and try again"
            ) from None
        raise
    return {"rule_id": rule_id, "param": param, "value": value, "text": t.text(params)}


def activity(table: Any, limit: int = 50) -> list[dict[str, Any]]:
    """The latest audit events across the system, newest first: rule changes, alert
    addresses and every disruption's transitions. A filtered scan: the table is small."""
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": "#e = :audit",
        "ExpressionAttributeNames": {"#e": "entity"},
        "ExpressionAttributeValues": {":audit": "audit_event"},
    }
    while True:
        page = table.scan(**kwargs)
        events += [from_dynamo(i) for i in page.get("Items", [])]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    events.sort(key=lambda e: e.get("at", ""), reverse=True)
    keep = (
        "at",
        "action",
        "actor",
        "disruption_id",
        "plan_id",
        "rule_id",
        "param",
        "from",
        "to",
        "role",
        "reason",
    )
    return [{k: e[k] for k in keep if e.get(k) is not None} for e in events[:limit]]


def rule_changes(table: Any) -> list[dict[str, Any]]:
    page = table.query(
        KeyConditionExpression=Key(schema.PK).eq(CONFIG) & Key(schema.SK).begins_with(f"AUDIT{S}"),
        ConsistentRead=True,
    )
    return [from_dynamo(i) for i in page.get("Items", [])]
