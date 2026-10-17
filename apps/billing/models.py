"""Invoices, payments and refunds (PRD §11).

Money rules:
- An invoice is a draft while items are added. Issuing it locks the items and the totals.
- line total = quantity × unit price − line discount + tax on that net amount.
- grand total = sum of line totals − invoice discount.
- balance due = grand total − amount paid. Payments can never exceed the balance.
- A refund returns money from one original payment. It does not reopen the balance: the
  refunded amount is credited to the patient, so the invoice stays settled.
- Financial rows are never edited or deleted. Mistakes are corrected with a refund or by
  cancelling an invoice that has no money on it. Every change is audit-logged.
Totals are stored on the invoice (recalculated by services.recalculate) so lists and daily
reports do not have to sum items on every page load.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.scoping import BranchScopedModel, OrgScopedModel
from apps.masterdata.models import money

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def to_money(value) -> Decimal:
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


class BillingSettings(OrgScopedModel):
    """Per-organization billing configuration (Settings → Billing). One row per organization."""

    tax_label = models.CharField(max_length=20, default="Tax", help_text="e.g. GST or Sales tax")
    default_tax_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal("100"))],
        help_text="Applied to new invoice items. 0 = no tax. Existing items keep their rate.",
    )
    max_discount_percent = models.DecimalField(
        "Largest discount allowed (%)",
        max_digits=5,
        decimal_places=2,
        default=Decimal("100"),
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal("100"))],
        help_text="Cap on any one discount, as a percent of the line or invoice. Applies to "
        "staff who hold the discount permission.",
    )
    receipt_message = models.CharField(
        max_length=160,
        default="Thank you. Please keep this receipt.",
        blank=True,
        help_text="Last line on payment receipts.",
    )

    class Meta:
        verbose_name_plural = "billing settings"
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="uniq_billing_settings_per_org")
        ]

    def __str__(self):
        return f"Billing settings · {self.organization}"


class PaymentMethod(OrgScopedModel):
    """Configurable ways to pay (PRD §11.3). Defaults are created for each organization."""

    DEFAULTS = [
        ("cash", "Cash", False),
        ("card", "Card", True),
        ("bank_transfer", "Bank transfer", True),
        ("easypaisa", "Easypaisa", True),
        ("jazzcash", "JazzCash", True),
        ("other", "Other", True),
    ]

    code = models.SlugField(max_length=30)
    name = models.CharField(max_length=60)
    requires_reference = models.BooleanField(
        default=False, help_text="Ask for a transaction or slip number when this method is used."
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_payment_method")
        ]

    def __str__(self):
        return self.name


class Invoice(BranchScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        UNPAID = "unpaid", "Unpaid"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    OUTSTANDING_STATUSES = (Status.UNPAID, Status.PARTIALLY_PAID)

    number = models.CharField(max_length=20)
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
        help_text="Empty only for walk-in pharmacy sales; customer_name is used then.",
    )
    customer_name = models.CharField(
        max_length=120, blank=True, help_text="Walk-in customer (no patient record)."
    )
    appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invoices",
        help_text="The OPD visit this invoice bills, when there is one.",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    subtotal = models.DecimalField(**money, default=ZERO)  # quantity × unit price, all lines
    line_discount_total = models.DecimalField(**money, default=ZERO)
    tax_total = models.DecimalField(**money, default=ZERO)
    invoice_discount = models.DecimalField(**money, default=ZERO)
    grand_total = models.DecimalField(**money, default=ZERO)
    amount_paid = models.DecimalField(**money, default=ZERO)
    amount_refunded = models.DecimalField(**money, default=ZERO)

    discount_reason = models.CharField(max_length=200, blank=True)
    discount_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    notes = models.CharField(max_length=300, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_invoice_number")
        ]
        indexes = [
            models.Index(fields=["branch", "status", "-created_at"]),
            models.Index(fields=["organization", "patient", "-created_at"], name="invoice_patient"),
        ]

    def __str__(self):
        return self.number

    @property
    def bill_to(self) -> str:
        return self.patient.full_name if self.patient_id else (self.customer_name or "Walk-in")

    @property
    def discount_total(self) -> Decimal:
        return self.line_discount_total + self.invoice_discount

    @property
    def balance_due(self) -> Decimal:
        if self.status in (self.Status.DRAFT, self.Status.CANCELLED):
            return ZERO
        return max(ZERO, self.grand_total - self.amount_paid)

    @property
    def refundable(self) -> Decimal:
        return max(ZERO, self.amount_paid - self.amount_refunded)

    @property
    def net_paid(self) -> Decimal:
        return self.amount_paid - self.amount_refunded

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    @property
    def is_outstanding(self) -> bool:
        return self.status in self.OUTSTANDING_STATUSES


class InvoiceItem(models.Model):
    class Kind(models.TextChoices):
        CONSULTATION = "consultation", "Consultation"
        SERVICE = "service", "Service"
        PROCEDURE = "procedure", "Procedure"
        LAB = "lab", "Laboratory"
        PHARMACY = "pharmacy", "Pharmacy"
        ROOM = "room", "Room / bed"
        MISC = "misc", "Miscellaneous"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SERVICE)
    service = models.ForeignKey(
        "masterdata.Service", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Copied from the service when added, so later price or name changes never alter old bills.
    description = models.CharField(max_length=160)
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(**money)
    discount = models.DecimalField(**money, default=ZERO)
    discount_reason = models.CharField(max_length=200, blank=True)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    tax_amount = models.DecimalField(**money, default=ZERO)
    line_total = models.DecimalField(**money, default=ZERO)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.description} × {self.quantity}"

    @property
    def gross(self) -> Decimal:
        return to_money(self.unit_price * self.quantity)

    def compute(self) -> None:
        """Fill tax_amount and line_total from quantity, price, discount and tax rate."""
        net = self.gross - to_money(self.discount)
        self.tax_amount = to_money(net * self.tax_percent / 100)
        self.line_total = net + self.tax_amount


class Payment(BranchScopedModel):
    number = models.CharField(max_length=20)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(CENT)]
    )
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    # One per submitted payment form, so a double click or a resubmitted page never charges twice.
    idempotency_key = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_payment_number"),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_payment_idempotency_key",
            ),
        ]
        indexes = [models.Index(fields=["branch", "-created_at"])]

    def __str__(self):
        return self.number

    @property
    def refunded_amount(self) -> Decimal:
        return self.refunds.aggregate(s=models.Sum("amount"))["s"] or ZERO

    @property
    def refundable(self) -> Decimal:
        return max(ZERO, self.amount - self.refunded_amount)


class Refund(BranchScopedModel):
    number = models.CharField(max_length=20)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="refunds")
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name="refunds",
        help_text="The original payment the money is returned from.",
    )
    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(CENT)]
    )
    reason = models.CharField(max_length=200)
    reference = models.CharField(max_length=80, blank=True)
    refunded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="The authorised user (holds billing.refund) who issued the refund.",
    )
    idempotency_key = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_refund_number"),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_refund_idempotency_key",
            ),
        ]

    def __str__(self):
        return self.number
