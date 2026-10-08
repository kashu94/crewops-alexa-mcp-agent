"""Alert addresses, managed by the admin alone.

An admin subscribes an email address to one role's alerts (controller or approver); an
address holds one role, because SNS keeps one subscription per address and topic. SNS
asks the address to confirm before anything is delivered, and every alert is filtered by
role, so an address only ever receives what its role is about. Nobody else can add or
remove addresses, and at most ``DAILY_CAP`` can be added a day and ``MAX_ADDRESSES`` held
at once, so a public demo account cannot be used to send mail to strangers.

Each subscription is also recorded in the table (``ALERTS / SUB#id``) with who added it,
and every change leaves an audit event (``ALERTS / AUDIT#ts#action#id``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from crewops.domain import Role
from crewops.store import schema
from crewops.store.schema import SEPARATOR as S
from crewops.tools import Caller

ALERT_ROLES = ("controller", "approver")
DAILY_CAP = 5
MAX_ADDRESSES = 20
ADMINS = frozenset({Role.ADMIN, Role.SUPER_ADMIN})
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
PK = "ALERTS"


class AlertError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


class AlertSubscriptions:
    def __init__(
        self,
        sns: Any,
        topic_arn: str,
        table: Any,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sns = sns
        self.topic_arn = topic_arn
        self.table = table
        self.clock = clock

    # ------------------------------------------------------------------

    def add(self, caller: Caller, email: str, role: str) -> dict[str, Any]:
        self._admin(caller)
        email = (email or "").strip()
        if len(email) > 254 or not EMAIL_RE.match(email):
            raise AlertError("INVALID", "that is not an email address")
        if role not in ALERT_ROLES:
            raise AlertError("INVALID", f"role must be one of {', '.join(ALERT_ROLES)}")
        held = self._records()
        same = [r for r in held if r["email"].lower() == email.lower()]
        if same:
            raise AlertError(
                "DUPLICATE",
                f"that address already receives {same[0]['role']} alerts; an address holds "
                "one role (remove it first to change the role)",
            )
        if len(held) >= MAX_ADDRESSES:
            raise AlertError("LIMIT", f"at most {MAX_ADDRESSES} alert addresses at once")
        now = self.clock()
        self._count_today(now, 1)
        try:
            arn = self.sns.subscribe(
                TopicArn=self.topic_arn,
                Protocol="email",
                Endpoint=email,
                Attributes={
                    "FilterPolicy": json.dumps({"role": [role]}),
                    "FilterPolicyScope": "MessageAttributes",
                },
                ReturnSubscriptionArn=True,
            )["SubscriptionArn"]
        except ClientError as exc:
            self._count_today(now, -1)  # nothing was added: give the day's slot back
            if _code(exc) == "InvalidParameter":
                message = exc.response.get("Error", {}).get("Message", "")
                raise AlertError("INVALID", f"AWS refused the address: {message}") from None
            raise
        sub_id = arn.rsplit(":", 1)[-1]
        record = {
            schema.PK: PK,
            schema.SK: f"SUB{S}{sub_id}",
            "entity": "alert_subscription",
            "subscription_id": sub_id,
            "subscription_arn": arn,
            "email": email,
            "role": role,
            "added_by": caller.user_id,
            "added_utc": _iso(now),
        }
        self.table.put_item(Item=record)
        self._audit("ALERT_ADDED", caller, now, sub_id, role)
        return {
            "subscription_id": sub_id,
            "email": email,
            "role": role,
            "status": "pending confirmation",
            "note": "AWS has emailed the address a link; nothing is sent until it is confirmed.",
        }

    def list(self, caller: Caller) -> list[dict[str, Any]]:
        self._admin(caller)
        return [
            {
                "subscription_id": r["subscription_id"],
                "email": r["email"],
                "role": r["role"],
                "added_by": r["added_by"],
                "added_utc": r["added_utc"],
                "status": self._status(r["subscription_arn"]),
            }
            for r in sorted(self._records(), key=lambda r: r["added_utc"])
        ]

    def remove(self, caller: Caller, subscription_id: str) -> dict[str, Any]:
        self._admin(caller)
        key = {schema.PK: PK, schema.SK: f"SUB{S}{subscription_id}"}
        record = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        if record is None:
            raise AlertError("NOT_FOUND", f"no alert address {subscription_id}")
        try:
            self.sns.unsubscribe(SubscriptionArn=record["subscription_arn"])
        except ClientError as exc:
            if _code(exc) == "InvalidParameter":
                # SNS cannot remove a subscription nobody has confirmed yet.
                raise AlertError(
                    "PENDING",
                    "that address has not confirmed yet; it can be removed once confirmed, "
                    "and an unconfirmed one lapses on its own after three days",
                ) from None
            if _code(exc) != "NotFound":
                raise
        self.table.delete_item(Key=key)
        self._audit("ALERT_REMOVED", caller, self.clock(), subscription_id, record["role"])
        return {"subscription_id": subscription_id, "status": "removed"}

    # ------------------------------------------------------------------

    @staticmethod
    def _admin(caller: Caller) -> None:
        if not caller.roles & ADMINS:
            raise AlertError("FORBIDDEN", "only an admin manages alert addresses")

    def _records(self) -> list[dict[str, Any]]:
        page = self.table.query(
            KeyConditionExpression=Key(schema.PK).eq(PK) & Key(schema.SK).begins_with(f"SUB{S}"),
            ConsistentRead=True,
        )
        return page.get("Items", [])

    def _count_today(self, now: datetime, step: int) -> None:
        """One more address today (``step`` 1), refused once the day's cap is reached, or
        one fewer (``step`` -1) when an addition failed."""
        day = now.astimezone(UTC).strftime("%Y-%m-%d")
        key = {schema.PK: PK, schema.SK: f"DAY{S}{day}"}
        if step < 0:
            self.table.update_item(
                Key=key,
                UpdateExpression="ADD #n :minus",
                ConditionExpression="#n > :zero",
                ExpressionAttributeNames={"#n": "added"},
                ExpressionAttributeValues={":minus": -1, ":zero": 0},
            )
            return
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="ADD #n :one SET #entity = :entity, #ttl = :ttl",
                ConditionExpression="attribute_not_exists(#n) OR #n < :cap",
                ExpressionAttributeNames={"#n": "added", "#entity": "entity", "#ttl": schema.TTL},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":cap": DAILY_CAP,
                    ":entity": "alert_daily_count",
                    ":ttl": int((now + timedelta(days=2)).timestamp()),
                },
            )
        except ClientError as exc:
            if _code(exc) == "ConditionalCheckFailedException":
                raise AlertError(
                    "LIMIT", f"at most {DAILY_CAP} alert addresses can be added a day"
                ) from None
            raise

    def _status(self, arn: str) -> str:
        try:
            attrs = self.sns.get_subscription_attributes(SubscriptionArn=arn)["Attributes"]
        except ClientError as exc:
            if _code(exc) == "NotFound":
                return "gone (never confirmed, or unsubscribed from the email)"
            raise
        return "pending confirmation" if attrs.get("PendingConfirmation") == "true" else "active"

    def _audit(self, action: str, caller: Caller, at: datetime, sub_id: str, role: str) -> None:
        when = _iso(at)
        self.table.put_item(
            Item={
                schema.PK: PK,
                schema.SK: f"AUDIT{S}{when}{S}{action}{S}{sub_id}",
                "entity": "audit_event",
                "action": action,
                "actor": caller.user_id,
                "at": when,
                "subscription_id": sub_id,
                "role": role,
            }
        )
