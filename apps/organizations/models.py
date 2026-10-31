import secrets
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models

from apps.core.models import TimeStampedModel

CURRENCY_CHOICES = [("PKR", "PKR · Pakistani rupee")]
LANGUAGE_CHOICES = [("en", "English"), ("ur", "Urdu (layout coming later)")]
TIMEZONE_CHOICES = [("Asia/Karachi", "Asia/Karachi")]

_prefix_validator = RegexValidator(r"^[A-Z0-9]{1,6}$", "Use 1–6 capital letters or digits.")

LOGO_MAX_BYTES = 2 * 1024 * 1024


def validate_logo_size(file):
    if file.size > LOGO_MAX_BYTES:
        raise ValidationError("Logo must be 2 MB or smaller.")


class Organization(TimeStampedModel):
    """A hospital or clinic group. Every business record belongs to exactly one organization."""

    name = models.CharField(max_length=150)
    registration_number = models.CharField(max_length=60, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    # SVG is deliberately not allowed: it can carry scripts.
    logo = models.ImageField(
        upload_to="organization-logos/",
        blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"]), validate_logo_size],
    )
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="PKR")
    timezone = models.CharField(max_length=40, choices=TIMEZONE_CHOICES, default="Asia/Karachi")
    language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default="en")
    patient_id_prefix = models.CharField(max_length=6, default="SD", validators=[_prefix_validator])
    receipt_prefix = models.CharField(max_length=6, default="INV", validators=[_prefix_validator])
    report_header = models.TextField(
        blank=True, help_text="Printed under the hospital name on receipts and reports."
    )
    report_footer = models.TextField(
        blank=True, help_text="Printed at the bottom of receipts and reports."
    )
    print_terms = models.TextField(
        "Terms and conditions",
        blank=True,
        help_text="Small print on invoices and receipts, e.g. refund and return policy.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Branch(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="branches"
    )
    name = models.CharField(max_length=120)
    code = models.CharField(
        max_length=10,
        validators=[RegexValidator(r"^[A-Z0-9-]{1,10}$", "Use capital letters, digits or -.")],
        help_text="Short code used on tokens and receipts, e.g. ISB-1.",
    )
    city = models.CharField(max_length=80, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    is_main = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    # Secret for the public waiting-room token display (no staff login on the TV).
    display_key = models.CharField(max_length=40, blank=True, db_index=True)

    class Meta:
        ordering = ["-is_main", "name"]
        verbose_name_plural = "branches"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="uniq_branch_code_per_org"
            ),
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_branch_name_per_org"
            ),
        ]

    def __str__(self):
        return f"{self.name} · {self.city}" if self.city else self.name

    def regenerate_display_key(self):
        self.display_key = secrets.token_urlsafe(24)
        self.save(update_fields=["display_key", "updated_at"])


class OrganizationPreferences(TimeStampedModel):
    """Hospital-wide switches and defaults (Settings → Preferences). One row per organization;
    use `preferences_for(org)`, which creates it on first use."""

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="preferences"
    )
    # Modules. Turning one off hides it and blocks its pages on the server; records are kept.
    feature_lab = models.BooleanField("Laboratory", default=True)
    feature_pharmacy = models.BooleanField("Pharmacy and inventory", default=True)
    feature_ipd = models.BooleanField("IPD and wards", default=True)
    feature_nursing = models.BooleanField(
        "Nursing desk",
        default=True,
        help_text="Ward tasks and shift handover. Needs IPD and wards.",
    )
    # Clinical defaults for new doctors (each doctor can still be changed).
    default_slot_minutes = models.PositiveSmallIntegerField(
        "Default consultation length (minutes)",
        default=15,
        validators=[MinValueValidator(5), MaxValueValidator(240)],
    )
    default_follow_up_days = models.PositiveSmallIntegerField(
        "Default follow-up window (days)",
        default=7,
        validators=[MaxValueValidator(365)],
        help_text="A visit within this many days of the last one counts as a follow-up.",
    )
    # Reception (OPD) flow.
    use_doctor_shifts = models.BooleanField(
        "Doctors work in shifts",
        default=False,
        help_text="Morning, evening and night clinics per doctor. Reception picks a shift.",
    )
    opd_collect_fee_upfront = models.BooleanField(
        "Collect the doctor's fee before the visit",
        default=True,
        help_text="Reception makes the fee invoice and takes the money when the patient arrives.",
    )
    opd_print_blank_prescription = models.BooleanField(
        "Print an empty prescription for each visit",
        default=True,
        help_text="For doctors who write by hand. Shows the patient, token and any vitals.",
    )
    opd_print_office_copy = models.BooleanField(
        "Print an office copy of the fee invoice",
        default=True,
        help_text="Off = only the patient copy is printed.",
    )
    opd_auto_print = models.BooleanField(
        "Open the print window straight after booking", default=True
    )
    # Waiting-room token display.
    display_refresh_seconds = models.PositiveSmallIntegerField(
        "Display refresh (seconds)",
        default=5,
        validators=[MinValueValidator(3), MaxValueValidator(60)],
    )
    display_show_doctor = models.BooleanField(
        "Show doctor names on the waiting-room display", default=True
    )
    # Settings → Fields & tax (apps/organizations/field_rules.py).
    field_rules = models.JSONField(default=dict, blank=True)
    default_sale_tax_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    default_purchase_gst_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    # Pharmacy.
    pharmacy_require_prescription = models.BooleanField(
        "Refuse Rx-only medicines without a prescription",
        default=False,
        help_text="Off = the sale goes through and is recorded in the audit log.",
    )
    pharmacy_auto_print_receipt = models.BooleanField(
        "Print the receipt as soon as a sale is paid", default=True
    )
    near_expiry_days = models.PositiveSmallIntegerField(
        "Expiry alert (days before expiry)",
        default=90,
        validators=[MinValueValidator(7), MaxValueValidator(365)],
    )
    default_reorder_level = models.PositiveIntegerField(
        "Default reorder level for new products",
        default=0,
        help_text="In base units (tablets, ml). 0 = no low-stock alert until set per product.",
    )

    class Meta:
        verbose_name_plural = "organization preferences"

    def __str__(self):
        return f"Preferences · {self.organization}"


