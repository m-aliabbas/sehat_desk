"""Staff attendance, pay profiles, doctor commission, payroll and staff ledgers (Phases 34–35).

Money rules (CLAUDE.md: financial writes are transactional and never silently overwritten):
- CommissionEarning rows are append-only. A refund or cancellation after payment posts a
  negative adjustment; the original row stays. Each row keeps the rate it was earned at.
- A payroll run is a draft until finalized. Finalizing posts the staff ledger and settles the
  commission rows in one transaction. A finalized run is never edited.
- StaffLedgerEntry is append-only. amount > 0: the hospital owes the staff member more;
  amount < 0: owes less (a payout or advance). Balance = sum of amounts.
"""

from datetime import time
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import BranchScopedModel
from apps.masterdata.models import WEEKDAYS, money

ZERO = Decimal("0.00")
percent = {
    "max_digits": 5,
    "decimal_places": 2,
    "default": ZERO,
    "validators": [MinValueValidator(ZERO), MaxValueValidator(Decimal("100"))],
}


class PayProfile(BranchScopedModel):
    """How one staff member is paid, and from which branch's payroll."""

    class PayType(models.TextChoices):
        MONTHLY = "monthly", "Monthly salary"
        DAILY = "daily", "Daily wage"
        COMMISSION = "commission", "Commission only"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pay_profile"
    )
    pay_type = models.CharField(max_length=12, choices=PayType.choices, default=PayType.MONTHLY)
    monthly_salary = models.DecimalField(**money, default=ZERO)
    daily_rate = models.DecimalField("Daily wage", **money, default=ZERO)
    deduct_absences = models.BooleanField(
        default=True,
        help_text="Monthly salary: deduct one day's pay for each day marked absent "
        "(half for a half day).",
    )
    work_start = models.TimeField(default=time(9), help_text="Check-ins after this are late.")
    late_grace_minutes = models.PositiveSmallIntegerField(default=15)
    weekly_off = models.PositiveSmallIntegerField(
        choices=WEEKDAYS, null=True, blank=True, default=6, help_text="Day off every week."
    )
    consultation_percent = models.DecimalField(
        "Consultation commission (%)",
        **percent,
        help_text="Doctor's share of each paid consultation.",
    )
    procedure_percent = models.DecimalField(
        "Procedure commission (%)",
        **percent,
        help_text="Doctor's share of each paid procedure they perform.",
    )
    lab_referral_percent = models.DecimalField(
        "Lab referral commission (%)",
        **percent,
        help_text="Doctor's share of paid lab tests they order.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["user__first_name", "user__last_name"]

    def __str__(self):
        return f"Pay profile · {self.user}"

    @property
    def has_commission(self) -> bool:
        return bool(
            self.consultation_percent
            or self.procedure_percent
            or self.lab_referral_percent
            or self.rules.exists()
        )


class CommissionRule(TimeStampedModel):
    """A per-procedure (or per-service) override of the profile's default commission."""

    profile = models.ForeignKey(PayProfile, on_delete=models.CASCADE, related_name="rules")
    service = models.ForeignKey("masterdata.Service", on_delete=models.PROTECT, related_name="+")
    percent = models.DecimalField("Commission (%)", **percent)
    fixed_amount = models.DecimalField(
        "Fixed amount (each)", **money, default=ZERO, help_text="Paid per unit, on top of %."
    )

    class Meta:
        ordering = ["service__name"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "service"], name="uniq_commission_rule")
        ]

    def __str__(self):
        return f"{self.service} · {self.percent}% + {self.fixed_amount}"


class AttendanceRecord(BranchScopedModel):
    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        LATE = "late", "Late"
        HALF_DAY = "half_day", "Half day"
        ABSENT = "absent", "Absent"
        LEAVE = "leave", "Leave (paid)"
        OFF = "off", "Off / holiday"

    SHORT = {
        "present": "P",
        "late": "L",
        "half_day": "H",
        "absent": "A",
        "leave": "LV",
        "off": "O",
    }

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attendance"
    )
    date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices)
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    self_marked = models.BooleanField(default=False)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-date", "user__first_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user", "date"], name="uniq_attendance_per_day"
            )
        ]
        indexes = [models.Index(fields=["branch", "date"])]

    def __str__(self):
        return f"{self.user} {self.date} {self.status}"

    @property
    def short(self) -> str:
        return self.SHORT.get(self.status, "")

    @property
    def hours(self):
        if self.check_in and self.check_out:
            return round((self.check_out - self.check_in).total_seconds() / 3600, 1)
        return None


