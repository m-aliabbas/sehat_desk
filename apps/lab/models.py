"""Laboratory (PRD §8): test catalogue, orders, samples, results, verification and reports.

Order flow:  payment pending ─paid─▶ sample pending ─collect─▶ sample collected ─start─▶
processing ─results─▶ result entered ─verify─▶ verified ─release─▶ released.
The order status is derived from its items, samples and invoice (services.refresh_status);
views never set it.

Results are clinical records. Until verification the current result may be re-entered. After
verification it is locked: only a verifier can amend it, which stores a new LabResult version
with a reason and sends it back for verification. Older versions are never edited or deleted.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import BranchScopedModel, OrgScopedModel
from apps.masterdata.models import money


class SampleType(models.TextChoices):
    BLOOD = "blood", "Whole blood (EDTA)"
    SERUM = "serum", "Serum"
    PLASMA = "plasma", "Plasma"
    URINE = "urine", "Urine"
    STOOL = "stool", "Stool"
    SWAB = "swab", "Swab"
    SPUTUM = "sputum", "Sputum"
    CSF = "csf", "CSF"
    OTHER = "other", "Other"


class LabTestCategory(OrgScopedModel):
    name = models.CharField(max_length=80)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "lab test categories"
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_lab_category")
        ]

    def __str__(self):
        return self.name


class LabTest(OrgScopedModel):
    class ResultType(models.TextChoices):
        NUMERIC = "numeric", "Numeric"
        TEXT = "text", "Text"
        POSNEG = "posneg", "Positive / negative"
        CHOICE = "choice", "Choose from a list"

    code = models.CharField(max_length=16)
    name = models.CharField(max_length=120)
    category = models.ForeignKey(LabTestCategory, on_delete=models.PROTECT, related_name="tests")
    sample_type = models.CharField(max_length=10, choices=SampleType.choices)
    price = models.DecimalField(**money, default=Decimal("0"))
    turnaround_hours = models.PositiveSmallIntegerField(
        default=24, help_text="Expected time from sample collection to report."
    )
    result_type = models.CharField(
        max_length=10, choices=ResultType.choices, default=ResultType.NUMERIC
    )
    unit = models.CharField(max_length=30, blank=True, help_text="e.g. g/dL, mg/dL, ×10⁹/L")
    decimals = models.PositiveSmallIntegerField(default=1)
    options = models.CharField(
        max_length=300, blank=True, help_text="For a list result: options separated by commas."
    )
    normal_value = models.CharField(
        max_length=60,
        blank=True,
        help_text="For positive/negative or list results: the normal answer, e.g. Negative.",
    )
    method = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category__sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_lab_test_code")
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"

    @property
    def option_list(self) -> list[str]:
        if self.result_type == self.ResultType.POSNEG:
            return ["Negative", "Positive"]
        return [o.strip() for o in self.options.split(",") if o.strip()]

    def range_for(self, patient):
        """The most specific reference range for the patient's sex and age, or None."""
        age = patient.age
        best = None
        for r in self.ranges.all():
            if r.gender not in ("any", patient.gender):
                continue
            if age is not None and (
                age < r.age_min_years or (r.age_max_years is not None and age > r.age_max_years)
            ):
                continue
            if age is None and (r.age_min_years > 0 or r.age_max_years is not None):
                continue
            # An age band beats a sex-only range: paediatric ranges matter more than sex.
            score = (r.age_min_years > 0 or r.age_max_years is not None) * 2 + (r.gender != "any")
            if best is None or score > best[0]:
                best = (score, r)
        return best[1] if best else None


class LabReferenceRange(TimeStampedModel):
    GENDERS = [("any", "Any"), ("male", "Male"), ("female", "Female")]

    test = models.ForeignKey(LabTest, on_delete=models.CASCADE, related_name="ranges")
    gender = models.CharField(max_length=6, choices=GENDERS, default="any")
    age_min_years = models.PositiveSmallIntegerField(default=0)
    age_max_years = models.PositiveSmallIntegerField(null=True, blank=True)
    low = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    high = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    critical_low = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    critical_high = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)

    class Meta:
        ordering = ["gender", "age_min_years"]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        def fmt(v):
            return f"{Decimal(v).normalize():f}" if v is not None else ""

        if self.low is not None and self.high is not None:
            text = f"{fmt(self.low)} – {fmt(self.high)}"
        elif self.low is not None:
            text = f"≥ {fmt(self.low)}"
        elif self.high is not None:
            text = f"≤ {fmt(self.high)}"
        else:
            text = ""
        who = []
        if self.gender != "any":
            who.append(self.get_gender_display())
        if self.age_min_years or self.age_max_years is not None:
            who.append(f"{self.age_min_years}–{self.age_max_years or '∞'} y")
        return f"{text} ({', '.join(who)})" if who and text else text


class LabPanel(OrgScopedModel):
    """A package of tests sold together at a package price (e.g. LFT, Lipid profile)."""

    code = models.CharField(max_length=16)
    name = models.CharField(max_length=120)
    price = models.DecimalField(**money, default=Decimal("0"))
    tests = models.ManyToManyField(LabTest, related_name="panels")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_lab_panel_code")
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


