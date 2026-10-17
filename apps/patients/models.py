"""Patients are organization-wide: one MRN is used in every branch and department.

Demographic edits are audited. Allergies are clinical facts and are never deleted; a wrong
entry is marked "entered in error" with a reason, and the history stays.
"""

from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import OrgScopedModel

from .identity import format_cnic, format_phone

PHOTO_MAX_BYTES = 3 * 1024 * 1024


def validate_photo_size(file):
    if file.size > PHOTO_MAX_BYTES:
        raise ValidationError("Photo must be 3 MB or smaller.")


def patient_photo_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    # Not under media/organization-logos/, so the public media route never serves it.
    return f"patient-photos/{instance.organization_id}/{instance.mrn}.{ext}"


class Patient(OrgScopedModel):
    class Gender(models.TextChoices):
        FEMALE = "female", "Female"
        MALE = "male", "Male"
        OTHER = "other", "Other"
        UNKNOWN = "unknown", "Not recorded"

    class BloodGroup(models.TextChoices):
        UNKNOWN = "", "Not recorded"
        A_POS = "A+", "A+"
        A_NEG = "A-", "A−"
        B_POS = "B+", "B+"
        B_NEG = "B-", "B−"
        AB_POS = "AB+", "AB+"
        AB_NEG = "AB-", "AB−"
        O_POS = "O+", "O+"
        O_NEG = "O-", "O−"

    class AllergyStatus(models.TextChoices):
        # "Unknown" and "none known" are different clinical facts (design kit state checklist).
        UNKNOWN = "unknown", "Not asked yet"
        NONE = "none", "No known allergies"
        PRESENT = "present", "Has allergies"

    mrn = models.CharField("MRN", max_length=20)
    full_name = models.CharField(max_length=150)
    gender = models.CharField(max_length=10, choices=Gender.choices, default=Gender.UNKNOWN)
    date_of_birth = models.DateField(null=True, blank=True)
    dob_is_estimated = models.BooleanField(default=False)
    phone = models.CharField(max_length=20, blank=True, help_text="Stored normalized.")
    cnic = models.CharField(
        "CNIC", max_length=13, blank=True, help_text="13 digits, stored without dashes."
    )
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=80, blank=True)
    emergency_contact_name = models.CharField(max_length=150, blank=True)
    emergency_contact_phone = models.CharField(max_length=20, blank=True)
    emergency_contact_relation = models.CharField(max_length=40, blank=True)
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True, default="")
    allergy_status = models.CharField(
        max_length=10, choices=AllergyStatus.choices, default=AllergyStatus.UNKNOWN
    )
    photo = models.ImageField(
        upload_to=patient_photo_path,
        blank=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp"]), validate_photo_size],
    )
    consent_recorded = models.BooleanField(
        default=False, help_text="Patient consent for contact and record use recorded."
    )
    registered_branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    is_active = models.BooleanField(default=True)
    # Lower-cased copy of full_name for fast case-insensitive search on SQLite and PostgreSQL.
    name_search = models.CharField(max_length=150, editable=False, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "mrn"], name="uniq_patient_mrn")
        ]
        indexes = [
            models.Index(fields=["organization", "phone"]),
            models.Index(fields=["organization", "cnic"]),
            models.Index(fields=["organization", "emergency_contact_phone"]),
            # Patient list (newest first) and name search within one hospital.
            models.Index(fields=["organization", "-created_at"], name="patient_org_recent"),
            models.Index(fields=["organization", "name_search"], name="patient_org_name"),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.mrn})"

    def save(self, *args, **kwargs):
        self.name_search = " ".join(self.full_name.lower().split())
        super().save(*args, **kwargs)

    @property
    def age(self) -> int | None:
        if not self.date_of_birth:
            return None
        today = date.today()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    @property
    def age_label(self) -> str:
        age = self.age
        if age is None:
            return "Age not recorded"
        if age == 0:
            months = (date.today() - self.date_of_birth).days // 30
            return f"{months} months"
        return f"{'~' if self.dob_is_estimated else ''}{age} years"

    @property
    def phone_display(self) -> str:
        return format_phone(self.phone)

    @property
    def cnic_display(self) -> str:
        return format_cnic(self.cnic)

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return ((parts[0][0] + parts[-1][0]) if len(parts) > 1 else self.full_name[:2]).upper()


class PatientIdentifier(TimeStampedModel):
    """Extra identifiers besides the patient's own CNIC (passport, guardian CNIC, ...)."""

    class Kind(models.TextChoices):
        PASSPORT = "passport", "Passport"
        GUARDIAN_CNIC = "guardian_cnic", "Guardian CNIC"
        B_FORM = "b_form", "B-Form (child registration)"
        OTHER = "other", "Other"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="identifiers")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    value = models.CharField(max_length=40, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "kind", "value"], name="uniq_patient_identifier"
            )
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.value}"


class Allergy(TimeStampedModel):
    class Severity(models.TextChoices):
        UNKNOWN = "unknown", "Severity unknown"
        MILD = "mild", "Mild"
        MODERATE = "moderate", "Moderate"
        SEVERE = "severe", "Severe"

    class Source(models.TextChoices):
        REPORTED = "reported", "Patient reported"
        VERIFIED = "verified", "Clinically verified"

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="allergies")
    substance = models.CharField(max_length=120)
    reaction = models.CharField(max_length=200, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.UNKNOWN)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.REPORTED)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_in_error = models.BooleanField(default=False)
    error_reason = models.CharField(max_length=200, blank=True)
    error_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["entered_in_error", "-created_at"]
        verbose_name_plural = "allergies"

    def __str__(self):
        return self.substance


class PatientAlert(TimeStampedModel):
    """Non-allergy flags shown in the patient header, e.g. "Hearing impaired"."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="alerts")
    message = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
