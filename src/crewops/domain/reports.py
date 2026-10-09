"""Live reports: who may report what, and what a well-formed report looks like.

A report is the first event in a disruption's life (REPORTED). It names its type, who sent
it, when, and type-specific details. Parsing validates and normalises the details, so
everything downstream can rely on their shape: dates as ``YYYY-MM-DD``, instants as UTC
``...Z`` strings, numbers as numbers.

Who may report is decided here, in code, never by a prompt. The roles checked are the ones
the message states: the sender is trusted to have set them from a verified login. In the
finished system only the MCP server, which signs users in through Cognito, sends reports,
and the queue accepts messages from it alone.

* reporter, controller and approver: any type;
* crew: only their own unavailability;
* auditor, admin and super admin: nothing (they observe and configure, they do not operate).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from crewops.domain.models import (
    STATION_RE,
    CertType,
    DataError,
    parse_date,
    parse_utc,
)

REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
GATE_RE = re.compile(r"^[A-Z]{3}-G[0-9]{1,2}$")
MAX_CREW_IN_ONE_REPORT = 12
MAX_NARRATIVE = 1000
MAX_DELAY_MINUTES = 24 * 60


class Role(StrEnum):
    CREW = "crew"
    REPORTER = "reporter"
    CONTROLLER = "controller"
    APPROVER = "approver"
    AUDITOR = "auditor"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class ReportType(StrEnum):
    CREW_UNAVAILABLE = "crew_unavailable"
    FLIGHT_DELAY = "flight_delay"
    AIRCRAFT_TECHNICAL = "aircraft_technical"
    STATION_DISRUPTION = "station_disruption"
    GATE_CHANGE = "gate_change"
    CERTIFICATION_LAPSE = "certification_lapse"
    MULTIPLE_CREW_UNAVAILABLE = "multiple_crew_unavailable"
    GATE_CLOSURE = "gate_closure"


OPERATIONAL_REPORTERS = frozenset({Role.REPORTER, Role.CONTROLLER, Role.APPROVER})
UNAVAILABILITY_REASONS = frozenset({"sick", "fatigue", "personal", "other"})


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shown(value: Any, limit: int = 60) -> str:
    """A short, safe rendering of bad input for an error message.

    Error messages end up in logs and in stored rejection records, so they must never grow
    with the input: a 500,000-character field must not make a 500,000-character message.
    """
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _encodable(value: str, where: str) -> str:
    """Refuse text DynamoDB cannot store, such as a lone UTF-16 surrogate from JSON."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DataError(f"{where}: contains characters that cannot be stored") from None
    return value


def _text(d: Mapping[str, Any], key: str, ctx: str, max_len: int = 100) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DataError(f"{ctx}.{key}: expected a non-empty string, got {_shown(value)}")
    value = value.strip()
    if len(value) > max_len:
        raise DataError(f"{ctx}.{key}: longer than {max_len} characters")
    return _encodable(value, f"{ctx}.{key}")


def _window(d: Mapping[str, Any], ctx: str) -> tuple[str, str]:
    start = parse_utc(d.get("from_utc"), f"{ctx}.from_utc")
    end = parse_utc(d.get("to_utc"), f"{ctx}.to_utc")
    if end <= start:
        raise DataError(f"{ctx}: to_utc must be after from_utc")
    return _iso(start), _iso(end)


# --------------------------------------------------------------------------
# Details per report type
# --------------------------------------------------------------------------


