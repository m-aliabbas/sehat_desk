"""Business segments used by ledgers, the P&L and commission (Phase 36).

Every invoice line maps to one segment by its kind, so income needs no extra tagging.
Expenses carry their segment explicitly.
"""

HOSPITAL, DOCTORS, LAB, PHARMACY = "hospital", "doctors", "lab", "pharmacy"

SEGMENTS = [
    (DOCTORS, "Doctors / OPD"),
    (LAB, "Laboratory"),
    (PHARMACY, "Pharmacy"),
    (HOSPITAL, "Hospital (system)"),
]
LABELS = dict(SEGMENTS)
KEYS = [k for k, _ in SEGMENTS]

# InvoiceItem.Kind -> segment
BY_ITEM_KIND = {
    "consultation": DOCTORS,
    "procedure": DOCTORS,
    "lab": LAB,
    "pharmacy": PHARMACY,
    "service": HOSPITAL,
    "room": HOSPITAL,
    "misc": HOSPITAL,
}


def for_item_kind(kind: str) -> str:
    return BY_ITEM_KIND.get(kind, HOSPITAL)


def empty(value=None):
    """{segment: value} for every segment, in display order."""
    from decimal import Decimal

    return {k: (Decimal("0.00") if value is None else value) for k in KEYS}
