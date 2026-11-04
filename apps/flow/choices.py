"""Counters and visit states, kept free of model imports so masterdata can use them too."""

from django.db import models


class CounterLocation(models.TextChoices):
    """Where money is taken. A service's billed_at names the counter that collects its fee."""

    RECEPTION = "RECEPTION", "Reception"
    PHARMACY = "PHARMACY", "Pharmacy"
    LAB = "LAB", "Laboratory"
    CENTRAL_CASH = "CENTRAL_CASH", "Central cash"


class RxPrintAt(models.TextChoices):
    """Where a prescription written in SehatDesk prints by itself."""

    NONE = "NONE", "Not by itself (press Print)"
    DOCTOR = "DOCTOR", "Doctor's desk, when signed"
    PHARMACY = "PHARMACY", "Pharmacy, when opened there"
    BOTH = "BOTH", "Both: doctor's desk and pharmacy"


class VisitState(models.TextChoices):
    TOKEN_ISSUED = "TOKEN_ISSUED", "Token issued · pay first"
    IN_QUEUE = "IN_QUEUE", "In the doctor's queue"
    WITH_DOCTOR = "WITH_DOCTOR", "With the doctor"
    PHARMACY_QUEUE = "PHARMACY_QUEUE", "Pharmacy queue"
    LAB_QUEUE = "LAB_QUEUE", "Lab queue"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"
