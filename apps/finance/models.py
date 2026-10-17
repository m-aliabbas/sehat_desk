"""Expenses (Phase 36). Income, refunds, deposits, staff pay and supplier payments already live
in their own apps; the ledger and P&L (apps/finance/statements.py) read them from there.

Expenses are never edited or deleted. A mistake is voided with a reason and stays on record,
excluded from totals.
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.billing.models import CENT
from apps.core.scoping import BranchScopedModel, OrgScopedModel

from . import segments


class ExpenseCategory(OrgScopedModel):
    DEFAULTS = [
        ("Rent", segments.HOSPITAL),
        ("Electricity & utilities", segments.HOSPITAL),
        ("Medical supplies & consumables", segments.HOSPITAL),
        ("Lab reagents & kits", segments.LAB),
        ("Equipment maintenance", segments.HOSPITAL),
        ("Cleaning & housekeeping", segments.HOSPITAL),
        ("Marketing", segments.HOSPITAL),
        ("Pharmacy running costs", segments.PHARMACY),
        ("Visiting doctor fee", segments.DOCTORS),
        ("Miscellaneous", segments.HOSPITAL),
    ]

    name = models.CharField(max_length=80)
    segment = models.CharField(
        max_length=12,
        choices=segments.SEGMENTS,
        default=segments.HOSPITAL,
        help_text="Default segment for expenses in this category.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_expense_category")
        ]

    def __str__(self):
        return self.name


class Expense(BranchScopedModel):
    number = models.CharField(max_length=20)
    date = models.DateField()
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    segment = models.CharField(max_length=12, choices=segments.SEGMENTS)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(CENT)]
    )
    method = models.ForeignKey(
        "billing.PaymentMethod", on_delete=models.PROTECT, related_name="expenses"
    )
    payee = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=80, blank=True, help_text="Bill or receipt number.")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=200, blank=True)
    idempotency_key = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_expense_number"),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_expense_idempotency_key",
            ),
        ]
        indexes = [models.Index(fields=["branch", "date"])]

    def __str__(self):
        return self.number

    @property
    def is_void(self) -> bool:
        return self.voided_at is not None
