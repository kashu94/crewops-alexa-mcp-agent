"""Turn a report into a decision-ready analysis: what broke, who must be replaced, and how.

``analyse`` takes a validated report and the current operation and returns an ``Analysis``:

* the seats that lost their crew, each with its blast radius and ranked legal options;
* a joint plan when several seats need cover at once, so one person is never offered twice;
* for delays, what the delay does to the duty day and to each rostered crew member,
  including the trailing legs that must come off when the day is too long for anyone;
* plain notes for anything a controller should know (nothing to cover, already started,
  gate already taken).

Pure and deterministic: no AWS, no model. The disruption Lambda stores the result and
alerts the controller; the agent later reads it back to answer questions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from crewops.domain import GateSlot, Report, ReportType
from crewops.engine.impact import (
    DelayEffect,
    JointPlan,
    PairingImpact,
    Seat,
    delay_effect,
    joint_plan,
    pairing_impact,
)
from crewops.engine.options import AlreadyStarted, Option, OptionsResult, find_options
from crewops.engine.world import World

TOP_OPTIONS = 5
"""Ranked options kept per seat; counts of the rest are kept too."""

MAX_SEATS, MAX_DELAYS, MAX_FLIGHTS, MAX_NOTES = 40, 60, 300, 50
"""Caps on what one analysis stores, so it always fits a DynamoDB item (400 KB) however
long a closure or absence runs. Anything beyond a cap is counted, never silently lost."""

STATION_REOPEN_TURNAROUND = timedelta(minutes=30)
"""After a station reopens, the first movements need this long: a flight due to depart or
land in the closure is delayed until reopening plus this turnaround."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inr(amount: int) -> str:
    return f"₹{amount:,}"


def option_dict(o: Option) -> dict[str, Any]:
    return {
        "rank": o.rank,
        "action": o.action,
        "strategy": o.strategy.value,
        "crew_id": o.crew_id,
        "name": o.name,
        "crew_rank": o.crew_rank.value if o.crew_rank else None,
        "cost_inr": o.cost_inr,
        "cost_breakdown": dict(o.cost_breakdown),
        "delay_hours": o.delay_hours,
        "positioning_flight": o.positioning_flight,
    }


@dataclass(frozen=True, slots=True)
class SeatAnalysis:
    seat: Seat
    crew_out: str | None
    """The rostered crew member who can no longer fly it, when there is one."""
    impact: PairingImpact
    options: OptionsResult | None
    """None when the pairing has already started and can no longer be re-crewed."""
    note: str = ""
    delay_hours: float = 0.0
    """The schedule the options were judged against: a delayed day needs cover legal for
    the day as it will now run, and approval re-checks against the same."""
    delay_date: date | None = None

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "pairing_id": self.seat.pairing_id,
            "role": self.seat.role.value,
            "crew_out": self.crew_out,
            "delay_hours": self.delay_hours,
            "delay_date": self.delay_date.isoformat() if self.delay_date else None,
            "impact": {
                "affected_dates": [d.isoformat() for d in self.impact.affected_dates],
                "uncovered_flights": list(self.impact.uncovered_flights),
                "at_risk_flights": list(self.impact.at_risk_flights),
                "passengers": self.impact.passengers,
            },
            "note": self.note,
        }
        if self.options is None:
            doc["status"] = "already_started"
            return doc
        r = self.options
        doc.update(
            status="options" if r.crew_options else "no_legal_cover",
            recommended=option_dict(r.recommended) if r.recommended else None,
            options=[option_dict(o) for o in r.crew_options[:TOP_OPTIONS]],
            legal_options=len(r.crew_options),
            excluded=len(r.excluded),
            cancellation_cost_inr=r.cancellation.cost_inr,
            cancellation_multiple=r.cancellation_multiple,
            next_tier_premium_inr=r.next_tier_premium_inr,
        )
        return doc


@dataclass(frozen=True, slots=True)
class Analysis:
    disruption_id: str
    report_type: ReportType
    summary: str
    seats: tuple[SeatAnalysis, ...] = ()
    joint: JointPlan | None = None
    delays: tuple[DelayEffect, ...] = ()
    affected_flights: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "disruption_id": self.disruption_id,
            "report_type": self.report_type.value,
            "summary": self.summary,
            "seats": [s.to_dict() for s in self.seats[:MAX_SEATS]],
            "delays": [_delay_dict(d) for d in self.delays[:MAX_DELAYS]],
            "affected_flights": list(self.affected_flights[:MAX_FLIGHTS]),
            "notes": list(self.notes[:MAX_NOTES]),
            "joint_plan": None,
            "omitted": {
                name: n - cap
                for name, n, cap in (
                    ("seats", len(self.seats), MAX_SEATS),
                    ("delays", len(self.delays), MAX_DELAYS),
                    ("affected_flights", len(self.affected_flights), MAX_FLIGHTS),
                    ("notes", len(self.notes), MAX_NOTES),
                )
                if n > cap
            },
        }
        if self.joint is not None:
            doc["joint_plan"] = {
                "assignments": [
                    {"pairing_id": s.pairing_id, "role": s.role.value, **option_dict(o)}
                    for s, o in self.joint.assignments
                ],
                "unfilled": [
                    {"pairing_id": s.pairing_id, "role": s.role.value} for s in self.joint.unfilled
                ],
                "total_cost_inr": self.joint.total_cost_inr,
                "equal_cost_alternatives": self.joint.equal_cost_alternatives,
                "exhaustive": self.joint.exhaustive,
            }
        return doc


