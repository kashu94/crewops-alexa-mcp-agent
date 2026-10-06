"""Who could cover this seat, what each choice costs, and why everyone else is out.

``find_options`` assesses every crew member of the needed rank against the pairing and
returns the legal ones ranked by cost, with cancellation always listed last as the explicit
baseline. Every figure a controller might want spoken is computed here, never by a model:
the premium of the next tier, how many times cheaper covering is than cancelling, how many
options tie on cost.

Ranking is by cost, then departure delay, then crew id for a stable order. Seniority and
the provided risk score are shown next to each option but never change its rank.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from crewops.domain import Rank
from crewops.engine.legality import Assessment, assess
from crewops.engine.rules import BASE, CERT, DUTY, FDP, FLIGHT, QUAL, REST
from crewops.engine.world import World


class Strategy(StrEnum):
    RESERVE_CALLOUT = "reserve_callout"
    DAY_OFF_CALLOUT = "day_off_callout"
    POSITIONING = "positioning"
    CANCEL = "cancel"

    @property
    def label(self) -> str:
        return {
            Strategy.RESERVE_CALLOUT: "Reserve callout",
            Strategy.DAY_OFF_CALLOUT: "Day-off callout",
            Strategy.POSITIONING: "Position from another base",
            Strategy.CANCEL: "Cancel",
        }[self]


FUNNEL = (
    ("qualified", (QUAL,), "not rated on the aircraft or not active"),
    ("certified", (CERT,), "a certification is invalid on a duty date"),
    ("in position", (BASE,), "cannot get to the departure in time"),
    ("rested", (REST,), "rest conflict or already rostered"),
    ("within limits", (FDP, DUTY, FLIGHT), "would breach a duty or flight-hour limit"),
)
"""The order a controller thinks in: can they fly this type, are they current, can they get
there, are they free, and do they have the hours. Each excluded candidate drops at the
first stage they fail."""


@dataclass(frozen=True, slots=True)
class Option:
    rank: int
    strategy: Strategy
    crew_id: str | None
    name: str
    crew_rank: Rank | None
    base: str | None
    cost_inr: int
    cost_breakdown: dict[str, int]
    delay_hours: float
    """How late the first departure leaves with this choice."""
    positioning_flight: str | None = None
    reachability_minutes: int | None = None
    seniority: int | None = None
    risk_score: float | None = None
    resilience: float | None = None
    """0-100: how much of today's reserve cover for this rank and base survives the choice."""

    @property
    def action(self) -> str:
        if self.crew_id is None:
            return self.name
        who = f"{self.crew_rank} {self.name} ({self.crew_id})"
        if self.strategy is Strategy.POSITIONING:
            return (
                f"Assign {who}, positioning from {self.base} "
                f"(first departure ~{self.delay_hours:g}h late)"
            )
        return f"Assign {who} ({self.strategy.label.lower()})"


@dataclass(frozen=True, slots=True)
class Exclusion:
    crew_id: str
    name: str
    stage: str
    rule_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FunnelStage:
    stage: str
    remaining: int
    dropped: int
    reason: str


@dataclass(frozen=True, slots=True)
class OptionsResult:
    pairing_id: str
    role: Rank
    options: tuple[Option, ...]
    """Legal crew options ranked by cost, then cancellation last."""
    strategies: tuple[Option, ...]
    """The cheapest legal option of each strategy, then cancellation."""
    excluded: tuple[Exclusion, ...]
    funnel: tuple[FunnelStage, ...]
    policies: dict[str, Option] = field(default_factory=dict)

    @property
    def crew_options(self) -> tuple[Option, ...]:
        return tuple(o for o in self.options if o.crew_id is not None)

    @property
    def recommended(self) -> Option | None:
        return self.crew_options[0] if self.crew_options else None

    @property
    def cancellation(self) -> Option:
        return self.options[-1]

    @property
    def next_tier_premium_inr(self) -> int:
        """Extra cost of the next-cheapest price tier over the recommendation."""
        tiers = sorted({o.cost_inr for o in self.crew_options})
        return tiers[1] - tiers[0] if len(tiers) > 1 else 0

    @property
    def equal_cost_alternatives(self) -> int:
        """Other legal options exactly as cheap as the recommendation."""
        if not self.recommended:
            return 0
        cheapest = self.recommended.cost_inr
        return sum(o.cost_inr == cheapest for o in self.crew_options) - 1

    @property
    def cancellation_multiple(self) -> int:
        """How many times the recommendation's cost cancelling would cost."""
        if not self.recommended or not self.recommended.cost_inr:
            return 0
        return round(self.cancellation.cost_inr / self.recommended.cost_inr)


def _strategy(a: Assessment) -> Strategy:
    if a.positioning is not None:
        return Strategy.POSITIONING
    return Strategy.RESERVE_CALLOUT if a.on_reserve else Strategy.DAY_OFF_CALLOUT


