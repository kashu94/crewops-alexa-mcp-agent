"""Where changes go: the AppSync Events API (live console) and the SNS topic (email)."""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Sequence
from typing import Any

from crewops.notify.changes import Alert

log = logging.getLogger(__name__)

MAX_EVENTS_PER_PUBLISH = 5
"""AppSync Events accepts at most five events in one publish request."""


class EventPublisher:
    """Publishes to an AppSync Events channel over HTTP, signed with the function's own
    IAM credentials (the API accepts publishes only from IAM)."""

    def __init__(self, http_domain: str, region: str, credentials: Any, timeout: float = 5.0):
        self.url = f"https://{http_domain}/event"
        self.region = region
        self.credentials = credentials
        self.timeout = timeout

    def publish(self, channel: str, events: Sequence[dict[str, Any]]) -> None:
        for start in range(0, len(events), MAX_EVENTS_PER_PUBLISH):
            batch = events[start : start + MAX_EVENTS_PER_PUBLISH]
            body = json.dumps({"channel": channel, "events": [json.dumps(e) for e in batch]})
            request = urllib.request.Request(
                self.url, data=body.encode(), headers=self._signed(body), method="POST"
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read() or b"{}")
            if result.get("failed"):
                log.warning("AppSync refused %d event(s): %s", len(result["failed"]), result)

    def _signed(self, body: str) -> dict[str, str]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        request = AWSRequest(
            method="POST", url=self.url, data=body, headers={"content-type": "application/json"}
        )
        SigV4Auth(self.credentials.get_frozen_credentials(), "appsync", self.region).add_auth(
            request
        )
        return dict(request.headers.items())


class AlertPublisher:
    """Publishes an alert to the topic, tagged with its role, so only addresses the admin
    subscribed to that role receive it."""

    def __init__(self, sns: Any, topic_arn: str) -> None:
        self.sns = sns
        self.topic_arn = topic_arn

    def publish(self, alert: Alert) -> None:
        self.sns.publish(
            TopicArn=self.topic_arn,
            Subject=alert.subject,
            Message=alert.message,
            MessageAttributes={"role": {"DataType": "String", "StringValue": alert.role}},
        )
