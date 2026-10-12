"""A plan's fingerprint: SHA-256 over exactly what was submitted, for four-eyes evidence.

When a controller submits a plan, its fingerprint is taken over everything that decides it:
the disruption, who replaces whom on which trip, the costs, what is left uncovered, the
rosters it was made against, who submitted it and when. The approver sees the same
fingerprint on the approval card. Approval computes it again from the stored plan and
refuses if the two differ, so what is approved is byte for byte what was submitted, even if
the stored record was edited in between. The approval then gets its own seal: SHA-256 over
the plan's fingerprint, the approver and the time, recorded with the decision and in the
audit trail.

This is tamper evidence, not a signature: nobody holds a private key. Anyone with the plan
can recompute both values and check them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

SIGNED_FIELDS = (
    "plan_id",
    "disruption_id",
    "desk",
    "submitted_by",
    "submitted_utc",
    "note",
    "assignments",
    "uncovered",
    "total_cost_inr",
    "rosters_before",
)


def _canon(value: Any) -> Any:
    """The same value however it was stored: numbers as their shortest decimal text (a
    float written to DynamoDB comes back a Decimal), mappings with sorted keys."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int | float | Decimal):
        number = Decimal(str(value)).normalize()
        return format(number, "f") if number == number.to_integral() else str(number)
    if isinstance(value, Mapping):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [_canon(v) for v in value]
    return str(value)


def fingerprint(plan: Mapping[str, Any]) -> str:
    body = {k: _canon(plan.get(k)) for k in SIGNED_FIELDS}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def approval_seal(plan_fingerprint: str, approver: str, at: str) -> str:
    return hashlib.sha256(f"{plan_fingerprint}|{approver}|{at}".encode()).hexdigest()


def short(value: str | None) -> str:
    """How it is shown: the first 16 hex digits, like a commit id."""
    return (value or "")[:16]
