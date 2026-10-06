"""Roster uploads: parse a document, merge it over the current operation, and validate.

An upload is a JSON object holding any of the operation's collections, in the same shapes
as the seed files, for example ``{"pairings": [...], "flagged_exceptions": [...]}``. The
seed's ``rosters.json`` and ``rules.json`` can therefore be uploaded unchanged.

Uploaded records replace existing records with the same identity (a crew id, a flight id,
a pairing id, ...) and new identities are added. Nothing is ever deleted by an upload.

The merged operation is validated as a whole, because most mistakes only show against
other records: a pairing naming a crew member who does not exist, or a new duty that
double-books someone. Nothing is written unless the merged result has no errors.
Everything here is pure; ``roster_upload`` does the AWS input and output.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from crewops.domain import (
    Certification,
    CostTable,
    Crew,
    DataError,
    DutyClock,
    Flight,
    GateSlot,
    Pairing,
    ReserveDuty,
    RiskSignal,
    RosterException,
    Rule,
)
from crewops.ingestion.seed import SeedData, Severity, validate

MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Collection:
    record_type: type
    seed_field: str
    """The ``SeedData`` field this collection merges into."""
    identity: Callable[[Any], Any]
    """What makes two records "the same record" for replacement."""


COLLECTIONS: dict[str, Collection] = {
    "crew": Collection(Crew, "crew", lambda r: r.crew_id),
    "certifications": Collection(
        Certification, "certifications", lambda r: (r.crew_id, r.cert_type)
    ),
    "duty_clocks": Collection(DutyClock, "duty_clocks", lambda r: r.crew_id),
    "reserve_pool": Collection(ReserveDuty, "reserves", lambda r: (r.crew_id, r.base)),
    "risk_signals": Collection(RiskSignal, "risk_signals", lambda r: r.crew_id),
    "flagged_exceptions": Collection(
        RosterException, "exceptions", lambda r: (r.crew_id, r.date, r.rule_id)
    ),
    "flights": Collection(Flight, "flights", lambda r: r.flight_id),
    "pairings": Collection(Pairing, "pairings", lambda r: r.pairing_id),
    "boarding_gates": Collection(GateSlot, "gates", lambda r: r.flight_id),
    "rules": Collection(Rule, "rules", lambda r: r.rule_id),
}
COSTS_KEY = "costs"
DESCRIPTIVE_KEYS = frozenset({"note", "notes", "time_convention", "definitions"})
"""Free-text keys the seed files carry. Accepted and ignored."""


@dataclass(frozen=True, slots=True)
class Upload:
    records: Mapping[str, tuple[Any, ...]]
    """Collection name -> parsed records."""
    costs: CostTable | None = None

    def all_records(self) -> Iterator[Any]:
        for name in COLLECTIONS:
            yield from self.records.get(name, ())
        if self.costs is not None:
            yield self.costs

    @property
    def counts(self) -> dict[str, int]:
        out = {name: len(rows) for name, rows in self.records.items() if rows}
        if self.costs is not None:
            out[COSTS_KEY] = 1
        return out


def parse_upload(doc: Any) -> Upload:
    """Parse and check one upload document. Raises ``DataError`` naming the bad field."""
    if not isinstance(doc, Mapping):
        raise DataError("upload: expected a JSON object at the top level")
    unknown = set(doc) - set(COLLECTIONS) - {COSTS_KEY} - DESCRIPTIVE_KEYS
    if unknown:
        allowed = ", ".join(sorted([*COLLECTIONS, COSTS_KEY]))
        raise DataError(f"upload: unknown key(s) {sorted(unknown)}; allowed: {allowed}")

    records: dict[str, tuple[Any, ...]] = {}
    for name, collection in COLLECTIONS.items():
        if name not in doc:
            continue
        rows = doc[name]
        if not isinstance(rows, list):
            raise DataError(f"{name}: expected a list")
        parsed = tuple(
            collection.record_type.from_dict(row, f"{name}[{i}]") for i, row in enumerate(rows)
        )
        dupes = [k for k, n in Counter(collection.identity(r) for r in parsed).items() if n > 1]
        if dupes:
            raise DataError(f"{name}: the same record appears more than once: {dupes[:5]}")
        records[name] = parsed

    costs = CostTable.from_dict(doc[COSTS_KEY]) if COSTS_KEY in doc else None
    upload = Upload(records=records, costs=costs)
    if not upload.counts:
        raise DataError("upload: no records found")
    return upload


def merge(base: SeedData, upload: Upload) -> SeedData:
    """``base`` with the uploaded records replacing or adding by identity."""
    changes: dict[str, Any] = {}
    for name, rows in upload.records.items():
        collection = COLLECTIONS[name]
        current = getattr(base, collection.seed_field)
        incoming = {collection.identity(r): r for r in rows}
        kept = [r for r in current if collection.identity(r) not in incoming]
        changes[collection.seed_field] = (*kept, *rows)
    if upload.costs is not None:
        changes["costs"] = upload.costs
    return replace(base, **changes)


class Status(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IngestResult:
    upload_key: str
    status: Status
    counts: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "upload": self.upload_key,
            "status": str(self.status),
            "records": self.counts,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def ingest(raw: bytes, base: SeedData, upload_key: str) -> tuple[IngestResult, list[Any]]:
    """Decide what an upload would change. Returns the result and the records to write.

    Records to write is empty unless the result is accepted. Warnings already present in
    ``base`` are not repeated, so a report only shows what this upload introduced.
    """

    def rejected(*errors: str) -> tuple[IngestResult, list[Any]]:
        return IngestResult(upload_key, Status.REJECTED, errors=errors), []

    if len(raw) > MAX_UPLOAD_BYTES:
        return rejected(f"upload is {len(raw)} bytes; the limit is {MAX_UPLOAD_BYTES}")
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return rejected(f"not valid JSON: {exc}")
    try:
        upload = parse_upload(doc)
    except DataError as exc:
        return rejected(str(exc))

    known = {str(i) for i in validate(base)}
    issues = [i for i in validate(merge(base, upload)) if str(i) not in known]
    errors = tuple(str(i) for i in issues if i.severity is Severity.ERROR)
    warnings = tuple(str(i) for i in issues if i.severity is Severity.WARNING)
    if errors:
        return IngestResult(
            upload_key, Status.REJECTED, upload.counts, errors=errors, warnings=warnings
        ), []
    accepted = IngestResult(upload_key, Status.ACCEPTED, upload.counts, warnings=warnings)
    return accepted, list(upload.all_records())