def _delay_dict(d: DelayEffect) -> dict[str, Any]:
    return {
        "pairing_id": d.pairing_id,
        "date": d.date.isoformat(),
        "delay_hours": d.delay_hours,
        "fdp_hours": d.fdp_hours,
        "fdp_limit": d.fdp_limit,
        "duty_breaches_limit": d.duty_breaches_limit,
        "breaches": [
            {"crew_id": b.crew_id, "role": b.role.value, "reasons": list(b.reasons)}
            for b in d.breaches
        ],
        "legs_to_drop": list(d.legs_to_drop),
        "cancellation_cost_inr": d.cancellation_cost_inr,
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _hours_up(seconds: float) -> float:
    """Seconds as hours, rounded up to the whole minute, so a delay is never rounded away."""
    return math.ceil(seconds / 60) / 60


def _flight_index(world: World) -> dict[str, tuple[str, date]]:
    """Flight id -> (pairing id, duty date)."""
    out = {}
    for pid, pairing in world.pairings.items():
        for day in pairing.days:
            for fid in day.flight_ids:
                out[fid] = (pid, day.date)
    return out


def _seat_for(
    world: World,
    seat: Seat,
    crew_out: str | None,
    dates: set[date] | None,
    delay_hours: float = 0.0,
    delay_date: date | None = None,
) -> SeatAnalysis:
    impact = pairing_impact(world, seat.pairing_id, dates)
    try:
        options = find_options(world, seat.pairing_id, seat.role, delay_hours, delay_date)
    except AlreadyStarted as exc:
        return SeatAnalysis(seat, crew_out, impact, None, str(exc), delay_hours, delay_date)
    return SeatAnalysis(seat, crew_out, impact, options, "", delay_hours, delay_date)


def _crew_label(world: World, crew_id: str) -> str:
    crew = world.crew.get(crew_id)
    return f"{crew.name} ({crew_id})" if crew and crew.name else crew_id


def _seat_summary(seats: tuple[SeatAnalysis, ...]) -> str:
    parts = []
    for s in seats:
        head = f"{s.seat.pairing_id} {s.seat.role.value}"
        if s.options is None:
            parts.append(f"{head}: already started")
        elif s.options.recommended:
            rec = s.options.recommended
            parts.append(f"{head}: {rec.action}, {_inr(rec.cost_inr)}")
        else:
            parts.append(f"{head}: no legal cover")
    cancellations = {
        s.seat.pairing_id: s.options.cancellation
        for s in seats
        if s.options is not None and not s.options.recommended
    }
    for pid, cancel in sorted(cancellations.items()):
        parts.append(f"cancelling {pid} ({cancel.name.lower()}) costs {_inr(cancel.cost_inr)}")
    return "; ".join(parts)


def _delay_outcome(
    world: World, effect: DelayEffect, flight_label: str
) -> tuple[list[SeatAnalysis], str]:
    """Seats to re-crew and a one-line summary for one delayed duty day."""
    head = (
        f"{flight_label} on {effect.date}: duty becomes {effect.fdp_hours}h "
        f"against a {effect.fdp_limit}h limit"
    )
    if effect.duty_breaches_limit:
        legs = ", ".join(f.split("-")[0] for f in effect.legs_to_drop)
        return [], (
            f"{head}. No crew can legally fly the whole delayed day: {legs} must come off "
            f"this crew (cancelling costs {_inr(effect.cancellation_cost_inr)}); the remaining "
            "legs still need the usual legality checks"
        )
    seats = [
        _seat_for(
            world,
            Seat(effect.pairing_id, b.role),
            b.crew_id,
            {effect.date},
            effect.delay_hours,
            effect.date,
        )
        for b in effect.breaches
    ]
    if not seats:
        return [], f"{head}. All rostered crew stay legal"
    causes = sorted({b.reasons[0] for b in effect.breaches if b.reasons})
    cause = f" ({'; '.join(causes[:2])})" if causes else ""
    return seats, (
        f"{head}. {len(seats)} crew member(s) become illegal{cause}: " + _seat_summary(tuple(seats))
    )


# --------------------------------------------------------------------------
# Per report type
# --------------------------------------------------------------------------


def _crew_out_seats(world: World, crew_id: str, dates: set[date]) -> list[tuple[Seat, set[date]]]:
    """Seats this crew member holds on pairings with a duty day in ``dates``."""
    seats = []
    for pid, pairing in sorted(world.pairings.items()):
        for seat in pairing.crew:
            if seat.crew_id != crew_id:
                continue
            hit = {d.date for d in pairing.days} & dates
            if hit:
                seats.append((Seat(pid, seat.role), hit))
    return seats


def _with_joint(seats: list[SeatAnalysis], world: World) -> JointPlan | None:
    """A joint plan over seats that can still be re-crewed, using each seat's own options.

    Reusing the per-seat options keeps the plan consistent with them, including under a
    delayed schedule, and avoids searching for every seat twice.
    """
    open_seats = [s for s in seats if s.options is not None]
    if len(open_seats) < 2:
        return None
    return joint_plan(world, [s.seat for s in open_seats], [s.options for s in open_seats])


def _crew_unavailable(world: World, report: Report) -> Analysis:
    d = report.details
    start, end = date.fromisoformat(d["from_date"]), date.fromisoformat(d["to_date"])
    dates = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    who = _crew_label(world, d["crew_id"])
    pairs = _crew_out_seats(world, d["crew_id"], dates)
    if not pairs:
        return Analysis(
            report.report_id,
            report.report_type,
            f"{who} is unavailable {start} to {end} but has no rostered duty then: "
            "nothing to cover.",
        )
    seats = tuple(_seat_for(world, s, d["crew_id"], hit) for s, hit in pairs)
    return Analysis(
        report.report_id,
        report.report_type,
        f"{who} unavailable ({d['reason']}): " + _seat_summary(seats),
        seats=seats,
        joint=_with_joint(list(seats), world),
    )


def _certification_lapse(world: World, report: Report) -> Analysis:
    d = report.details
    valid_to = date.fromisoformat(d["valid_to"])
    who = _crew_label(world, d["crew_id"])
    after = {day.date for p in world.pairings.values() for day in p.days if day.date > valid_to}
    pairs = _crew_out_seats(world, d["crew_id"], after)
    if not pairs:
        return Analysis(
            report.report_id,
            report.report_type,
            f"{who}'s {d['cert_type']} expires {valid_to}: no rostered duty after that date.",
        )
    seats = tuple(_seat_for(world, s, d["crew_id"], hit) for s, hit in pairs)
    return Analysis(
        report.report_id,
        report.report_type,
        f"{who}'s {d['cert_type']} expires {valid_to}: " + _seat_summary(seats),
        seats=seats,
        joint=_with_joint(list(seats), world),
    )


def _delays(
    world: World,
    report: Report,
    delays: dict[tuple[str, date], tuple[float, str, str]],
    affected: list[str],
    lead: str,
) -> Analysis:
    if not delays:
        return Analysis(
            report.report_id,
            report.report_type,
            f"{lead}: no rostered flights affected.",
            affected_flights=tuple(affected),
        )
    effects, seats, lines = [], [], []
    for (pid, day), (hours, label, flight_id) in sorted(delays.items()):
        if hours <= 0:
            continue
        try:
            effect = delay_effect(world, pid, day, hours, from_flight=flight_id)
        except ValueError as exc:
            lines.append(f"{label} on {day}: not assessed ({exc})")
            continue
        effects.append(effect)
        new_seats, line = _delay_outcome(world, effect, label)
        seats.extend(new_seats)
        lines.append(line)
    return Analysis(
        report.report_id,
        report.report_type,
        f"{lead}. " + ". ".join(lines) + ".",
        seats=tuple(seats),
        joint=_with_joint(seats, world),
        delays=tuple(effects),
        affected_flights=tuple(affected),
    )


def _flight_delay(world: World, report: Report) -> Analysis:
    d = report.details
    index = _flight_index(world)
    fid = d["flight_id"]
    hours = d["delay_minutes"] / 60
    lead = f"{fid.split('-')[0]} delayed {d['delay_minutes']} min"
    if fid not in index:
        return Analysis(
            report.report_id,
            report.report_type,
            f"{lead}: the flight is not on any pairing.",
            affected_flights=(fid,),
        )
    pid, day = index[fid]
    return _delays(world, report, {(pid, day): (hours, fid.split("-")[0], fid)}, [fid], lead)


def _aircraft_technical(world: World, report: Report) -> Analysis:
    d = report.details
    start, end = (
        datetime.fromisoformat(d[k].replace("Z", "+00:00")) for k in ("from_utc", "to_utc")
    )
    index = _flight_index(world)
    hit = sorted(
        (
            f
            for f in world.flights.values()
            if f.aircraft == d["aircraft"] and start <= f.dep_utc < end
        ),
        key=lambda f: f.dep_utc,
    )
    delays: dict[tuple[str, date], tuple[float, str, str]] = {}
    for f in hit:
        if f.flight_id not in index:
            continue
        key = index[f.flight_id]
        if key not in delays:  # the first affected departure sets the day's delay
            hours = _hours_up((end - f.dep_utc).total_seconds())
            delays[key] = (hours, f.flight_no, f.flight_id)
    lead = f"{d['aircraft']} unavailable {_iso(start)} to {_iso(end)}"
    return _delays(world, report, delays, [f.flight_id for f in hit], lead)


def _station_disruption(world: World, report: Report) -> Analysis:
    d = report.details
    start, end = (
        datetime.fromisoformat(d[k].replace("Z", "+00:00")) for k in ("from_utc", "to_utc")
    )
    station = d["station"]
    index = _flight_index(world)
    worst: dict[tuple[str, date], tuple[float, str, str]] = {}
    affected = []
    for f in sorted(world.flights.values(), key=lambda f: f.dep_utc):
        moment = None
        if f.dep_station == station and start <= f.dep_utc < end:
            moment = f.dep_utc
        elif f.arr_station == station and start <= f.arr_utc < end:
            moment = f.arr_utc
        if moment is None:
            continue
        affected.append(f.flight_id)
        if f.flight_id in index:
            ready = end + STATION_REOPEN_TURNAROUND
            hours = _hours_up((ready - moment).total_seconds())
            key = index[f.flight_id]
            if key not in worst:
                # Flights are visited in departure order, so the first one seen is the
                # day's earliest affected leg: it and every later leg slip.
                worst[key] = (hours, f.flight_no, f.flight_id)
            elif hours > worst[key][0]:
                worst[key] = (hours, worst[key][1], worst[key][2])
    lead = (
        f"{station} closed {_iso(start)} to {_iso(end)} ({d['reason']}), "
        f"{len(affected)} flights affected"
    )
    return _delays(world, report, dict(worst), affected, lead)


def _gate_change(world: World, report: Report, gates: tuple[GateSlot, ...]) -> Analysis:
    d = report.details
    fid, new_gate = d["flight_id"], d["new_gate"]
    mine = next((g for g in gates if g.flight_id == fid), None)
    label = fid.split("-")[0]
    if mine is None:
        return Analysis(
            report.report_id,
            report.report_type,
            f"{label} moved to {new_gate}: no boarding slot on record for this flight.",
            affected_flights=(fid,),
        )
    clashes = [
        g
        for g in gates
        if g.gate == new_gate
        and g.flight_id != fid
        and g.start_utc < mine.end_utc
        and mine.start_utc < g.end_utc
    ]
    if not clashes:
        summary = (
            f"{label} moved from {mine.gate} to {new_gate}: the gate is free for its boarding "
            "window. Crew legality is unaffected."
        )
        return Analysis(report.report_id, report.report_type, summary, affected_flights=(fid,))
    taken = ", ".join(c.flight_id.split("-")[0] for c in clashes)
    return Analysis(
        report.report_id,
        report.report_type,
        f"{label} moved from {mine.gate} to {new_gate}, but {new_gate} is held by {taken} "
        "during its boarding window. Choose another gate, or the departure slips.",
        affected_flights=(fid, *[c.flight_id for c in clashes]),
        notes=tuple(
            f"{new_gate} held by {c.flight_id} {_iso(c.start_utc)} to {_iso(c.end_utc)}"
            for c in clashes
        ),
    )


def analyse(world: World, report: Report, gates: tuple[GateSlot, ...] = ()) -> Analysis:
    """The analysis of one report against the current operation."""
    kind = report.report_type
    if kind is ReportType.CREW_UNAVAILABLE:
        return _crew_unavailable(world, report)
    if kind is ReportType.CERTIFICATION_LAPSE:
        return _certification_lapse(world, report)
    if kind is ReportType.FLIGHT_DELAY:
        return _flight_delay(world, report)
    if kind is ReportType.AIRCRAFT_TECHNICAL:
        return _aircraft_technical(world, report)
    if kind is ReportType.STATION_DISRUPTION:
        return _station_disruption(world, report)
    return _gate_change(world, report, gates)


__all__ = ["Analysis", "SeatAnalysis", "analyse", "option_dict"]
