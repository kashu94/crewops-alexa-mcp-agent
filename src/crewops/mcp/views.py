"""What a disruption looks like to the desk: who owns it, what needs covering, and who else
wants the same crew.

Everything here is read from the stored analysis and the loaded operation; nothing is
decided. The contention check is the cross-controller part: an option that another open
disruption also ranks, or that another submitted plan already uses, is shown with that
disruption's desk, so two controllers do not call the same person for two trips.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from crewops.domain.desks import desks_for
from crewops.store import plans as plan_store
from crewops.store.items import from_dynamo
from crewops.store.reports import disruptions_with_status

OPEN = ("REPORTED", "OPTIONS_READY", "SUBMITTED")


def near(ours: set[str], theirs: Iterable[str]) -> bool:
    """Trips on the same or adjacent days compete: one person cannot fly both, and the rest
    rule links consecutive days."""
    mine = {date.fromisoformat(d) for d in ours}
    return any(abs((date.fromisoformat(d) - m).days) <= 1 for d in theirs for m in mine)


def _rows(tools: Any, entity: str, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        return tools.lookup(entity, dict(filters))["rows"]
    except Exception:  # an id the operation no longer has: show what is known
        return []


def desks_of(tools: Any, pairing_ids: Iterable[str]) -> list[dict[str, str]]:
    aircraft = {
        p["aircraft"]
        for pid in set(pairing_ids)
        for p in _rows(tools, "pairings", {"pairing_id": pid})
    }
    return [{"desk_id": d.desk_id, "controller": d.controller} for d in desks_for(aircraft)]


def seat_context(tools: Any, seat: Mapping[str, Any]) -> dict[str, Any]:
    """The pairing behind a seat: aircraft, dates, each flight's state and the crew on it."""
    pairing = next(iter(_rows(tools, "pairings", {"pairing_id": seat["pairing_id"]})), None)
    if pairing is None:
        return {}
    impact = seat.get("impact") or {}
    uncovered = set(impact.get("uncovered_flights") or [])
    at_risk = set(impact.get("at_risk_flights") or [])
    affected = set(impact.get("affected_dates") or [])
    flight_ids = [f for day in pairing["days"] for f in day["flights"]]
    flights = _rows(tools, "flights", {"flight_id": flight_ids}) if flight_ids else []
    flights.sort(key=lambda f: f["dep_utc"])
    names = {
        c["crew_id"]: c
        for c in _rows(tools, "crew", {"crew_id": [m["crew_id"] for m in pairing["crew"]]})
    }
    aircraft_type = flights[0]["aircraft_type"] if flights else None
    out_id = seat.get("crew_out")
    out_base = names.get(out_id, {}).get("base") if out_id else None
    return {
        "aircraft": pairing["aircraft"],
        "aircraft_type": aircraft_type,
        "base": out_base,
        "dates": [d["date"] for d in pairing["days"]],
        "affected_dates": sorted(affected),
        "requirement": (
            f"A {seat['role']}"
            + (f" rated on the {aircraft_type}" if aircraft_type else "")
            + ", legal for every rule on the affected days."
        ),
        "flights": [
            {
                "flight_id": f["flight_id"],
                "flight_no": f["flight_no"],
                "date": f["date"],
                "from": f["dep_station"],
                "to": f["arr_station"],
                "dep_utc": f["dep_utc"],
                "arr_utc": f["arr_utc"],
                "seats": f["seats"],
                "state": "uncrewed"
                if f["flight_id"] in uncovered
                else "at_risk"
                if f["flight_id"] in at_risk
                else "crewed",
            }
            for f in flights
        ],
        "crew": [
            {
                "crew_id": m["crew_id"],
                "name": names.get(m["crew_id"], {}).get("name"),
                "role": m["role"],
                "state": "out" if m["crew_id"] == out_id and m["role"] == seat["role"] else "on",
            }
            for m in pairing["crew"]
        ],
    }


def contention(
    tools: Any, table: Any, disruption_id: str, wants: list[tuple[set[str], set[str]]]
) -> dict[str, list[dict[str, Any]]]:
    """For each crew id, the other open disruptions that rank them as an option, and the
    submitted plans that already use them, each with its desk. ``wants`` pairs each seat's
    option crew with that seat's dates, so a person is flagged only against trips on the
    same or adjacent days as the seat they could cover."""
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    wants = [(ids, dates) for ids, dates in wants if ids and dates]
    if not wants:
        return {}

    def clashes(crew_id: str, their_dates: Iterable[str]) -> bool:
        theirs = list(their_dates)
        return any(crew_id in ids and near(dates, theirs) for ids, dates in wants)

    seen: set[tuple[str, str, str, str]] = set()

    def add(crew_id: str, entry: dict[str, Any]) -> None:
        key = (crew_id, entry["disruption_id"], entry["pairing_id"], entry["kind"])
        if key not in seen:
            seen.add(key)
            found[crew_id].append(entry)

    for status in OPEN:
        for item in disruptions_with_status(table, status):
            other = item["disruption_id"]
            if other == disruption_id:
                continue
            seats = from_dynamo(item.get("analysis") or {}).get("seats", [])
            desks = [d["desk_id"] for d in desks_of(tools, [s["pairing_id"] for s in seats])]
            for s in seats:
                their_dates = (s.get("impact") or {}).get("affected_dates") or []
                for o in s.get("options", []):
                    if o.get("crew_id") and clashes(o["crew_id"], their_dates):
                        add(
                            o["crew_id"],
                            {
                                "disruption_id": other,
                                "status": item["status"],
                                "pairing_id": s["pairing_id"],
                                "rank": o.get("rank"),
                                "desks": desks,
                                "kind": "option",
                            },
                        )
    for plan in plan_store.plans_with_status(table, plan_store.PLAN_SUBMITTED):
        if plan["disruption_id"] == disruption_id:
            continue
        for a in plan.get("assignments", []):
            pairing = next(iter(_rows(tools, "pairings", {"pairing_id": a["pairing_id"]})), None)
            if a.get("crew_id") and pairing and clashes(a["crew_id"], pairing["dates"]):
                add(
                    a["crew_id"],
                    {
                        "disruption_id": plan["disruption_id"],
                        "status": "SUBMITTED",
                        "pairing_id": a["pairing_id"],
                        "plan_id": plan["plan_id"],
                        "desks": [plan.get("desk")],
                        "kind": "plan",
                    },
                )
    return dict(found)
