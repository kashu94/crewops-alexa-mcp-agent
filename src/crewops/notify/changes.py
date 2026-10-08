"""What a stream record means for the people watching: a disruption changed status.

Only disruption status changes are of interest. Each becomes one console event, and some
also become an alert for a role:

=========================  ===================  ============================================
From -> to                 Alert to             Why
=========================  ===================  ============================================
REPORTED -> OPTIONS_READY  controller           a disruption is analysed and needs a plan
OPTIONS_READY -> SUBMITTED approver             a plan is waiting for approval
SUBMITTED -> OPTIONS_READY controller           the plan was rejected, withdrawn or went
                                                stale; the disruption needs another plan
=========================  ===================  ============================================

Events and alerts carry the disruption id, its report type and the statuses, never a name,
a crew id or the analysis: the details are read in CrewOps, signed in, with role checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crewops.store.plans import STATUS_SUBMITTED
from crewops.store.reports import STATUS_OPTIONS_READY, STATUS_REPORTED

CONSOLE_CHANNEL = "/ops/disruptions"


@dataclass(frozen=True, slots=True)
class Change:
    disruption_id: str
    before: str | None
    """None when the disruption was just reported."""
    after: str
    report_type: str


@dataclass(frozen=True, slots=True)
class Alert:
    role: str
    subject: str
    message: str


def _s(image: dict[str, Any], key: str) -> str | None:
    value = image.get(key) or {}
    return value.get("S") if isinstance(value, dict) else None


def change_of(record: dict[str, Any]) -> Change | None:
    """The status change in one stream record, or None when it is not one."""
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return None
    data = record.get("dynamodb") or {}
    new, old = data.get("NewImage") or {}, data.get("OldImage") or {}
    if _s(new, "entity") != "disruption":
        return None
    after, before = _s(new, "status"), _s(old, "status")
    disruption_id = _s(new, "disruption_id")
    if not after or not disruption_id or after == before:
        return None
    report = (new.get("report") or {}).get("M") or {}
    return Change(disruption_id, before, after, _s(report, "type") or "unknown")


def console_event(change: Change) -> dict[str, Any]:
    return {
        "kind": "disruption_status",
        "disruption_id": change.disruption_id,
        "status": change.after,
        "previous": change.before,
        "report_type": change.report_type,
    }


def alert_for(change: Change, stage: str) -> Alert | None:
    what = change.report_type.replace("_", " ")
    ref = f"Disruption {change.disruption_id} ({what})"
    footer = (
        "\n\nSign in to CrewOps to see the details. You receive this because a CrewOps "
        "administrator subscribed this address to {role} alerts."
    )
    if change.before == STATUS_REPORTED and change.after == STATUS_OPTIONS_READY:
        role, subject = "controller", f"options ready for {change.disruption_id}"
        body = f"{ref} has been analysed and its cover options are ready for a plan."
    elif change.before == STATUS_OPTIONS_READY and change.after == STATUS_SUBMITTED:
        role, subject = "approver", f"plan waiting for approval on {change.disruption_id}"
        body = f"{ref} has a plan waiting for approval by someone other than its submitter."
    elif change.before == STATUS_SUBMITTED and change.after == STATUS_OPTIONS_READY:
        role, subject = "controller", f"plan sent back on {change.disruption_id}"
        body = f"{ref} is back with controllers: its plan was rejected, withdrawn or went stale."
    else:
        return None
    return Alert(role, f"CrewOps {stage}: {subject}"[:100], body + footer.format(role=role))
