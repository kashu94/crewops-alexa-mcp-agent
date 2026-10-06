"""Duty-time arithmetic.

Two conventions quietly produce plausible but wrong legality answers if missed:

1. Duty and flight-hour limits use **calendar-day windows** (UTC dates, inclusive of the
   duty date), not rolling 168- or 672-hour windows from an instant.
2. The flight duty period limit **shrinks with sector count**: 13 h minus half an hour for
   every sector beyond the second, so a four-sector day caps at 12 h.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from crewops.domain import AircraftType


def hours_between(start: datetime, end: datetime) -> float:
    """Elapsed hours, rounded to two decimals."""
    return round((end - start).total_seconds() / 3600.0, 2)


def calendar_window(end: date, days: int) -> tuple[date, date]:
    """The inclusive window of ``days`` calendar days ending on ``end``.

    Seven days ending the 14th run from the 8th to the 14th, not from the 7th. This
    off-by-one is the most likely source of a wrong verdict, so it lives in one place.
    """
    return end - timedelta(days=days - 1), end


def format_hours(value: float) -> str:
    """``1.33`` -> ``'1h20m'``. How a breach is said aloud."""
    total_minutes = round(abs(value) * 60)
    return f"{total_minutes // 60}h{total_minutes % 60:02d}m"


@dataclass(frozen=True, slots=True)
class DutyDay:
    """One duty day: when it starts and ends, how many legs, and where it begins."""

    date: date
    report_utc: datetime
    release_utc: datetime
    n_sectors: int
    block_hours: float
    aircraft_type: AircraftType
    dep_station: str

    @property
    def fdp_hours(self) -> float:
        return hours_between(self.report_utc, self.release_utc)

    def delayed(self, hours: float) -> DutyDay:
        """The same day with a late departure: the crew still reports on time, so the
        day gets longer. Only release moves, which is how a delay breaches FDP."""
        return replace(self, release_utc=self.release_utc + timedelta(hours=hours))

    def shifted(self, hours: float) -> DutyDay:
        """The whole day moved later, for a crew member positioning in from another base.
        Report and release both move, so the day's length is unchanged."""
        shift = timedelta(hours=hours)
        return replace(
            self, report_utc=self.report_utc + shift, release_utc=self.release_utc + shift
        )
