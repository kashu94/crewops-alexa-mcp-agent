"""Submit, withdraw, reject and approve plans, with every rule enforced here, in code.

* **Only people decide.** Nothing here is a model tool: the advisor and the analysis only
  prepare options. Every call names a signed-in ``Caller``.
* **Controllers submit, for their desk.** A disruption belongs to every desk whose aircraft
  it touches; any of those desks' controllers may plan it.
* **Every seat accounted for.** A seat is a pairing, a role and the crew member being
  replaced. A seat with no legal cover (or that has already started) is listed in the plan
  as uncovered, and the controller must say so explicitly to submit. A disruption with no
  coverable seat at all can be closed with a reason, recording what was done instead.
* **Four eyes.** An approver decides, never on a plan they submitted. Only the submitter may
  withdraw.
* **Legal now, and again at approval.** Each choice must be a legal option when submitted
  and is checked again against the roster as it is at approval. If anything changed, the
  plan becomes STALE and nothing is written.
* **Approving is committing.** The roster changes, the plan, the disruption and the audit
  are one transaction, conditioned on each pairing's crew and each moved crew member's
  roster version being what the re-check saw. Two approvals, or an approval and a sick
  report, touching the same person cannot both succeed.
* **Contention is shown.** When another open plan or another disruption's proposed cover
  wants the same crew member on overlapping dates, the submitter is told, and the approver
  sees the same warnings in their queue.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from crewops.analysis.runner import Operation
from crewops.domain import Rank, Role
from crewops.domain.desks import DESKS, Desk
from crewops.engine import AlreadyStarted, World, assess, find_options
from crewops.store import plans as store
from crewops.store.items import from_dynamo
from crewops.store.reports import STATUS_OPTIONS_READY, disruptions_with_status, get_disruption
from crewops.tools import Caller

MAX_REASON = 1000
MAX_SEATS = 20
"""Most seats one plan covers. A commit writes each seat's cover and replaced crew versions,
each pairing and four more items, and DynamoDB allows 100 per transaction."""


class PlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    plan: dict[str, Any]
    warnings: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def seat_key(seat: Mapping[str, Any]) -> str:
    """ "P-2291/Captain/C-1042": pairing, role and who is replaced, so two seats of one role
    on one pairing are told apart."""
    return f"{seat['pairing_id']}/{seat['role']}/{seat.get('crew_out') or '-'}"


def _desk(desk_id: str | None) -> Desk | None:
    return next((d for d in DESKS if d.desk_id == desk_id), None)


def _roster(world: World, pairing_id: str) -> list[dict[str, str]]:
    return [{"crew_id": s.crew_id, "role": s.role.value} for s in world.pairings[pairing_id].crew]


def _dates(world: World, pairing_id: str) -> set[date]:
    p = world.pairings.get(pairing_id)
    return {d.date for d in p.days} if p else set()


class Plans:
    def __init__(
        self,
        table: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        load: Callable[[Any], Operation] = Operation.load,
    ) -> None:
        self.table = table
        self.clock = clock
        self.load = load

    # ------------------------------------------------------------------
    # Controller actions
    # ------------------------------------------------------------------

    def submit(
        self,
        caller: Caller,
        disruption_id: str,
        choices: Mapping[str, str] | None = None,
        note: str = "",
        accept_uncovered: bool = False,
    ) -> Outcome:
        """A plan from the disruption's analysis: the proposed cover (the joint plan when
        there is one), with any seat overridden by ``choices`` (seat key, or "pairing/role"
        when that names one seat, -> crew id)."""
        desk = self._controller_desk(caller)
        item = self._disruption(disruption_id)
        if item["status"] != STATUS_OPTIONS_READY:
            raise PlanError(
                "WRONG_STATE", f"{disruption_id} is {item['status']}, not ready for a plan"
            )
        analysis = from_dynamo(item.get("analysis") or {})
        world = self.load(self.table).world
        seats = analysis.get("seats", [])
        self._check_desk(desk, world, seats, disruption_id)

        coverable, uncovered = [], []
        for seat in seats:
            reason = self._why_uncoverable(world, seat)
            (uncovered if reason else coverable).append((seat, reason))
        if not coverable:
            raise PlanError(
                "NOTHING_TO_COVER",
                f"{disruption_id} has no seat that can be covered; close it with a reason "
                "once it is handled",
            )
        if uncovered and not accept_uncovered:
            listing = "; ".join(f"{seat_key(s)}: {r}" for s, r in uncovered)
            raise PlanError(
                "UNCOVERED",
                f"these seats cannot be covered: {listing}. Submit with them acknowledged, "
                "or handle them first",
            )

        if len(coverable) > MAX_SEATS:
            raise PlanError(
                "TOO_LARGE",
                f"{len(coverable)} seats is more than one plan can commit at once ({MAX_SEATS})",
            )
        picks = self._picks([s for s, _ in coverable], analysis, choices or {})
        assignments = [
            self._legal_choice(world, seat, picks[seat_key(seat)]) for seat, _ in coverable
        ]
        chosen = [a["crew_id"] for a in assignments]
        if len(set(chosen)) != len(chosen):
            raise PlanError("INVALID", "one crew member is chosen for two seats")

        plan = {
            "plan_id": f"pl-{uuid.uuid4().hex[:12]}",
            "disruption_id": disruption_id,
            "status": store.PLAN_SUBMITTED,
            "desk": desk.desk_id,
            "submitted_by": caller.user_id,
            "submitted_utc": self._now(),
            "note": note.strip()[:MAX_REASON],
            "assignments": assignments,
            "uncovered": [{"seat": seat_key(s), "reason": r} for s, r in uncovered],
            "total_cost_inr": sum(a["cost_inr"] for a in assignments),
            "rosters_before": {
                pid: _roster(world, pid) for pid in sorted({a["pairing_id"] for a in assignments})
            },
        }
        warnings = self.contention(plan, world)
        plan["warnings"] = list(warnings)
        try:
            store.submit(self.table, plan, self.clock())
        except store.Conflict:
            raise PlanError("CONFLICT", f"{disruption_id} changed meanwhile; look again") from None
        return Outcome(store.PLAN_SUBMITTED, plan, warnings)

    def withdraw(
        self, caller: Caller, disruption_id: str, plan_id: str, reason: str = ""
    ) -> Outcome:
        plan = self._submitted(disruption_id, plan_id)
        if plan["submitted_by"] != caller.user_id:
            raise PlanError("FORBIDDEN", "only the controller who submitted a plan can withdraw it")
        return self._close(plan, store.PLAN_WITHDRAWN, caller, reason or "withdrawn")

    def close(self, caller: Caller, disruption_id: str, reason: str) -> Outcome:
        """End a disruption that has no coverable seat, recording what was done instead (for
        example, the flights were cancelled). Nothing on the roster changes, and a crew
        member it took out stays out on its dates."""
        desk = self._controller_desk(caller)
        item = self._disruption(disruption_id)
        if item["status"] != STATUS_OPTIONS_READY:
            raise PlanError("WRONG_STATE", f"{disruption_id} is {item['status']}")
        if not reason.strip():
            raise PlanError("INVALID", "say what was done instead")
        analysis = from_dynamo(item.get("analysis") or {})
        world = self.load(self.table).world
        self._check_desk(desk, world, analysis.get("seats", []), disruption_id)
        coverable = [s for s in analysis.get("seats", []) if not self._why_uncoverable(world, s)]
        if coverable:
            raise PlanError(
                "WRONG_STATE",
                f"{disruption_id} still has legal cover for "
                f"{', '.join(seat_key(s) for s in coverable)}: submit a plan instead",
            )
        try:
            store.close_disruption(
                self.table, disruption_id, caller.user_id, self.clock(), reason[:MAX_REASON]
            )
        except store.Conflict:
            raise PlanError("CONFLICT", f"{disruption_id} changed meanwhile") from None
        return Outcome(store.STATUS_CLOSED, {"disruption_id": disruption_id, "reason": reason})

    # ------------------------------------------------------------------
    # Approver actions
    # ------------------------------------------------------------------

    def reject(self, caller: Caller, disruption_id: str, plan_id: str, reason: str) -> Outcome:
        plan = self._submitted(disruption_id, plan_id)
        self._check_approver(caller, plan)
        if not reason.strip():
            raise PlanError("INVALID", "say why the plan is rejected")
        return self._close(plan, store.PLAN_REJECTED, caller, reason)

    def approve(self, caller: Caller, disruption_id: str, plan_id: str) -> Outcome:
        """Re-check against the roster as it is now; commit if nothing changed, else STALE."""
        plan = self._submitted(disruption_id, plan_id)
        self._check_approver(caller, plan)
        moved = {a["crew_id"] for a in plan["assignments"]} | {
            a["replaces"] for a in plan["assignments"] if a.get("replaces")
        }
        # Read before the re-check: a change after it fails the commit's condition.
        versions = store.read_versions(self.table, moved)
        world = self.load(self.table).world
        reasons = self._still_valid(world, plan)
        rosters = {} if reasons else self._rosters_after(plan, reasons)
        if reasons:
            return self._close(plan, store.PLAN_STALE, caller, "; ".join(reasons), reasons)
        try:
            store.commit(self.table, plan, rosters, versions, caller.user_id, self.clock())
        except store.Conflict as exc:
            if exc.transient:
                raise PlanError(
                    "RETRY", "another change touched the same plan or crew just now; try again"
                ) from None
            current = store.get_plan(self.table, disruption_id, plan_id)
            if current and current["status"] == store.PLAN_SUBMITTED:
                why = ["the roster or a crew member's availability changed during approval"]
                return self._close(current, store.PLAN_STALE, caller, why[0], why)
            raise PlanError("CONFLICT", "the plan was decided meanwhile") from None
        return Outcome(store.PLAN_COMMITTED, plan | {"status": store.PLAN_COMMITTED})

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def queue(self, caller: Caller) -> list[dict[str, Any]]:
        """What is waiting for this person: plans to decide (approver), disruptions on their
        desk ready for a plan, and their plans sent back while still unresolved
        (controller)."""
        out: list[dict[str, Any]] = []
        world: World | None = None
        if Role.APPROVER in caller.roles:
            world = self.load(self.table).world
            for plan in store.plans_with_status(self.table, store.PLAN_SUBMITTED):
                if plan["submitted_by"] != caller.user_id:
                    warnings = self.contention(plan, world)
                    out.append({"kind": "to_decide", **summary(plan), "warnings": list(warnings)})
        desk = _desk(caller.desk) if Role.CONTROLLER in caller.roles else None
        if desk is not None:
            world = world or self.load(self.table).world
            ready = {}
            for item in disruptions_with_status(self.table, STATUS_OPTIONS_READY):
                analysis = from_dynamo(item.get("analysis") or {})
                if self._touches(desk, world, analysis.get("seats", [])):
                    ready[item["disruption_id"]] = item
                    out.append(
                        {
                            "kind": "ready_for_plan",
                            "disruption_id": item["disruption_id"],
                            "summary": analysis.get("summary", ""),
                        }
                    )
            for status in (store.PLAN_REJECTED, store.PLAN_STALE):
                for plan in store.plans_with_status(self.table, status):
                    if (
                        plan["submitted_by"] == caller.user_id
                        and plan["disruption_id"] in ready
                        and self._latest(plan)
                    ):
                        out.append({"kind": f"sent_back_{status.lower()}", **summary(plan)})
        return out

    def contention(self, plan: Mapping[str, Any], world: World) -> tuple[str, ...]:
        """Other open plans, and other disruptions' proposed cover, wanting the same crew on
        dates that overlap."""
        wanted: dict[str, set[date]] = defaultdict(set)
        for a in plan["assignments"]:
            wanted[a["crew_id"]] |= _dates(world, a["pairing_id"])

        def clash(crew_id: str, pairing_id: str) -> bool:
            return crew_id in wanted and bool(wanted[crew_id] & _dates(world, pairing_id))

        warnings = []
        for other in store.plans_with_status(self.table, store.PLAN_SUBMITTED):
            if other["disruption_id"] == plan["disruption_id"]:
                continue
            for a in other["assignments"]:
                if clash(a["crew_id"], a["pairing_id"]):
                    warnings.append(
                        f"{a['crew_id']} is also in plan {other['plan_id']} for "
                        f"{other['disruption_id']} ({other['desk']}), awaiting approval"
                    )
        for item in disruptions_with_status(self.table, STATUS_OPTIONS_READY):
            if item["disruption_id"] == plan["disruption_id"]:
                continue
            analysis = from_dynamo(item.get("analysis") or {})
            proposed = [
                (a["crew_id"], a["pairing_id"])
                for a in (analysis.get("joint_plan") or {}).get("assignments", [])
            ] or [
                (s["recommended"]["crew_id"], s["pairing_id"])
                for s in analysis.get("seats", [])
                if s.get("recommended")
            ]
            for crew_id, pairing_id in proposed:
                if clash(crew_id, pairing_id):
                    warnings.append(
                        f"{crew_id} is also the proposed cover for {item['disruption_id']} "
                        f"({pairing_id})"
                    )
        return tuple(dict.fromkeys(warnings))

    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _disruption(self, disruption_id: str) -> dict[str, Any]:
        item = get_disruption(self.table, disruption_id)
        if item is None:
            raise PlanError("NOT_FOUND", f"no disruption {disruption_id}")
        return item

    def _submitted(self, disruption_id: str, plan_id: str) -> dict[str, Any]:
        plan = store.get_plan(self.table, disruption_id, plan_id)
        if plan is None:
            raise PlanError("NOT_FOUND", f"no plan {plan_id} for {disruption_id}")
        if plan["status"] != store.PLAN_SUBMITTED:
            raise PlanError(
                "WRONG_STATE", f"plan {plan_id} is {plan['status']}, not awaiting a decision"
            )
        return plan

    def _latest(self, plan: Mapping[str, Any]) -> bool:
        plans = store.plans_for(self.table, plan["disruption_id"])
        latest = max(plans, key=lambda p: (p["submitted_utc"], p["plan_id"]))
        return latest["plan_id"] == plan["plan_id"]

    @staticmethod
    def _controller_desk(caller: Caller) -> Desk:
        desk = _desk(caller.desk) if Role.CONTROLLER in caller.roles else None
        if desk is None:
            raise PlanError("FORBIDDEN", "only a controller with a desk can do this")
        return desk

    @staticmethod
    def _check_approver(caller: Caller, plan: Mapping[str, Any]) -> None:
        if Role.APPROVER not in caller.roles:
            raise PlanError("FORBIDDEN", "only an approver can decide on a plan")
        if plan["submitted_by"] == caller.user_id:
            raise PlanError("FOUR_EYES", "a plan is decided by someone other than its submitter")

    @staticmethod
    def _touches(desk: Desk, world: World, seats: list[dict[str, Any]]) -> bool:
        return any(
            s["pairing_id"] in world.pairings
            and world.pairings[s["pairing_id"]].aircraft in desk.aircraft
            for s in seats
        )

    def _check_desk(
        self, desk: Desk, world: World, seats: list[dict[str, Any]], disruption_id: str
    ) -> None:
        if not self._touches(desk, world, seats):
            raise PlanError("FORBIDDEN", f"{disruption_id} touches no aircraft on {desk.desk_id}")

    @staticmethod
    def _why_uncoverable(world: World, seat: Mapping[str, Any]) -> str | None:
        if seat.get("status") != "options":
            return {"no_legal_cover": "no legal cover", "already_started": "already started"}.get(
                seat.get("status", ""), str(seat.get("status"))
            )
        out, pid = seat.get("crew_out"), seat["pairing_id"]
        if not out:
            return "no rostered crew member to replace"
        if pid not in world.pairings or {"crew_id": out, "role": seat["role"]} not in _roster(
            world, pid
        ):
            return f"{out} is no longer rostered there: the analysis is out of date"
        return None

    @staticmethod
    def _picks(
        seats: list[dict[str, Any]], analysis: Mapping[str, Any], choices: Mapping[str, str]
    ) -> dict[str, str]:
        """The crew for each seat: an explicit choice, else the joint plan's (matched to
        seats of the same pairing and role in order), else the seat's recommendation."""
        keys = {seat_key(s): s for s in seats}
        short: dict[str, list[str]] = defaultdict(list)
        for key, s in keys.items():
            short[f"{s['pairing_id']}/{s['role']}"].append(key)
        resolved: dict[str, str] = {}
        for given, crew_id in choices.items():
            if given in keys:
                resolved[given] = crew_id
            elif len(short.get(given, [])) == 1:
                resolved[short[given][0]] = crew_id
            elif given in short:
                raise PlanError(
                    "INVALID", f"{given} names several seats; use one of {', '.join(short[given])}"
                )
            else:
                raise PlanError("INVALID", f"no such seat: {given}")
        joint: dict[str, list[str]] = defaultdict(list)
        for a in (analysis.get("joint_plan") or {}).get("assignments", []):
            joint[f"{a['pairing_id']}/{a['role']}"].append(a["crew_id"])
        picks = {}
        for key, s in keys.items():
            group = f"{s['pairing_id']}/{s['role']}"
            fallback = joint[group].pop(0) if joint.get(group) else None
            crew_id = resolved.get(key) or fallback or (s.get("recommended") or {}).get("crew_id")
            if not crew_id:
                raise PlanError("INVALID", f"no cover chosen for {key}")
            picks[key] = crew_id
        return picks

    @staticmethod
    def _delay(seat: Mapping[str, Any]) -> tuple[float, date | None]:
        on = seat.get("delay_date")
        return float(seat.get("delay_hours") or 0), date.fromisoformat(on) if on else None

    def _legal_choice(self, world: World, seat: dict[str, Any], crew_id: str) -> dict[str, Any]:
        """The chosen crew member as a legal option for the seat now, with its cost."""
        hours, on = self._delay(seat)
        try:
            result = find_options(world, seat["pairing_id"], Rank(seat["role"]), hours, on)
        except AlreadyStarted as exc:
            raise PlanError("NOT_LEGAL", str(exc)) from None
        option = next((o for o in result.crew_options if o.crew_id == crew_id), None)
        if option is None:
            why = next((e.reasons for e in result.excluded if e.crew_id == crew_id), ())
            detail = f": {'; '.join(why)}" if why else ""
            raise PlanError(
                "NOT_LEGAL",
                f"{crew_id} is not a legal option for {seat_key(seat)}{detail}",
            )
        return {
            "pairing_id": seat["pairing_id"],
            "role": seat["role"],
            "crew_id": crew_id,
            "name": option.name,
            "replaces": seat["crew_out"],
            "strategy": option.strategy.value,
            "cost_inr": option.cost_inr,
            "delay_hours": hours,
            "delay_date": on.isoformat() if on else None,
        }

    @staticmethod
    def _still_valid(world: World, plan: Mapping[str, Any]) -> list[str]:
        reasons = []
        for pid, before in plan["rosters_before"].items():
            if pid not in world.pairings or _roster(world, pid) != before:
                reasons.append(f"the crew of {pid} changed after the plan was submitted")
        for a in plan["assignments"]:
            on = date.fromisoformat(a["delay_date"]) if a.get("delay_date") else None
            try:
                check = assess(world, a["crew_id"], a["pairing_id"], float(a["delay_hours"]), on)
            except Exception as exc:  # the crew member or pairing is gone
                reasons.append(f"{a['crew_id']} on {a['pairing_id']}: {exc}")
                continue
            if not check.legal:
                details = "; ".join(v.detail for v in check.blocking)
                reasons.append(
                    f"{a['crew_id']} is no longer legal for {a['pairing_id']}: {details}"
                )
        return reasons

    @staticmethod
    def _rosters_after(
        plan: Mapping[str, Any], reasons: list[str]
    ) -> dict[str, tuple[list[dict[str, str]], list[dict[str, str]]]]:
        """Each pairing's crew with every replaced seat taken by its cover. Never adds a
        seat: a replaced crew member no longer there is a reason the plan is stale."""
        rosters = {}
        for pid, before in plan["rosters_before"].items():
            after = [dict(seat) for seat in before]
            for a in (a for a in plan["assignments"] if a["pairing_id"] == pid):
                seat = next(
                    (s for s in after if s["role"] == a["role"] and s["crew_id"] == a["replaces"]),
                    None,
                )
                if seat is None:
                    reasons.append(f"{a['replaces']} is no longer on {pid} as {a['role']}")
                    continue
                seat["crew_id"] = a["crew_id"]
            ids = [s["crew_id"] for s in after]
            if len(after) != len(before) or len(set(ids)) != len(ids):
                reasons.append(f"covering {pid} would leave it with a duplicate or missing seat")
            rosters[pid] = (before, after)
        return rosters

    def _close(
        self,
        plan: dict[str, Any],
        status: str,
        caller: Caller,
        reason: str,
        reasons: list[str] | None = None,
    ) -> Outcome:
        try:
            store.close(self.table, plan, status, caller.user_id, self.clock(), reason[:MAX_REASON])
        except store.Conflict:
            raise PlanError("CONFLICT", "the plan was decided meanwhile") from None
        return Outcome(status, plan | {"status": status}, (), tuple(reasons or ()))


def summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "disruption_id": plan["disruption_id"],
        "plan_id": plan["plan_id"],
        "desk": plan["desk"],
        "submitted_by": plan["submitted_by"],
        "total_cost_inr": plan["total_cost_inr"],
        "assignments": [
            f"{a['pairing_id']} {a['role']}: {a['name']} ({a['crew_id']}) replacing "
            f"{a['replaces']}, ₹{a['cost_inr']:,}"
            for a in plan["assignments"]
        ],
        "uncovered": [f"{u['seat']}: {u['reason']}" for u in plan.get("uncovered", [])],
        "reason": plan.get("reason"),
    }
