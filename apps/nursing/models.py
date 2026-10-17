"""Nursing care for admitted patients (Phase 17): assignments, notes, vitals schedule,
intake/output, the medication administration record (MAR) and shift handover.

Everything here is a clinical record. Nothing is edited or deleted: a wrong entry is marked
"entered in error" with a reason and stays visible, struck through. Every action is also
written to the audit log against the admission, which is the nursing audit trail.
"""

from datetime import date, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel


class ErrorMarkable(models.Model):
    entered_in_error = models.BooleanField(default=False)
    error_reason = models.CharField(max_length=200, blank=True)
    error_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    error_marked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class Shift(models.TextChoices):
    MORNING = "morning", "Morning (07:00–14:00)"
    EVENING = "evening", "Evening (14:00–21:00)"
    NIGHT = "night", "Night (21:00–07:00)"


def current_shift(at=None) -> tuple[str, date]:
    """(shift, shift date). The night shift belongs to the date it started on."""
    at = timezone.localtime(at)
    if 7 <= at.hour < 14:
        return Shift.MORNING, at.date()
    if 14 <= at.hour < 21:
        return Shift.EVENING, at.date()
    start = at.date() if at.hour >= 21 else at.date() - timedelta(days=1)
    return Shift.NIGHT, start


class NurseAssignment(TimeStampedModel):
    admission = models.ForeignKey(
        "ipd.Admission", on_delete=models.PROTECT, related_name="nurse_assignments"
    )
    nurse = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="nurse_assignments"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["admission", "nurse"],
                condition=Q(ended_at__isnull=True),
                name="uniq_open_nurse_assignment",
            )
        ]

    def __str__(self):
        return f"{self.nurse} · {self.admission}"


class NursingNote(ErrorMarkable, TimeStampedModel):
    class Kind(models.TextChoices):
        GENERAL = "general", "General"
        ASSESSMENT = "assessment", "Assessment"
        PROCEDURE = "procedure", "Procedure / care given"
        INCIDENT = "incident", "Incident / fall"
        EDUCATION = "education", "Patient / family education"

    admission = models.ForeignKey(
        "ipd.Admission", on_delete=models.PROTECT, related_name="nursing_notes"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.GENERAL)
    note = models.TextField()
    recorded_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"Nursing note {self.admission} {self.recorded_at:%Y-%m-%d %H:%M}"


class VitalsSchedule(TimeStampedModel):
    """How often observations are due for one admission."""

    INTERVALS = [(1, "Every hour"), (2, "Every 2 hours"), (4, "Every 4 hours"),
                 (6, "Every 6 hours"), (8, "Every 8 hours"), (12, "Every 12 hours"),
                 (24, "Once a day")]  # fmt: skip

    admission = models.OneToOneField(
        "ipd.Admission", on_delete=models.PROTECT, related_name="vitals_schedule"
    )
    interval_hours = models.PositiveSmallIntegerField(choices=INTERVALS, default=6)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return f"{self.admission} every {self.interval_hours} h"


class IntakeOutput(ErrorMarkable, TimeStampedModel):
    class Kind(models.TextChoices):
        INTAKE = "intake", "Intake"
        OUTPUT = "output", "Output"

    class Category(models.TextChoices):
        ORAL = "oral", "Oral fluids"
        IV = "iv", "IV fluids"
        FEED = "feed", "Tube feed"
        BLOOD = "blood", "Blood / products"
        URINE = "urine", "Urine"
        DRAIN = "drain", "Drain"
        VOMIT = "vomit", "Vomit / aspirate"
        STOOL = "stool", "Stool"
        OTHER = "other", "Other"

    INTAKE_CATEGORIES = {"oral", "iv", "feed", "blood", "other"}
    OUTPUT_CATEGORIES = {"urine", "drain", "vomit", "stool", "other"}

    admission = models.ForeignKey(
        "ipd.Admission", on_delete=models.PROTECT, related_name="intake_output"
    )
    kind = models.CharField(max_length=6, choices=Kind.choices)
    category = models.CharField(max_length=6, choices=Category.choices)
    volume_ml = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(10000)]
    )
    notes = models.CharField(max_length=200, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.volume_ml} ml"


class MedicationAdministration(ErrorMarkable, TimeStampedModel):
    """One charted dose on the MAR. Scheduled doses have scheduled_for; as-needed (SOS) and
    one-off (STAT) doses are charted without it."""

    class Status(models.TextChoices):
        GIVEN = "given", "Given"
        MISSED = "missed", "Missed"
        REFUSED = "refused", "Refused by patient"
        HELD = "held", "Held (clinical reason)"

    NEEDS_REASON = {Status.MISSED, Status.REFUSED, Status.HELD}

    order = models.ForeignKey(
        "ipd.MedicationOrder", on_delete=models.PROTECT, related_name="administrations"
    )
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(max_length=8, choices=Status.choices)
    reason = models.CharField(max_length=200, blank=True)
    dose_given = models.CharField(max_length=40, blank=True)
    administered_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-administered_at"]
        constraints = [
            # A scheduled dose is charted once; a wrong entry is marked in error, then re-charted.
            models.UniqueConstraint(
                fields=["order", "scheduled_for"],
                condition=Q(entered_in_error=False, scheduled_for__isnull=False),
                name="uniq_charted_scheduled_dose",
            )
        ]

    def __str__(self):
        return f"{self.order} {self.get_status_display()}"


class HandoverNote(TimeStampedModel):
    """Shift handover for a ward, acknowledged by the receiving nurse."""

    ward = models.ForeignKey("ipd.Ward", on_delete=models.PROTECT, related_name="handovers")
    shift = models.CharField(max_length=8, choices=Shift.choices)
    shift_date = models.DateField()
    summary = models.TextField(help_text="Patients to watch, changes during the shift.")
    pending_tasks = models.TextField(blank=True)
    given_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Handover {self.ward} {self.shift_date} {self.shift}"
