"""OPD encounters (PRD §6): one consultation between a doctor and a patient.

draft ──complete──▶ completed ──reopen (consultation.reopen + reason)──▶ draft ──complete──▶ ...

While a draft, the note fields are saved as the doctor types (autosave). Completing signs the
note: its content is copied into an EncounterVersion that is never edited again. Reopening keeps
every signed version, so a completed record is corrected by a new version, never overwritten
(CLAUDE.md rule). Clinical text never goes into audit logs; only field names do.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import BranchScopedModel, OrgScopedModel

# The consultation note, in the order the workspace shows it. (field, label, section)
NOTE_FIELDS = [
    ("chief_complaint", "Chief complaint", "history"),
    ("history_present_illness", "History of present illness", "history"),
    ("past_medical_history", "Past medical history", "history"),
    ("surgical_history", "Surgical history", "history"),
    ("family_history", "Family / social history", "history"),
    ("allergy_notes", "Allergy review", "history"),
    ("current_medicines", "Current medicines", "history"),
    ("examination_notes", "Examination", "exam"),
    ("provisional_diagnosis", "Provisional diagnosis", "assessment"),
    ("final_diagnosis", "Final diagnosis", "assessment"),
    ("treatment_plan", "Treatment plan", "plan"),
    ("follow_up_date", "Follow-up date", "plan"),
    ("follow_up_instructions", "Follow-up instructions", "plan"),
    ("referral_to", "Refer to", "plan"),
    ("referral_reason", "Referral reason", "plan"),
    ("doctor_notes", "Doctor notes", "plan"),
]
NOTE_FIELD_NAMES = [f for f, _, _ in NOTE_FIELDS]
# Signed alongside the note (Phase 10): coded diagnoses and the prescription lines.
VERSION_EXTRA_KEYS = ["diagnoses", "prescription", "advice"]


class Encounter(BranchScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        COMPLETED = "completed", "Completed"

    number = models.CharField(max_length=20)
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="encounters"
    )
    doctor = models.ForeignKey(
        "masterdata.DoctorProfile", on_delete=models.PROTECT, related_name="encounters"
    )
    department = models.ForeignKey(
        "masterdata.Department", on_delete=models.PROTECT, related_name="encounters"
    )
    appointment = models.OneToOneField(
        "appointments.Appointment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="encounter",
    )
    token = models.OneToOneField(
        "queue.QueueToken",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="encounter",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    visit_reason = models.CharField(max_length=200, blank=True)

    # The note. Free text for now; structured diagnosis codes arrive in Phase 10.
    chief_complaint = models.TextField(blank=True)
    history_present_illness = models.TextField(blank=True)
    past_medical_history = models.TextField(blank=True)
    surgical_history = models.TextField(blank=True)
    family_history = models.TextField(blank=True)
    allergy_notes = models.TextField(blank=True)
    current_medicines = models.TextField(blank=True)
    examination_notes = models.TextField(blank=True)
    provisional_diagnosis = models.TextField(blank=True)
    final_diagnosis = models.TextField(blank=True)
    treatment_plan = models.TextField(blank=True)
    follow_up_date = models.DateField(null=True, blank=True)
    follow_up_instructions = models.TextField(blank=True)
    referral_to = models.CharField(max_length=160, blank=True)
    referral_reason = models.TextField(blank=True)
    doctor_notes = models.TextField(blank=True)

    # Bumped on every save, so two open tabs cannot silently overwrite each other.
    revision = models.PositiveIntegerField(default=0)
    last_saved_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reopen_reason = models.CharField(
        max_length=300, blank=True, help_text="Why the open amendment was started."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_encounter_number")
        ]
        indexes = [
            models.Index(fields=["doctor", "status"]),
            models.Index(fields=["patient", "-started_at"]),
        ]

    def __str__(self):
        return self.number

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    @property
    def is_amendment(self) -> bool:
        """A reopened encounter that already has a signed version."""
        return self.is_draft and self.reopened_at is not None

    def note(self) -> dict:
        return {
            f: (str(getattr(self, f)) if getattr(self, f) is not None else "")
            for f in NOTE_FIELD_NAMES
        }

    def filled_fields(self) -> list[str]:
        return [f for f, v in self.note().items() if v.strip()]


class EncounterVersion(models.Model):
    """A signed copy of the note. Append-only: never edited or deleted by the app."""

    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveSmallIntegerField()
    content = models.JSONField()
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    signed_at = models.DateTimeField(default=timezone.now)
    amendment_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["encounter", "version"], name="uniq_encounter_version")
        ]

    def __str__(self):
        return f"{self.encounter.number} v{self.version}"

    def changed_from(self, previous) -> list[str]:
        """Labels of note fields that differ from an earlier version."""
        if previous is None:
            return []
        labels = dict((f, label) for f, label, _ in NOTE_FIELDS)
        labels |= {"diagnoses": "Diagnoses", "prescription": "Prescription", "advice": "Advice"}
        return [
            labels.get(f, f)
            for f in [*NOTE_FIELD_NAMES, *VERSION_EXTRA_KEYS]
            if (self.content.get(f) or "") != (previous.content.get(f) or "")
        ]

    def diagnoses(self) -> list[dict]:
        return self.content.get("diagnoses") or []

    def prescription(self) -> list[dict]:
        return self.content.get("prescription") or []


# --- Diagnoses (Phase 10) ---------------------------------------------------------------------


class DiagnosisCode(OrgScopedModel):
    """Searchable diagnosis catalogue (ICD-10 style codes). A starter list is loaded on first use;
    hospitals can add their own codes."""

    code = models.CharField(max_length=10)
    title = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_diagnosis_code")
        ]

    def __str__(self):
        return f"{self.code} · {self.title}"


class EncounterDiagnosis(TimeStampedModel):
    """A diagnosis on a consultation: coded, or free text when no code fits.

    Editable while the encounter is a draft. Completing copies the list into the signed
    EncounterVersion, so later amendments never lose what was signed.
    """

    class Kind(models.TextChoices):
        PROVISIONAL = "provisional", "Provisional"
        FINAL = "final", "Final"

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="diagnoses")
    code = models.ForeignKey(
        DiagnosisCode, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    text = models.CharField(max_length=200, help_text="Copied from the code, or free text.")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.PROVISIONAL)
    is_primary = models.BooleanField(default=False)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-is_primary", "created_at", "pk"]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"{self.code.code} · {self.text}" if self.code_id else self.text

    def snapshot(self) -> dict:
        return {
            "code": self.code.code if self.code_id else "",
            "text": self.text,
            "kind": self.kind,
            "primary": self.is_primary,
        }