def _stage(a: Assessment) -> str:
    failed = {v.rule_id for v in a.verdicts if v.failed}
    for stage, rule_ids, _ in FUNNEL:
        if failed & set(rule_ids):
            return stage
    return "within limits"


def _resilience(world: World, a: Assessment, role: Rank, base: str) -> float:
    """A reserve callout uses one of today's on-call crew of this rank at this base.

    Using the only reserve captain at a station is a bigger hit than using one of twelve,
    so the score is 100 * (n - 1) / n. A day-off callout leaves the reserve pool intact.
    """
    if not a.on_reserve:
        return 100.0
    day = a.days[0].date
    pool = sum(
        1
        for crew_id, duties in world.reserves.items()
        if any(day in r.dates and r.base == base for r in duties)
        and world.crew[crew_id].rank is role
    )
    return 100.0 if pool <= 0 else round(100.0 * (pool - 1) / pool, 1)


def find_options(world: World, pairing_id: str, role: Rank | str) -> OptionsResult:
    """Every legal way to fill ``role`` on ``pairing_id``, cheapest first."""
    role = Rank(role)
    pairing = world.pairing(pairing_id)
    incumbents = set(pairing.holder(role))
    pool = sorted(
        (c for c in world.crew.values() if c.rank is role and c.crew_id not in incumbents),
        key=lambda c: c.crew_id,
    )

    assessed = [assess(world, c.crew_id, pairing_id) for c in pool]
    legal = [a for a in assessed if a.legal]
    illegal = [a for a in assessed if not a.legal]

    costs = world.costs
    rows = []
    for a in legal:
        crew = world.crew[a.crew_id]
        breakdown = {"callout": costs.callout(role, a.on_reserve)}
        if a.positioning is not None:
            breakdown["positioning"] = costs.deadhead_positioning
            if a.departure_slip_hours:
                breakdown["delay"] = round(a.departure_slip_hours * costs.delay_per_duty_hour)
        rows.append((sum(breakdown.values()), a.departure_slip_hours, a, crew, breakdown))
    rows.sort(key=lambda r: (r[0], r[1], r[2].crew_id))

    options = []
    for i, (cost, slip, a, crew, breakdown) in enumerate(rows, start=1):
        options.append(
            Option(
                rank=i,
                strategy=_strategy(a),
                crew_id=a.crew_id,
                name=crew.name,
                crew_rank=crew.rank,
                base=crew.base,
                cost_inr=cost,
                cost_breakdown=breakdown,
                delay_hours=slip,
                positioning_flight=a.positioning.flight_id if a.positioning else None,
                reachability_minutes=world.reachability.get(a.crew_id),
                seniority=world.seniority.get(a.crew_id),
                risk_score=world.risk.get(a.crew_id),
                resilience=_resilience(world, a, role, crew.base),
            )
        )

    n_flights = len(pairing.flight_ids)
    cancel_cost = n_flights * costs.cancellation_per_flight
    cancel = Option(
        rank=len(options) + 1,
        strategy=Strategy.CANCEL,
        crew_id=None,
        name=f"Cancel all {n_flights} flights of the pairing",
        crew_rank=None,
        base=None,
        cost_inr=cancel_cost,
        cost_breakdown={"cancellation": cancel_cost},
        delay_hours=0.0,
    )
    options.append(cancel)

    best: dict[Strategy, Option] = {}
    for o in options[:-1]:
        best.setdefault(o.strategy, o)
    strategies = [*best.values(), cancel]

    crew_options = options[:-1]
    policies: dict[str, Option] = {}
    if crew_options:
        policies["cheapest"] = crew_options[0]
        policies["least_delay"] = min(
            crew_options, key=lambda o: (o.delay_hours, o.cost_inr, o.crew_id)
        )
        policies["keeps_most_reserve_cover"] = min(
            crew_options, key=lambda o: (-(o.resilience or 0), o.cost_inr, o.crew_id)
        )

    excluded = tuple(
        Exclusion(
            crew_id=a.crew_id,
            name=world.crew[a.crew_id].name,
            stage=_stage(a),
            rule_ids=tuple(dict.fromkeys(v.rule_id for v in a.blocking)),
            reasons=tuple(v.detail for v in a.blocking),
        )
        for a in illegal
    )

    dropped = Counter(e.stage for e in excluded)
    funnel = [FunnelStage("considered", len(pool), 0, "")]
    remaining = len(pool)
    for stage, _, reason in FUNNEL:
        if n := dropped.get(stage, 0):
            remaining -= n
            funnel.append(FunnelStage(stage, remaining, n, reason))
    funnel.append(FunnelStage("legal", remaining, 0, ""))

    return OptionsResult(
        pairing_id=pairing_id,
        role=role,
        options=tuple(options),
        strategies=tuple(strategies),
        excluded=excluded,
        funnel=tuple(funnel),
        policies=policies,
    )
