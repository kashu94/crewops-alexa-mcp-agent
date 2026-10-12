"""Desktop alerts (web push) for the people a disruption needs, even with CrewOps closed.

Run by the notifier Lambda next to the Alexa notifications, for the same people: the
controller of the desk an analysed or sent-back disruption is on, and every approver when a
plan is submitted (``crewops.alexa.notifier.recipients``). Each browser they turned desktop
alerts on in gets one notification; clicking it opens the disruption.

Best effort, like the Alexa chime: a failure is logged, never retried (the console and email
carry the same news). A browser the push service says is gone is forgotten.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

from crewops.alexa.notifier import current, recipients
from crewops.notify.changes import change_of

log = logging.getLogger()
log.setLevel(logging.INFO)


def message(record: dict[str, Any]) -> dict[str, Any] | None:
    """What the notification says. Names the disruption, never a crew member."""
    change = change_of(record)
    if change is None:
        return None
    what = change.report_type.replace("_", " ")
    if change.after == "SUBMITTED":
        title, body = (
            "Plan waiting for approval",
            f"{change.disruption_id} ({what}) has a plan to approve.",
        )
    elif change.before == "SUBMITTED":
        title, body = "Plan sent back", f"{change.disruption_id} ({what}) needs another plan."
    else:
        title, body = "New on your desk", f"{change.disruption_id} ({what}) is ready for a plan."
    return {
        "title": f"CrewOps: {title}",
        "body": body,
        "url": f"/console/{change.disruption_id}",
        "tag": change.disruption_id,
    }


BUDGET_SECONDS = 20
"""How long the notifier spends on desktop alerts: a slow push service must not run the
Lambda out of time (that would retry the whole batch, emails and all)."""


def process(
    records: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    send: Any,
    forget: Any,
    budget: float = BUDGET_SECONDS,
) -> int:
    """``send(subscription, message)`` returns "sent", "gone" or "failed"; ``forget`` drops a
    gone one. Sends side by side, and gives up on any not done within ``budget`` seconds.
    Returns how many were sent."""
    people = [s | {"notifications": True} for s in subscriptions]
    jobs = [
        (sub, note)
        for record in records
        if (note := message(record)) is not None
        for sub in recipients(record, people)
    ]
    if not jobs:
        return 0

    def one(sub: dict[str, Any], note: dict[str, Any]) -> bool:
        try:
            outcome = send(sub, note)
            if outcome == "gone":
                forget(sub)
            return outcome == "sent"
        except Exception:
            log.exception("desktop alert to %s failed", sub.get("username"))
            return False

    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(one, sub, note) for sub, note in jobs]
    done, late = wait(futures, timeout=budget)
    pool.shutdown(wait=False, cancel_futures=True)
    if late:
        log.warning("%d desktop alert(s) not sent in time", len(late))
    return sum(1 for f in done if f.result())


def notify(records: list[dict[str, Any]], settings: Any) -> int:
    """Send the desktop alerts these stream records call for. Best effort."""
    import boto3

    from crewops.notify.webpush import Sender
    from crewops.store import push
    from crewops.store.ops_store import table_for
    from crewops.store.users import get_profile

    table = table_for(settings)
    found = current(push.subscriptions(table), lambda u: get_profile(table, u))
    people = [s | {"notifications": True} for s in found]
    if not any(recipients(r, people) for r in records):
        return 0  # nobody to notify: no keys fetched
    ssm = boto3.Session(region_name=settings.aws_region).client("ssm")
    prefix = settings.push_parameter_prefix
    private = ssm.get_parameter(Name=f"{prefix}/vapid-private", WithDecryption=True)
    public = ssm.get_parameter(Name=f"{prefix}/vapid-public")
    sender = Sender(private["Parameter"]["Value"], public["Parameter"]["Value"])
    return process(
        records,
        found,
        sender.send,
        lambda s: push.forget(table, s["username"], s["endpoint"]),
    )
