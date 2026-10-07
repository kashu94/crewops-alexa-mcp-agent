"""The trust gate: every id, number, time, name, rule verdict and legality verdict in an
answer must come from a tool result of the same turn.

The model has no other source of facts, so anything it states that no tool returned is
invented. The check is set membership over the trace: deterministic, so it cannot itself
make something up. A draft that fails is discarded and the deterministic rendering of the
tool results is spoken instead.

Evidence is what the tools *returned*. The ids a call was made with also count when the call
succeeded (the question named them), and the ids in an error message count (so "C-1024
doesn't exist" can be said), but no number ever comes from an argument or an error: a model
cannot make a figure true by passing it to a tool.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from crewops.agent.entities import (
    AIRCRAFT_RE,
    CREW_RE,
    FLIGHT_ID_RE,
    FLIGHT_NO_RE,
    PAIRING_RE,
    RULE_RE,
)
from crewops.tools import TraceEntry

NUMERIC_FLOOR = 10.0
"""Whole numbers below this are prose ("all 7 rules", "2 options"), not claims to source."""
TOLERANCE = 0.01

DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
ISO_DT_RE = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
CLOCK_RE = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?Z?(?![\d:])")
ID_PATTERNS = (
    FLIGHT_ID_RE,
    CREW_RE,
    PAIRING_RE,
    FLIGHT_NO_RE,
    RULE_RE,
    AIRCRAFT_RE,
    DATE_RE,
    CLOCK_RE,
)
DURATION_RE = re.compile(r"(?<![\w.])(\d+)h(\d{2})m\b")
""""1h20m": one claim, in hours."""
NUMBER_RE = re.compile(
    r"(?<![\w.\-])(-(?=[\d₹]))?(?:₹\s*)?(\d[\d,]*(?:\.\d+)?)"
    r"(\s?(?:lakhs?|crores?|thousand|million)\b|[a-zA-Z]{1,4}\b)?",
    re.I,
)
"""A number, with its sign and any unit stuck to it ("47h", "120min", "45k", "2359Z")."""
NAME_RE = re.compile(r"\b[A-Z]\. ?[A-Z][a-zA-Z'-]+")
"""Every crew name is "Initial. Surname"."""
FULL_NAME_ID_RE = re.compile(r"\b([A-Z][a-zA-Z'-]{2,}\s+[A-Z][a-zA-Z'-]+)\s*\((C-\d{4})\)")
"""A full first name beside an id was invented: no crew name has one."""
RANK_WORDS = {"captain", "first", "officer", "senior", "cabin", "crew", "fo", "reserve"}
FLIGHT_MENTION_RE = re.compile(r"\bflight\s+([A-Za-z]{1,4}\d{1,4})\b", re.I)
PROSE_DATE_RE = re.compile(
    r"\b\d{1,2}\s*(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r"\s*(?:st|nd|rd|th)?\b",
    re.I,
)
WINDOW_RE = re.compile(
    r"\b(?:next|last|past|coming|previous|preceding)\s+\d+\s*(?:day|week|month|hour|hr)s?\b",
    re.I,
)
""""the next 30 days" echoes the question's window; it is not a measured value."""
FAIL_WORDS_RE = re.compile(
    r"\b(?:breach\w*|violat\w*|exceed\w*|illegal|fails?|failed|non-?compliant)\b", re.I
)
PASS_WORDS_RE = re.compile(
    r"\b(?:clears?|cleared|pass(?:es|ed)?|legal|compliant|within\s+limits?|meets?|fine|ok|okay|"
    r"satisfied|satisfies)\b",
    re.I,
)
NEGATION_RE = re.compile(r"\b(?:not|no|never|without|cannot)\b|n't\b", re.I)
CLAUSE_RE = re.compile(r"[^.;!?\n]+")
LEGAL_CLAIM_RE = re.compile(
    r"\b(C-\d{4})\b([^.;!?\n]{0,80}?)\b(legal|illegal|legally|cleared|cover|take|fly|operate)\b"
    r"(?!\s+(?:option|choice))",
    re.I,
)
"""A crew id, then within the clause a verdict: "is legal", "is not legal", "is cleared
to", "can cover", "cannot take"."""
MODAL_RE = re.compile(r"\b(cannot|can[’']t|unable to|able to|could|can|may)\b", re.I)
"""Longest first, so "can't" is not read as "can"."""
RANK_SURNAME_RE = re.compile(
    r"\b(?:Captain|Capt\.?|First Officer|FO|Purser|Senior Cabin Crew|Cabin Crew)"
    r"\s+([A-Z][a-z]{2,})\b"
)
ORDINAL_DAY_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b", re.I)
RANK_SURNAME_ID_RE = re.compile(
    r"\b(?:Captain|Capt\.?|First Officer|FO|Purser|Senior Cabin Crew|Cabin Crew)\s+"
    r"([A-Z][a-z]{2,})\s*\(\s*(C-\d{4})\s*\)"
)
""""Captain Reddy (C-1526)": the surname must be that id's."""
RANK_SURNAME_SKIP = frozenset({"Officer", "Crew", "Cabin", "Seat", "Seats", "Callout"})
FIRST_SURNAME_RE = re.compile(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b")
NOT_FIRST_NAMES = frozenset(
    [
        "Captain",
        "First",
        "Senior",
        "Cabin",
        "Crew",
        "Control",
        "Reserve",
        "Assign",
        "Cancel",
        "Cancelling",
        "Callout",
        "Day",
        "Desks",
        "The",
        "For",
        "With",
        "Delayed",
        "Boardings",
        "Rostered",
        "Position",
        "Nothing",
        "Costs",
        "Please",
    ]
)

UNCOVERED_RE = re.compile(r"\b(uncrewed|uncovered)\b", re.I)
COUNT_RE = re.compile(
    r"(?<![\d.,])\b(\d[\d,]*)\s+(?:captains?|first officers?|pilots?|crew(?: members?)?|"
    r"cabin crew|senior cabin crew|pairings?|flights?(?! hours?)(?!\s*\()|records?|options?|gates?|"
    r"certifications?|reserves?)\b",
    re.I,
)


@dataclass(slots=True)
class Claim:
    kind: str
    """identifier, number, rule_context, legality, crew_name, flight_no, uncovered,
    headcount or completeness."""
    value: str
    supported: bool = False
    source: str | None = None
    derivation: str | None = None


@dataclass(slots=True)
class Verification:
    ok: bool
    claims: list[Claim] = field(default_factory=list)
    unsupported: list[Claim] = field(default_factory=list)

    def summary(self) -> str:
        if self.unsupported:
            return "unsourced: " + ", ".join(c.value for c in self.unsupported)
        return f"verified: {len(self.claims)} claims traced to tool results"


def normalise_clock(value: str) -> str | None:
    """``6:00``, ``06:00Z``, ``06:00:00+00:00`` -> ``06:00``. Only UTC and zero seconds."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(Z|[+-]00:?00)?", value.strip())
    if not m or m.group(3) not in (None, "00") or int(m.group(1)) > 23:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _stamps(text: str) -> tuple[list[str], str]:
    """The (date, time) inside every full timestamp, and the text with the times removed
    (the dates stay, to be matched as dates)."""
    found = []
    for stamp in ISO_DT_RE.findall(text):
        date, clock = re.split(r"[T ]", stamp, maxsplit=1)
        found.append(date)
        found.append(normalise_clock(clock) or clock)
    rest = ISO_DT_RE.sub(lambda m: " " + re.split(r"[T ]", m.group(), maxsplit=1)[0] + " ", text)
    return found, rest


def _ids(text: str) -> list[str]:
    stamped, rest = _stamps(text)
    out = list(stamped)
    for pattern in ID_PATTERNS:
        out += pattern.findall(rest)
    return out


def _strip(text: str) -> str:
    """No numbers from inside ids ("C-1042" is not 1042), dates or calendar words."""
    text = ISO_DT_RE.sub(" ", text)
    for pattern in ID_PATTERNS:
        text = pattern.sub(" ", text)
    text = ORDINAL_DAY_RE.sub(" ", text)  # "the 15th" is a day, not a quantity
    return WINDOW_RE.sub(" ", PROSE_DATE_RE.sub(" ", text))


def numbers_in(text: str) -> list[tuple[str, float]]:
    """(as written, value) for every number in the text, ids and dates excluded."""
    text = _strip(text)
    out = []
    for m in DURATION_RE.finditer(text):
        out.append((m.group(), round(int(m.group(1)) + int(m.group(2)) / 60, 2)))
    text = DURATION_RE.sub(" ", text)
    for m in NUMBER_RE.finditer(text):
        try:
            value = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(3) or "").strip().lower().rstrip("s")
        value *= {
            "k": 1e3,
            "thousand": 1e3,
            "lakh": 1e5,
            "l": 1e5,
            "crore": 1e7,
            "million": 1e6,
        }.get(unit, 1)
        if m.group(1):
            value = -value
        out.append((m.group().strip(), value))
    return out


def _dicts(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _dicts(item)


def _scalars(node: Any) -> Iterable[Any]:
    """Every value in a result, and the length of every list ("12 records" is sourced)."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _scalars(value)
    elif isinstance(node, list | tuple):
        yield len(node)
        for item in node:
            yield from _scalars(item)
    else:
        yield node


@dataclass(slots=True)
class Evidence:
    identifiers: dict[str, str] = field(default_factory=dict)
    numbers: list[tuple[float, int, str]] = field(default_factory=list)
    """(value, which result it came from, tool)."""
    rule_statuses: dict[str, set[str]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    legal: dict[str, set[bool]] = field(default_factory=dict)
    """crew id -> every legality verdict a result gave for them."""
    counts: set[float] = field(default_factory=set)
    lists: list[list[str]] = field(default_factory=list)
    costs: list[tuple[float, int, str]] = field(default_factory=list)
    """Rupee amounts, by result: the only numbers arithmetic may combine."""
    surnames: set[str] = field(default_factory=set)
    text: str = ""
    """Every string the results contained, for names said exactly as a tool gave them."""

    def has_id(self, value: str) -> str | None:
        return self.identifiers.get(value) or self.identifiers.get(normalise_clock(value) or "")

    def has_number(self, value: float) -> str | None:
        return next((t for n, _, t in self.numbers if abs(n - value) <= TOLERANCE), None)

    def derive(self, value: float) -> tuple[str, str] | None:
        """A sum or difference of two rupee amounts from the *same* result ("₹5,500 more"
        than the recommendation; a plan's two costs together). Nothing wider: over all the
        numbers in a result, pairwise arithmetic can reach almost any small number."""
        if abs(value) < NUMERIC_FLOOR:
            return None
        by_result: dict[int, list[tuple[float, str]]] = {}
        for n, i, t in self.costs:
            by_result.setdefault(i, []).append((n, t))
        for values in by_result.values():
            for a, tool in values:
                for b, _ in values:
                    if a == b:
                        continue
                    for expr, result in ((f"{a:g} - {b:g}", a - b), (f"{a:g} + {b:g}", a + b)):
                        if abs(result - value) <= TOLERANCE:
                            return expr, tool
        return None


COUNT_FIELDS = frozenset(
    {
        "count",
        "legal_options",
        "excluded_count",
        "directly_affected_flights",
        "at_risk_flights",
        "open_issues",
        "total_boarding_gates",
        "occupied_boarding_gates",
        "of_boarding_gates",
        "equal_cost_alternatives",
    }
)
ERROR_NAME_ID_RE = re.compile(
    r"(C-\d{4})\s*\(([A-Z]\. [A-Z][a-zA-Z'-]+)|([A-Z]\. [A-Z][a-zA-Z'-]+)\s*\((C-\d{4})"
)
""""C-1938 (N. Reddy, ..." and "H. Pillai (C-1317, ...": names a did-you-mean names."""


def build_evidence(trace: Sequence[TraceEntry]) -> Evidence:
    ev = Evidence()
    for index, entry in enumerate(trace):
        sources: list[Any] = []
        if entry.ok:
            sources = [entry.result, entry.args]
        elif entry.error:
            sources = [entry.error]
        for payload in sources:
            for scalar in _scalars(payload):
                if isinstance(scalar, str):
                    for found in _ids(scalar):
                        ev.identifiers.setdefault(found, entry.tool)
        if not entry.ok:
            for a, b, c, d in ERROR_NAME_ID_RE.findall(entry.error or ""):
                crew_id, name = (a, b) if a else (d, c)
                ev.names.setdefault(crew_id, name)
            continue
        for scalar in _scalars(entry.result):
            if isinstance(scalar, bool) or scalar is None:
                continue
            if isinstance(scalar, int | float):
                ev.numbers.append((float(scalar), index, entry.tool))
            elif isinstance(scalar, str):
                ev.numbers += [(v, index, entry.tool) for _, v in numbers_in(scalar)]
        for d in _dicts(entry.result):
            if "rule_id" in d and "status" in d:
                ev.rule_statuses.setdefault(str(d["rule_id"]), set()).add(str(d["status"]).upper())
            if isinstance(d.get("crew_id"), str) and isinstance(d.get("name"), str):
                ev.names.setdefault(d["crew_id"], d["name"])
            for key, value in d.items():
                if isinstance(value, list):
                    ev.counts.add(float(len(value)))  # a list's length is a count it gives
                if isinstance(value, bool) or not isinstance(value, int | float):
                    continue
                if key.endswith("cost_inr"):
                    ev.costs.append((float(value), index, entry.tool))
                if key in COUNT_FIELDS:
                    ev.counts.add(float(value))
        result = entry.result if isinstance(entry.result, dict) else {}
        if entry.tool == "check_legality" and "legal" in result:
            ev.legal.setdefault(result["crew_id"], set()).add(bool(result["legal"]))
        if entry.tool == "find_options":
            if result.get("crew_out"):  # out of the seat: they will not fly it
                ev.legal.setdefault(result["crew_out"], set()).add(False)
            for o in result.get("options", []) + [result.get("recommended") or {}]:
                if o.get("crew_id"):
                    ev.legal.setdefault(o["crew_id"], set()).add(True)
            for x in result.get("excluded", []):
                ev.legal.setdefault(x["crew_id"], set()).add(False)
        if entry.tool == "simulate":
            for changed in result.get("changed", []):
                ev.legal.setdefault(changed["crew_id"], set()).update({True, False})
        strings = [x for x in _scalars(entry.result) if isinstance(x, str)]
        ev.text += "\n" + "\n".join(strings)
        if entry.tool == "ripple":
            ids = [
                f
                for p in result.get("affected_pairings", [])
                for f in p.get("directly_affected_flights", [])
            ]
            if len(ids) >= 2:
                ev.lists.append(ids)
    for name in ev.names.values():
        ev.surnames.add(name.split()[-1])
    return ev


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _clauses(text: str) -> Iterable[tuple[int, str]]:
    for m in CLAUSE_RE.finditer(text):
        yield m.start(), m.group()


def _negated(text: str, at: int) -> bool:
    """A negation just before position ``at``: "not breached", "isn't legal", "no breach"."""
    return bool(NEGATION_RE.search(text[max(0, at - 16) : at]))


def _rule_context(text: str, ev: Evidence) -> list[Claim]:
    """ "breaches RULE-DUTY-02" must match that rule's real status.

    Read clause by clause, and each rule takes the verdict word nearest to it, so one rule's
    verdict never attaches to its neighbour. A negation counts only right before the verdict
    word: "RULE-DUTY-02 is not breached" claims a pass.
    """
    out, seen = [], set()
    for _, clause in _clauses(text):
        verdicts = [(m.start(), True) for m in FAIL_WORDS_RE.finditer(clause)]
        verdicts += [(m.start(), False) for m in PASS_WORDS_RE.finditer(clause)]
        if not verdicts:
            continue
        for rule in RULE_RE.finditer(clause):
            after = [v for v in verdicts if v[0] >= rule.end()]
            before = [v for v in verdicts if v[0] < rule.start()]
            if after:  # "RULE-X passes but RULE-Y fails": each takes the word after it
                at, is_fail = min(after, key=lambda v: v[0] - rule.end())
            else:
                at, is_fail = max(before, key=lambda v: v[0])
            wanted = "FAIL" if is_fail != _negated(clause, at) else "PASS"
            key = f"{rule.group()} ({wanted.lower()})"
            if wanted not in ev.rule_statuses.get(rule.group(), set()) and key not in seen:
                seen.add(key)
                out.append(Claim("rule_context", key))
    return out


def _legality(text: str, ev: Evidence) -> list[Claim]:
    """ "C-2087 is legal for P-2291", "C-2087 can cover P-2291", "C-2087 cannot take it": each
    must match a legality verdict a tool gave for C-2087. Negation is read only between the
    id and the verdict, so "No, C-2087 is legal" still claims legal."""
    out = []
    for _, clause in _clauses(text):
        for m in LEGAL_CLAIM_RE.finditer(clause):
            crew_id, mid, word = m.group(1), m.group(2), m.group(3).lower()
            if word in ("cover", "take", "fly", "operate"):
                modal = MODAL_RE.search(mid)
                if not modal:
                    continue  # "C-3310 to cover": an assignment, checked as an id
                claimed = not re.search(r"cannot|can[’']t|unable", modal.group(), re.I)
                claimed = claimed != bool(re.search(r"\bnot\b", mid[modal.end() :], re.I))
            else:
                claimed = (word != "illegal") != bool(NEGATION_RE.search(mid))
            if claimed not in ev.legal.get(crew_id, set()):
                out.append(Claim("legality", f"{crew_id} {'legal' if claimed else 'not legal'}"))
    return out


def _names(text: str, ev: Evidence) -> list[Claim]:
    """Every crew name must be one a tool gave, and beside an id it must be that id's name."""
    out: list[Claim] = []
    known = set(ev.names.values())
    seen: set[str] = set()

    def flag(value: str) -> None:
        if value not in seen:
            seen.add(value)
            out.append(Claim("crew_name", value))

    for raw in NAME_RE.findall(text):
        name = re.sub(r"\.\s?", ". ", raw)
        if name not in known:
            flag(name)
    pairs = re.findall(r"([A-Z]\. ?[A-Z][a-zA-Z'-]+)\s*[(,]\s*(C-\d{4})\)?", text)
    pairs += [
        (n, c) for c, n in re.findall(r"(C-\d{4})\s*\(\s*([A-Z]\. ?[A-Z][a-zA-Z'-]+)\s*\)", text)
    ]
    for raw, crew_id in pairs:
        name = re.sub(r"\.\s?", ". ", raw)
        if crew_id in ev.identifiers and ev.names.get(crew_id) != name:
            flag(f"{name} ({crew_id})")
    for full, crew_id in FULL_NAME_ID_RE.findall(text):
        if full.split()[0].lower() not in RANK_WORDS:
            flag(f"{full} ({crew_id})")
    # A person named another way: "Captain Sharma", or "Ravi Iyer" for R. Iyer. Only a
    # surname a tool gave counts, and only in the form the tool gave it.
    for surname, crew_id in RANK_SURNAME_ID_RE.findall(text):
        given = ev.names.get(crew_id)
        if given is None or given.split()[-1] != surname:
            flag(f"{surname} ({crew_id})")
    for surname in RANK_SURNAME_RE.findall(text):
        if surname not in ev.surnames and surname not in RANK_SURNAME_SKIP:
            flag(surname)
    for first, surname in FIRST_SURNAME_RE.findall(text):
        if (
            surname in ev.surnames
            and first not in NOT_FIRST_NAMES
            and f"{first} {surname}" not in ev.text
        ):
            flag(f"{first} {surname}")
    return out


def _malformed_flights(text: str) -> list[Claim]:
    return [
        Claim("flight_no", t)
        for t in dict.fromkeys(x.upper() for x in FLIGHT_MENTION_RE.findall(text))
        if not FLIGHT_NO_RE.fullmatch(t)
    ]


def _uncovered(text: str, trace: Sequence[TraceEntry]) -> list[Claim]:
    if not UNCOVERED_RE.search(text):
        return []
    if any(e.ok and e.tool in ("ripple", "find_options") for e in trace):
        return []
    return [Claim("uncovered", UNCOVERED_RE.search(text).group())]  # type: ignore[union-attr]


def _headcount(text: str, ev: Evidence) -> list[Claim]:
    """A stated headcount must equal a count a lookup computed: "26 captains" could
    otherwise pass because 26 is someone's seniority elsewhere in the rows."""
    if not ev.counts:
        return []
    out = []
    for m in COUNT_RE.finditer(_strip(text)):
        n = float(m.group(1).replace(",", ""))
        if n >= NUMERIC_FLOOR and n not in ev.counts:
            out.append(Claim("headcount", m.group(0).strip()))
    return out


def _completeness(text: str, ev: Evidence) -> list[Claim]:
    out = []
    for ids in ev.lists:
        named = sum(1 for i in ids if i in text or re.search(rf"\b{i.split('-')[0]}\b", text))
        if 0 < named < len(ids):
            out.append(Claim("completeness", f"named {named} of {len(ids)} affected flights"))
    return out


def verify(text: str, trace: Sequence[TraceEntry]) -> Verification:
    """Check an answer against the tool results that produced it."""
    trace = list(trace)
    ev = build_evidence(trace)
    claims: list[Claim] = []
    for value in dict.fromkeys(_ids(text)):
        source = ev.has_id(value)
        claims.append(Claim("identifier", value, source is not None, source))
    for raw, value in dict.fromkeys(numbers_in(text)):
        small = abs(value) < NUMERIC_FLOOR and float(value).is_integer() and value >= 0
        if small and "₹" not in raw:
            continue
        claim = Claim("number", raw)
        if source := ev.has_number(value):
            claim.supported, claim.source = True, source
        elif derived := ev.derive(value):
            claim.supported = True
            claim.derivation, claim.source = derived
        claims.append(claim)
    claims += _rule_context(text, ev)
    claims += _legality(text, ev)
    claims += _names(text, ev)
    claims += _malformed_flights(text)
    claims += _uncovered(text, trace)
    claims += _headcount(text, ev)
    claims += _completeness(text, ev)
    unsupported = [c for c in claims if not c.supported]
    return Verification(ok=bool(trace) and not unsupported, claims=claims, unsupported=unsupported)


RETRY_INSTRUCTION = """\
Your draft stated things no tool result supports: {bad}.
Every id, number, time and name must come from a tool result. Call the tool that
establishes them, or answer without them. Do not restate them from memory."""


def retry_prompt(result: Verification) -> str:
    return RETRY_INSTRUCTION.format(bad=", ".join(c.value for c in result.unsupported))
