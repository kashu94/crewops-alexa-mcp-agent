"""Deterministic legality engine: duty arithmetic, the seven rules, and per-crew assessment.

No model call happens here. Every number an agent repeats comes from this package.
"""

from crewops.engine.legality import Assessment, assess
from crewops.engine.rules import ALL_RULES, RuleBook, RuleVerdict, Verdict
from crewops.engine.world import Positioning, UnknownEntity, World, build_world

__all__ = [
    "ALL_RULES",
    "Assessment",
    "Positioning",
    "RuleBook",
    "RuleVerdict",
    "UnknownEntity",
    "Verdict",
    "World",
    "assess",
    "build_world",
]