def preferences_for(organization) -> OrganizationPreferences:
    cached = getattr(organization, "_sd_preferences", None)
    if cached is None:
        cached, _ = OrganizationPreferences.objects.get_or_create(organization=organization)
        organization._sd_preferences = cached
    return cached


class PrintLayout(TimeStampedModel):
    """How one kind of printed document looks. One row per organization and document; a document
    with no row uses the defaults below. Hospital name, logo and texts stay in Settings → Hospital.
    """

    class Document(models.TextChoices):
        PRESCRIPTION = "prescription", "Prescription"
        FEE_SLIP = "fee_slip", "Doctor's fee slip"
        INVOICE = "invoice", "General invoice"
        PHARMACY_INVOICE = "pharmacy_invoice", "Pharmacy invoice"
        LAB_INVOICE = "lab_invoice", "Lab invoice"
        PROCEDURE_INVOICE = "procedure_invoice", "Procedure invoice"
        LAB_REPORT = "lab_report", "Lab report"

    class Paper(models.TextChoices):
        A4 = "A4", "A4"
        A5 = "A5", "A5"
        LETTER = "letter", "Letter"
        THERMAL = "thermal", "Thermal roll (80 mm)"

    class Side(models.TextChoices):
        LEFT = "left", "Left"
        CENTER = "center", "Centre"
        RIGHT = "right", "Right"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="print_layouts"
    )
    document = models.CharField(max_length=20, choices=Document.choices)
    paper = models.CharField(max_length=10, choices=Paper.choices, blank=True, default="")
    margin_mm = models.PositiveSmallIntegerField(
        "Page margin (mm)", default=10, validators=[MaxValueValidator(40)]
    )
    top_space_mm = models.PositiveSmallIntegerField(
        "Blank space at top (mm)",
        default=0,
        validators=[MaxValueValidator(120)],
        help_text="For paper with a pre-printed letterhead.",
    )
    bottom_space_mm = models.PositiveSmallIntegerField(
        "Blank space at bottom (mm)", default=0, validators=[MaxValueValidator(120)]
    )
    text_size = models.PositiveSmallIntegerField(
        "Text size (%)",
        default=100,
        validators=[MinValueValidator(70), MaxValueValidator(150)],
    )
    show_header = models.BooleanField("Print the hospital header", default=True)
    header_align = models.CharField(
        "Header position", max_length=6, choices=Side.choices, default=Side.LEFT
    )
    show_logo = models.BooleanField("Print the logo", default=True)
    logo_mm = models.PositiveSmallIntegerField(
        "Logo size (mm)", default=14, validators=[MinValueValidator(5), MaxValueValidator(60)]
    )
    header_text = models.TextField(
        "Extra header text", blank=True, help_text="Below the hospital's own header."
    )
    show_footer = models.BooleanField("Print the footer", default=True)
    footer_text = models.TextField(
        "Extra footer text", blank=True, help_text="Above the hospital's own footer."
    )
    # Prescription only.
    doctor_side = models.CharField(
        "Doctor's name position", max_length=6, choices=Side.choices, default=Side.LEFT
    )
    show_qualifications = models.BooleanField("Doctor's qualifications", default=True)
    show_registration = models.BooleanField("PMDC number", default=True)
    show_vitals = models.BooleanField("Vitals", default=True)
    show_allergies = models.BooleanField("Allergies", default=True)
    extra_lines = models.TextField(
        "Extra lines on an empty prescription",
        blank=True,
        help_text="One per line, e.g. Diagnosis, Tests advised, Next visit. Printed with space "
        "to write.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "document"], name="uniq_print_layout_per_document"
            )
        ]

    def __str__(self):
        return f"{self.get_document_display()} layout · {self.organization}"

    @property
    def lines(self) -> list[str]:
        return [line.strip() for line in self.extra_lines.splitlines() if line.strip()]


def print_layout_for(organization, document) -> PrintLayout:
    """The saved layout, or an unsaved one holding the defaults."""
    return PrintLayout.objects.filter(
        organization=organization, document=document
    ).first() or PrintLayout(organization=organization, document=document)
