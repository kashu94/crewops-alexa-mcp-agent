"""Controller desks: who watches which aircraft.

Three desks share the operation, two aircraft each. A disruption belongs to every desk
whose aircraft it touches, which is how two controllers can end up competing for the same
reserve crew member, and why cover is always checked across desks.

Controllers dispatch; none of them fly. Their names are not on the crew roster.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Desk:
    desk_id: str
    controller: str
    aircraft: tuple[str, ...]


DESKS: tuple[Desk, ...] = (
    Desk("desk-1", "Ananya Iyer", ("VT-DXA", "VT-DXB")),
    Desk("desk-2", "Rohit Malhotra", ("VT-DXC", "VT-DXD")),
    Desk("desk-3", "Divya Rao", ("VT-DXE", "VT-DXF")),
)


def desks_for(aircraft: set[str] | frozenset[str]) -> list[Desk]:
    """The desks covering any of ``aircraft``, in desk order."""
    return [d for d in DESKS if aircraft & set(d.aircraft)]