def _crew_unavailable(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    start = parse_date(d.get("from_date"), f"{ctx}.from_date")
    end = parse_date(d.get("to_date", d.get("from_date")), f"{ctx}.to_date")
    if end < start:
        raise DataError(f"{ctx}: to_date is before from_date")
    reason = _text(d, "reason", ctx)
    if reason not in UNAVAILABILITY_REASONS:
        allowed = sorted(UNAVAILABILITY_REASONS)
        raise DataError(f"{ctx}.reason: one of {allowed}, got {_shown(reason)}")
    out = {
        "crew_id": _text(d, "crew_id", ctx),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "reason": reason,
    }
    if d.get("from_utc"):
        # Part-way through the first day (taken ill mid-trip): legs before it are flown.
        at = parse_utc(d.get("from_utc"), f"{ctx}.from_utc")
        if at.date() != start:
            raise DataError(f"{ctx}.from_utc: must be on from_date ({start.isoformat()})")
        out["from_utc"] = _iso(at)
    return out


def _multiple_crew_unavailable(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    """Several crew out at once (a stomach bug after a crew meal, a road accident on the way
    in): one report, so their seats are covered together and never by one another."""
    raw = d.get("crew_ids")
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_CREW_IN_ONE_REPORT:
        raise DataError(f"{ctx}.crew_ids: a list of 2 to {MAX_CREW_IN_ONE_REPORT} crew ids")
    crew_ids = [_text({"crew_id": c}, "crew_id", f"{ctx}.crew_ids") for c in raw]
    if len(set(crew_ids)) != len(crew_ids):
        raise DataError(f"{ctx}.crew_ids: a crew member is listed twice")
    one = _crew_unavailable({**d, "crew_id": crew_ids[0]}, ctx)
    del one["crew_id"]
    return {"crew_ids": sorted(crew_ids), **one}


def _flight_delay(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    minutes = d.get("delay_minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise DataError(f"{ctx}.delay_minutes: expected a whole number of minutes")
    if not 1 <= minutes <= MAX_DELAY_MINUTES:
        raise DataError(f"{ctx}.delay_minutes: must be 1 to {MAX_DELAY_MINUTES}")
    return {"flight_id": _text(d, "flight_id", ctx), "delay_minutes": minutes}


def _aircraft_technical(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    start, end = _window(d, ctx)
    return {"aircraft": _text(d, "aircraft", ctx), "from_utc": start, "to_utc": end}


def _station_disruption(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    station = _text(d, "station", ctx)
    if not STATION_RE.match(station):
        raise DataError(f"{ctx}.station: expected a 3-letter station code, got {_shown(station)}")
    start, end = _window(d, ctx)
    return {
        "station": station,
        "from_utc": start,
        "to_utc": end,
        "reason": _text(d, "reason", ctx, max_len=200),
    }


def _gate(d: Mapping[str, Any], key: str, ctx: str) -> str:
    gate = _text(d, key, ctx, 20)
    if not GATE_RE.match(gate):
        raise DataError(f"{ctx}.{key}: expected a gate like BLR-G2, got {_shown(gate)}")
    return gate


GATE_CLOSURE_CAUSES = ("internal", "external")
"""internal: the gate's own equipment or staff (jet bridge, power, a security sweep), fixed
by the station. external: weather or a natural event, which may close more gates."""


def _gate_closure(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    start, end = _window(d, ctx)
    cause = d.get("cause", "internal")
    if cause not in GATE_CLOSURE_CAUSES:
        raise DataError(f"{ctx}.cause: one of {list(GATE_CLOSURE_CAUSES)}, got {_shown(cause)}")
    return {
        "gate": _gate(d, "gate", ctx),
        "from_utc": start,
        "to_utc": end,
        "reason": _text(d, "reason", ctx, max_len=200),
        "cause": cause,
    }


def _gate_change(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    return {"flight_id": _text(d, "flight_id", ctx), "new_gate": _text(d, "new_gate", ctx, 20)}


def _certification_lapse(d: Mapping[str, Any], ctx: str) -> dict[str, Any]:
    raw = d.get("cert_type")
    try:
        cert_type = CertType(raw)
    except ValueError:
        allowed = ", ".join(c.value for c in CertType)
        raise DataError(f"{ctx}.cert_type: unknown {_shown(raw)} (allowed: {allowed})") from None
    return {
        "crew_id": _text(d, "crew_id", ctx),
        "cert_type": cert_type.value,
        "valid_to": parse_date(d.get("valid_to"), f"{ctx}.valid_to").isoformat(),
    }


DETAIL_PARSERS: dict[ReportType, Callable[[Mapping[str, Any], str], dict[str, Any]]] = {
    ReportType.CREW_UNAVAILABLE: _crew_unavailable,
    ReportType.FLIGHT_DELAY: _flight_delay,
    ReportType.AIRCRAFT_TECHNICAL: _aircraft_technical,
    ReportType.STATION_DISRUPTION: _station_disruption,
    ReportType.GATE_CHANGE: _gate_change,
    ReportType.CERTIFICATION_LAPSE: _certification_lapse,
    ReportType.MULTIPLE_CREW_UNAVAILABLE: _multiple_crew_unavailable,
    ReportType.GATE_CLOSURE: _gate_closure,
}


def crew_out_of(report_type: str, details: Mapping[str, Any]) -> tuple[str, ...]:
    """The crew a report takes off the roster: the sick, or the holder of a lapsed
    certificate. Empty for reports about flights, aircraft, stations and gates."""
    if report_type == ReportType.MULTIPLE_CREW_UNAVAILABLE:
        return tuple(details.get("crew_ids") or ())
    if report_type in (ReportType.CREW_UNAVAILABLE, ReportType.CERTIFICATION_LAPSE):
        return (details["crew_id"],) if details.get("crew_id") else ()
    return ()


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reporter:
    user_id: str
    roles: frozenset[Role]
    crew_id: str | None = None
    """Set when the reporter is themselves crew; required for crew self-reports."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "roles": sorted(r.value for r in self.roles),
            "crew_id": self.crew_id,
        }

    @classmethod
    def from_dict(cls, d: Any, ctx: str = "reported_by") -> Reporter:
        if not isinstance(d, Mapping):
            raise DataError(f"{ctx}: expected an object")
        raw_roles = d.get("roles")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise DataError(f"{ctx}.roles: expected a non-empty list")
        try:
            roles = frozenset(Role(r) for r in raw_roles)
        except ValueError:
            allowed = ", ".join(r.value for r in Role)
            raise DataError(
                f"{ctx}.roles: unknown role in {_shown(raw_roles)} (allowed: {allowed})"
            ) from None
        crew_id = d.get("crew_id")
        if crew_id is not None and (not isinstance(crew_id, str) or not crew_id.strip()):
            raise DataError(f"{ctx}.crew_id: expected a crew id or null")
        return cls(user_id=_text(d, "user_id", ctx), roles=roles, crew_id=crew_id)


@dataclass(frozen=True, slots=True)
class Report:
    report_id: str
    """Chosen by the sender and reused on any resend, so a report is stored only once."""
    report_type: ReportType
    reported_by: Reporter
    reported_utc: datetime
    details: Mapping[str, Any]
    narrative: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "type": self.report_type.value,
            "reported_by": self.reported_by.to_dict(),
            "reported_utc": _iso(self.reported_utc),
            "details": dict(self.details),
            "narrative": self.narrative,
        }

    @classmethod
    def from_dict(cls, d: Any, ctx: str = "report") -> Report:
        if not isinstance(d, Mapping):
            raise DataError(f"{ctx}: expected a JSON object")
        report_id = d.get("report_id")
        if not isinstance(report_id, str) or not REPORT_ID_RE.match(report_id):
            raise DataError(
                f"{ctx}.report_id: 8-64 letters, digits, '-' or '_', got {_shown(report_id)}"
            )
        try:
            report_type = ReportType(d.get("type"))
        except ValueError:
            allowed = ", ".join(t.value for t in ReportType)
            shown = _shown(d.get("type"))
            raise DataError(f"{ctx}.type: unknown {shown} (allowed: {allowed})") from None
        details = d.get("details")
        if not isinstance(details, Mapping):
            raise DataError(f"{ctx}.details: expected an object")
        narrative = d.get("narrative") or ""
        if not isinstance(narrative, str) or len(narrative) > MAX_NARRATIVE:
            raise DataError(f"{ctx}.narrative: text up to {MAX_NARRATIVE} characters")
        _encodable(narrative, f"{ctx}.narrative")
        return cls(
            report_id=report_id,
            report_type=report_type,
            reported_by=Reporter.from_dict(d.get("reported_by"), f"{ctx}.reported_by"),
            reported_utc=parse_utc(d.get("reported_utc"), f"{ctx}.reported_utc"),
            details=DETAIL_PARSERS[report_type](details, f"{ctx}.details"),
            narrative=narrative.strip(),
        )


MAX_CLOCK_SKEW = timedelta(minutes=5)


def check_timing(report: Report, received: datetime) -> str | None:
    """None if the report time is plausible: not ahead of when it arrived (small skew allowed).

    Late reports are fine; a report from the future is a clock error or a forgery, and would
    also jump the queue in the status index, which is ordered by report time.
    """
    if report.reported_utc > received + MAX_CLOCK_SKEW:
        return (
            f"reported_utc {report.to_dict()['reported_utc']} is ahead of the time it arrived "
            f"({_iso(received)})"
        )
    return None


def authorize(report: Report) -> str | None:
    """None if the sender may file this report, otherwise the reason they may not."""
    roles = report.reported_by.roles
    if roles & OPERATIONAL_REPORTERS:
        return None
    if Role.CREW in roles:
        if report.report_type is not ReportType.CREW_UNAVAILABLE:
            return "crew may only report their own unavailability"
        if not report.reported_by.crew_id:
            return "crew reporter has no crew id"
        if report.details["crew_id"] != report.reported_by.crew_id:
            return "crew may only report their own unavailability"
        return None
    held = ", ".join(sorted(r.value for r in roles))
    return f"roles ({held}) may not file reports"