class PayrollRun(BranchScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        FINALIZED = "finalized", "Finalized"

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "year", "month"], name="uniq_payroll_month")
        ]

    def __str__(self):
        return f"Payroll {self.year}-{self.month:02d}"

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    @property
    def label(self) -> str:
        from datetime import date

        return date(self.year, self.month, 1).strftime("%B %Y")


class Payslip(TimeStampedModel):
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="payslips")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payslips"
    )
    # Snapshot of the pay profile when calculated.
    pay_type = models.CharField(max_length=12, choices=PayProfile.PayType.choices)
    monthly_salary = models.DecimalField(**money, default=ZERO)
    daily_rate = models.DecimalField(**money, default=ZERO)
    working_days = models.PositiveSmallIntegerField(default=0)
    present_days = models.DecimalField(max_digits=5, decimal_places=1, default=ZERO)
    absent_days = models.DecimalField(max_digits=5, decimal_places=1, default=ZERO)
    leave_days = models.PositiveSmallIntegerField(default=0)
    late_count = models.PositiveSmallIntegerField(default=0)
    base_pay = models.DecimalField(**money, default=ZERO)
    absence_deduction = models.DecimalField(**money, default=ZERO)
    commission = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    bonus = models.DecimalField(**money, default=ZERO)
    deduction = models.DecimalField("Other deduction", **money, default=ZERO)
    adjustment_note = models.CharField(max_length=200, blank=True)
    net_pay = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)

    class Meta:
        ordering = ["user__first_name", "user__last_name"]
        constraints = [models.UniqueConstraint(fields=["run", "user"], name="uniq_payslip_per_run")]

    def __str__(self):
        return f"{self.run} · {self.user}"

    @property
    def salary_pay(self) -> Decimal:
        """Pay before commission, bonus and other deductions."""
        return self.base_pay - self.absence_deduction


class CommissionEarning(BranchScopedModel):
    """A doctor's share of one paid invoice line (or an adjustment after a refund)."""

    class Kind(models.TextChoices):
        EARNING = "earning", "Earned"
        ADJUSTMENT = "adjustment", "Refund / cancellation adjustment"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="commission_earnings"
    )
    invoice = models.ForeignKey("billing.Invoice", on_delete=models.PROTECT, related_name="+")
    invoice_item = models.ForeignKey(
        "billing.InvoiceItem", on_delete=models.PROTECT, related_name="commission_earnings"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.EARNING)
    segment = models.CharField(max_length=12)  # apps.finance.segments key
    basis = models.DecimalField(
        max_digits=12, decimal_places=2, help_text="Line amount the share is taken from."
    )
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    fixed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payslip = models.ForeignKey(
        Payslip, on_delete=models.PROTECT, null=True, blank=True, related_name="earnings"
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["organization", "user", "payslip"])]

    def __str__(self):
        return f"{self.user} {self.amount} on {self.invoice_item_id}"

    @property
    def clinic_share(self) -> Decimal:
        return self.basis - self.amount if self.kind == self.Kind.EARNING else ZERO


class StaffLedgerEntry(BranchScopedModel):
    class Kind(models.TextChoices):
        OPENING = "opening", "Opening balance"
        SALARY = "salary", "Salary / wage"
        COMMISSION = "commission", "Commission"
        BONUS = "bonus", "Bonus"
        DEDUCTION = "deduction", "Deduction"
        ADVANCE = "advance", "Advance paid"
        PAYMENT = "payment", "Payment to staff"
        ADJUSTMENT = "adjustment", "Adjustment"

    PAYOUT_KINDS = (Kind.ADVANCE, Kind.PAYMENT)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_ledger"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payslip = models.ForeignKey(
        Payslip, on_delete=models.PROTECT, null=True, blank=True, related_name="ledger_entries"
    )
    method = models.ForeignKey(
        "billing.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    idempotency_key = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name_plural = "staff ledger entries"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_staff_ledger_idempotency_key",
            )
        ]
        indexes = [models.Index(fields=["organization", "user", "created_at"])]

    def __str__(self):
        return f"{self.user} {self.get_kind_display()} {self.amount}"
