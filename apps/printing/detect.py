"""Which printer suits which paper, from the sizes its driver reports and its name.

Drivers are not always honest, so this only fills a blank; Settings → Printers can override it.
"""

import re

NOT_PAPER = re.compile(r"pdf|xps|onenote|fax|send to|snagit", re.I)
LABEL_NAME = re.compile(r"label|sticker|barcode|zebra|\bzd\d|\btsc\b|godex|xp-?(2|3|4)\d\d", re.I)
THERMAL_NAME = re.compile(
    r"thermal|receipt|\bpos\b|pos-?\d|\btm-|xp-?\d\d\b|80 ?mm|rongta|bixolon", re.I
)
A4_MM = (210, 297)


def _has_a4(sizes: list) -> bool:
    return any(abs(min(s) - A4_MM[0]) <= 2 and abs(max(s) - A4_MM[1]) <= 2 for s in sizes)


def kind_of(name: str, sizes: list) -> str:
    """office, thermal, label, or "" when this cannot print our papers or we cannot tell."""
    if NOT_PAPER.search(name):
        return ""
    if LABEL_NAME.search(name):
        return "label"
    if THERMAL_NAME.search(name):
        return "thermal"
    if not sizes:
        return ""
    if _has_a4(sizes):  # an office printer also lists small cards and envelopes
        return "office"
    narrowest = min(min(s) for s in sizes)
    if 70 <= narrowest <= 90:
        return "thermal"
    return "label" if narrowest < 70 else ""


def candidates(printers: list, sizes: dict) -> dict[str, list[str]]:
    kinds = {name: kind_of(name, sizes.get(name, [])) for name in printers}
    office = [name for name, kind in kinds.items() if kind == "office"]
    return {
        "thermal": [name for name, kind in kinds.items() if kind == "thermal"],
        "label": [name for name, kind in kinds.items() if kind == "label"],
        "a4": office,
        "a5": [],  # follows the A4 printer unless an admin picks one
    }
