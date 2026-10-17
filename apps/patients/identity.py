"""Normalization of Pakistani phone numbers and CNICs, so search and duplicate checks match
however reception typed them ("0300-1234567", "+92 300 1234567", "923001234567")."""

import re

_NON_DIGITS = re.compile(r"\D")


def digits(value: str) -> str:
    return _NON_DIGITS.sub("", value or "")


def normalize_phone(value: str) -> str:
    """Canonical form: local 11-digit "03001234567" for Pakistani numbers, else "+<digits>"."""
    raw = (value or "").strip()
    d = digits(raw)
    if not d:
        return ""
    if d.startswith("0092"):
        d = d[2:]
    if d.startswith("92") and len(d) == 12:
        return "0" + d[2:]
    if d.startswith("3") and len(d) == 10:
        return "0" + d
    if d.startswith("0") and len(d) in (10, 11):
        return d
    return ("+" + d) if raw.startswith("+") or len(d) > 11 else d


def format_phone(value: str) -> str:
    if len(value) == 11 and value.startswith("03"):
        return f"{value[:4]} {value[4:]}"
    return value


def is_valid_phone(value: str) -> bool:
    return 7 <= len(digits(value)) <= 15


def normalize_cnic(value: str) -> str:
    return digits(value)


def is_valid_cnic(value: str) -> bool:
    return len(digits(value)) == 13


def format_cnic(value: str) -> str:
    d = digits(value)
    return f"{d[:5]}-{d[5:12]}-{d[12]}" if len(d) == 13 else value
