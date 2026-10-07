"""Remove personal data with no operational purpose, at the point an answer leaves.

It runs after the verifier, never before: the verifier checks the answer against the
unredacted tool results, and a redacted answer would make real numbers look unsourced.

Kept on purpose: crew name, rank, base and crew id. A controller has to know who to call.
Removed: email addresses, phone numbers, Indian government ids (Aadhaar, PAN, passport),
payment cards (Luhn-checked, so long ids are not mistaken for cards) and IP addresses in
text, and any field whose name marks it as an address, date of birth, health note, bank
detail, government id or emergency contact, whatever its content. Those have no reliable
text pattern: an address reads like prose and a birth date like any other date.
"""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(
    r"(?<![\d-])(?:\+\d[\d\s\-().]{6,14}\d|\d{10,15}"
    r"|0\d{2,4}[\s-]\d{3,4}[\s-]\d{4})(?![\d-])"
)
"""A leading + with digits, a bare run of 10-15 digits, or a landline with its 0 prefix.
Never a date or flight id: those are 4-2-2 groups."""
MOBILE_RE = re.compile(
    r"\b((?:call|phone|mobile|cell|contact|tel|ring|whatsapp|number)\b\D{0,12})"
    r"[6-9]\d{4}[\s-]\d{5}(?![\d-])",
    re.I,
)
"""A mobile written 5+5 ("98765 43210"), only beside a word saying it is a number: two
plain amounts ("70000 80000") look exactly the same."""
AADHAAR_RE = re.compile(r"(?<![\d-])(?<!\d\s)\d{4}[\s-]\d{4}[\s-]\d{4}(?![\s-]?\d)")
"""Exactly three groups of four: not three groups inside a longer number."""
PAN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])")
PASSPORT_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Z]\d{7}(?![A-Za-z0-9])")
IPV4_RE = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])"
)
CARD_RE = re.compile(r"(?<![\d-])(?:\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7}|\d{13,19})(?![\d-])")
"""Grouped in fours, or one unbroken run: never digits joined across separate dates."""

SENSITIVE_FIELDS = frozenset(
    {
        "address", "physical_address", "home_address", "residential_address",
        "date_of_birth", "dob", "birth_date",
        "medical_notes", "medical_condition", "health_notes", "diagnosis",
        "bank_account", "bank_account_number", "ifsc_code", "iban",
        "national_id", "ssn", "tax_id", "government_id", "aadhaar", "pan", "passport",
        "emergency_contact", "next_of_kin", "emergency_contact_number",
        "phone", "mobile", "email", "personal_email",
    }
)  # fmt: skip


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def _cards(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"[ \-]", "", m.group(0))
        return "[card removed]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    return CARD_RE.sub(repl, text)


def redact_text(text: str) -> str:
    """Most specific pattern first, so a PAN is not taken for something more generic."""
    text = EMAIL_RE.sub("[email removed]", text)
    text = _cards(text)
    text = PAN_RE.sub("[id removed]", text)
    text = PASSPORT_RE.sub("[id removed]", text)
    text = AADHAAR_RE.sub("[id removed]", text)
    text = IPV4_RE.sub("[address removed]", text)
    text = MOBILE_RE.sub(lambda m: m.group(1) + "[phone removed]", text)
    return PHONE_RE.sub("[phone removed]", text)


def redact(value: Any) -> Any:
    """A copy of a JSON-like value with personal data removed, at any depth."""
    if isinstance(value, dict):
        return {
            k: ("[removed]" if str(k).lower() in SENSITIVE_FIELDS and v is not None else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