class LabOrder(BranchScopedModel):
    class Status(models.TextChoices):
        PAYMENT_PENDING = "payment_pending", "Payment pending"
        SAMPLE_PENDING = "sample_pending", "Sample pending"
        SAMPLE_COLLECTED = "sample_collected", "Sample collected"
        PROCESSING = "processing", "Processing"
        RESULT_ENTERED = "result_entered", "Result entered"
        VERIFIED = "verified", "Verified"
        RELEASED = "released", "Released"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        DOCTOR = "doctor", "Doctor"
        DESK = "desk", "Lab desk"

    class Priority(models.TextChoices):
        ROUTINE = "routine", "Routine"
        URGENT = "urgent", "Urgent"
        STAT = "stat", "STAT"

    number = models.CharField(max_length=20)
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="lab_orders"
    )
    encounter = models.ForeignKey(
        "encounters.Encounter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lab_orders",
    )
    doctor = models.ForeignKey(
        "masterdata.DoctorProfile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lab_orders",
        help_text="Ordering doctor (optional for walk-in lab patients).",
    )
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.DESK)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.ROUTINE)
    clinical_notes = models.CharField(max_length=300, blank=True)
    status = models.CharField(
        max_length=18, choices=Status.choices, default=Status.SAMPLE_PENDING, db_index=True
    )
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    has_critical = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    amendment_count = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_lab_order_number")
        ]
        indexes = [models.Index(fields=["branch", "status", "-created_at"])]

    def __str__(self):
        return self.number

    @property
    def payment_label(self) -> str:
        if self.invoice_id is None:
            return "Not billed"
        return self.invoice.get_status_display()

    @property
    def is_paid(self) -> bool:
        from apps.billing.models import Invoice

        return self.invoice_id is None or self.invoice.status in (
            Invoice.Status.PAID,
            Invoice.Status.REFUNDED,
        )

    @property
    def is_open(self) -> bool:
        return self.status not in (self.Status.RELEASED, self.Status.CANCELLED)


class LabSample(BranchScopedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for collection"
        COLLECTED = "collected", "Collected"
        REJECTED = "rejected", "Rejected"

    order = models.ForeignKey(LabOrder, on_delete=models.CASCADE, related_name="samples")
    sample_type = models.CharField(max_length=10, choices=SampleType.choices)
    accession = models.CharField(max_length=20, help_text="Printed on the tube label.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    collected_at = models.DateTimeField(null=True, blank=True)
    collected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    rejection_reason = models.CharField(max_length=200, blank=True)
    recollection_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="recollection"
    )

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "accession"], name="uniq_accession")
        ]

    def __str__(self):
        return self.accession


class LabOrderItem(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Sample pending"
        COLLECTED = "collected", "Sample collected"
        PROCESSING = "processing", "Processing"
        RESULT_ENTERED = "result_entered", "Result entered"
        VERIFIED = "verified", "Verified"
        CANCELLED = "cancelled", "Cancelled"

    order = models.ForeignKey(LabOrder, on_delete=models.CASCADE, related_name="items")
    test = models.ForeignKey(LabTest, on_delete=models.PROTECT, related_name="+")
    panel = models.ForeignKey(
        LabPanel, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    sample = models.ForeignKey(
        LabSample, on_delete=models.PROTECT, null=True, blank=True, related_name="items"
    )
    # Copied from the catalogue when ordered, so catalogue edits never change old reports.
    test_name = models.CharField(max_length=120)
    unit = models.CharField(max_length=30, blank=True)
    price = models.DecimalField(**money, default=Decimal("0"))
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.test_name

    @property
    def current_result(self):
        return next((r for r in self.results.all() if r.is_current), None)


class LabResult(TimeStampedModel):
    class Flag(models.TextChoices):
        NONE = "", "—"
        NORMAL = "normal", "Normal"
        LOW = "low", "Low"
        HIGH = "high", "High"
        CRITICAL_LOW = "critical_low", "Critical low"
        CRITICAL_HIGH = "critical_high", "Critical high"
        ABNORMAL = "abnormal", "Abnormal"

    CRITICAL_FLAGS = (Flag.CRITICAL_LOW, Flag.CRITICAL_HIGH)

    item = models.ForeignKey(LabOrderItem, on_delete=models.PROTECT, related_name="results")
    version = models.PositiveSmallIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    value_numeric = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    value_text = models.TextField(blank=True)
    interpretation = models.TextField(blank=True)
    flag = models.CharField(max_length=14, choices=Flag.choices, blank=True, default="")
    reference_text = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=30, blank=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_at = models.DateTimeField(default=timezone.now)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    amendment_reason = models.CharField(max_length=300, blank=True)
    critical_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    critical_acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["item", "version"], name="uniq_lab_result_version")
        ]
        # Unacknowledged critical results on worklists and doctor workspaces.
        indexes = [models.Index(fields=["flag", "is_current"], name="labresult_flag_current")]

    def __str__(self):
        return f"{self.item} v{self.version}"

    @property
    def is_critical(self) -> bool:
        return self.flag in self.CRITICAL_FLAGS

    @property
    def is_abnormal(self) -> bool:
        return self.flag not in ("", self.Flag.NORMAL)

    @property
    def display_value(self) -> str:
        if self.value_numeric is not None:
            decimals = self.item.test.decimals if self.item_id else 1
            return f"{self.value_numeric:.{decimals}f}"
        return self.value_text


def lab_attachment_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    order = instance.item.order
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    return (
        f"lab-attachments/{order.organization_id}/{order.number}/{instance.item_id}-{stamp}.{ext}"
    )


def validate_attachment_size(file):
    from django.core.exceptions import ValidationError

    if file.size > 10 * 1024 * 1024:
        raise ValidationError("Attachments can be at most 10 MB.")


class LabAttachment(TimeStampedModel):
    """A file (analyser printout, image) on a result. Served by a permission-checked view."""

    item = models.ForeignKey(LabOrderItem, on_delete=models.PROTECT, related_name="attachments")
    file = models.FileField(
        upload_to=lab_attachment_path,
        validators=[
            FileExtensionValidator(["pdf", "png", "jpg", "jpeg", "webp"]),
            validate_attachment_size,
        ],
    )
    original_name = models.CharField(max_length=200)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    def __str__(self):
        return self.original_name
