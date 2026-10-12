"""How serious a disruption is: high, medium or low, with the reasons, from its analysis.

Worked out from what the engine found, never guessed by a model, so the same disruption is
always rated the same and every rating can be explained:

* **High** when a flight is at risk however it is handled: no legal crew for a seat, the
  best way out is to cancel or fly a cabin crew member short, a joint plan leaves a seat
  unfilled, or a delay breaks a duty limit; or when enough lesser factors add up.
* **Medium** when it needs a real decision: cover only from outside today's reserves
  (a day-off callout, positioning, a command upgrade), it flies within a day, it touches
  many passengers or several seats, or departures leave late.
* **Low** when a reserve covers it with time to spare, or there is nothing to cover.

Computed when read (the console, the speaker, Alexa and desktop alerts), so disruptions
analysed before this existed are rated too.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

HIGH, MEDIUM, LOW = "high", "medium", "low"
LEVELS = (HIGH, MEDIUM, LOW)
MANY_PASSENGERS = 300
LONG_DELAY_MINUTES = 120
MANY_SEATS = 3
BEYOND_RESERVE = {
    "day_off_callout": "no reserve free: a day-off callout",
    "positioning": "cover must position from another base",
    "command_upgrade": "needs a command-qualified first officer",
    "right_seat": "needs a captain in the right seat",
}


def _first_date(seats: list[Mapping[str, Any]]) -> date | None:
    days = [d for s in seats for d in (s.get("impact") or {}).get("affected_dates") or []]
    return date.fromisoformat(min(days)) if days else None


ACTIVE = ("options", "no_legal_cover")
"""Seats still to cover; a trip already flying or cancelled adds nothing to the urgency."""


def severity(
    analysis: Mapping[str, Any], reported_utc: str | None = None, today: date | None = None
) -> dict[str, Any]:
    """``{"level": "high" | "medium" | "low", "reasons": [...]}`` for one analysis.

    How soon it flies is counted from ``today`` when given (a disruption read later is
    nearer its flights than when it was reported), else from when it was reported."""
    seats = [s for s in analysis.get("seats") or [] if s.get("status", "options") in ACTIVE]
    hard: list[str] = []
    soft: list[str] = []

    for s in seats:
        pairing = s.get("pairing_id", "a trip")
        if s.get("status") == "no_legal_cover":
            hard.append(f"no legal crew for {pairing}")
            continue
        strategy = (s.get("recommended") or {}).get("strategy")
        if strategy == "cancel":
            hard.append(f"the best way out for {pairing} is to cancel")
        elif strategy == "short_crew":
            hard.append(f"{pairing} would fly one cabin crew short")
        elif strategy in BEYOND_RESERVE:
            soft.append(BEYOND_RESERVE[strategy])
    unfilled = ((analysis.get("joint_plan") or {}).get("unfilled")) or []
    if unfilled:
        hard.append(f"{len(unfilled)} seat{'s' if len(unfilled) != 1 else ''} left unfilled")
    for d in analysis.get("delays") or []:
        if d.get("duty_breaches_limit") or d.get("legs_to_drop"):
            hard.append(f"the delay breaks a duty limit on {d.get('pairing_id', 'a trip')}")

    # Per trip, once: two seats on one trip are the same passengers.
    per_trip: dict[str, int] = {}
    for s in seats:
        booked = int((s.get("impact") or {}).get("passengers") or 0)
        trip = str(s.get("pairing_id"))
        per_trip[trip] = max(per_trip.get(trip, 0), booked)
    passengers = sum(per_trip.values())
    if passengers >= MANY_PASSENGERS:
        soft.append(f"{passengers} passengers booked")
    if len(seats) >= MANY_SEATS:
        soft.append(f"{len(seats)} seats to cover")
    first = _first_date(seats)
    start = today
    if start is None and reported_utc:
        try:
            start = date.fromisoformat(reported_utc[:10])
        except ValueError:
            start = None
    if first and start:
        lead = (first - start).days
        if 0 <= lead <= 1:
            soft.append("flies today" if lead == 0 else "flies tomorrow")
    late = [int(leg.get("minutes") or 0) for leg in analysis.get("late_departures") or []]
    if late and max(late) >= LONG_DELAY_MINUTES:
        soft.append(f"departures up to {max(late) // 60}h{max(late) % 60:02d} late")
    elif late:
        soft.append(f"{len(late)} departure{'s' if len(late) != 1 else ''} late")

    # One reason each, in a steady order.
    reasons = list(dict.fromkeys(hard + soft))
    if hard or len(soft) >= 3:
        level = HIGH
    elif soft:
        level = MEDIUM
    else:
        level = LOW
        if not seats and not late:
            reasons = ["nothing to cover"]
        else:
            reasons = reasons or ["a reserve covers it"]
    return {"level": level, "reasons": reasons[:4]}


def spoken_level(level: str) -> str:
    return {HIGH: "High severity", MEDIUM: "Medium severity", LOW: "Low severity"}.get(level, "")
