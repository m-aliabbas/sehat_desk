import secrets

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
    feature_lab = models.BooleanField("Laboratory module", default=True)
    feature_pharmacy = models.BooleanField("Pharmacy and inventory module", default=True)
    feature_ipd = models.BooleanField("Inpatient (IPD) and nursing module", default=True)
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
    # Waiting-room token display.
    display_refresh_seconds = models.PositiveSmallIntegerField(
        "Display refresh (seconds)",
        default=5,
        validators=[MinValueValidator(3), MaxValueValidator(60)],
    )
    display_show_doctor = models.BooleanField(
        "Show doctor names on the waiting-room display", default=True
    )
    # Pharmacy.
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
