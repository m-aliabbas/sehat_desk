from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.scoping import BranchScopedModel
from apps.masterdata.models import money


class AppointmentQuerySet(models.QuerySet):
    def active(self):
        """Appointments that still hold their slot."""
        return self.exclude(status__in=Appointment.RELEASED_STATUSES)


class Appointment(BranchScopedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked in"
        IN_QUEUE = "in_queue", "In queue"
        WITH_DOCTOR = "with_doctor", "With doctor"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"

    class VisitType(models.TextChoices):
        NEW = "new", "New consultation"
        FOLLOW_UP = "follow_up", "Follow-up"
        WALK_IN = "walk_in", "Walk-in"
        EMERGENCY = "emergency", "Emergency"

    class Source(models.TextChoices):
        COUNTER = "counter", "At the counter"
        PHONE = "phone", "Phone"
        REFERRAL = "referral", "Referral"
        OTHER = "other", "Other"

    RELEASED_STATUSES = (Status.CANCELLED, Status.NO_SHOW)
    # Allowed status changes. in_queue / with_doctor are used from Phases 6 and 8.
    TRANSITIONS = {
        Status.SCHEDULED: {Status.CONFIRMED, Status.CHECKED_IN, Status.CANCELLED, Status.NO_SHOW},
        Status.CONFIRMED: {Status.CHECKED_IN, Status.CANCELLED, Status.NO_SHOW},
        Status.CHECKED_IN: {
            Status.IN_QUEUE,
            Status.WITH_DOCTOR,
            Status.COMPLETED,
            Status.CANCELLED,
        },
        Status.IN_QUEUE: {Status.WITH_DOCTOR, Status.COMPLETED, Status.CANCELLED},
        Status.WITH_DOCTOR: {Status.COMPLETED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
        Status.NO_SHOW: set(),
    }
    RESCHEDULABLE = (Status.SCHEDULED, Status.CONFIRMED)

    number = models.CharField(max_length=20)
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="appointments"
    )
    doctor = models.ForeignKey(
        "masterdata.DoctorProfile", on_delete=models.PROTECT, related_name="appointments"
    )
    department = models.ForeignKey(
        "masterdata.Department", on_delete=models.PROTECT, related_name="appointments"
    )
    date = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    visit_type = models.CharField(max_length=12, choices=VisitType.choices, default=VisitType.NEW)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.COUNTER)
    reason = models.CharField(max_length=200, blank=True, help_text="Reason for visit (brief).")
    notes = models.TextField(blank=True)
    fee = models.DecimalField(**money, help_text="Expected consultation fee. Billed in Phase 7.")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)
    is_overbooked = models.BooleanField(default=False)

    checked_in_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=200, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reschedule_count = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    objects = AppointmentQuerySet.as_manager()

    class Meta:
        ordering = ["date", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_appointment_number"
            )
        ]
        indexes = [
            models.Index(fields=["branch", "doctor", "date"]),
            # Same-day duplicate check, follow-up suggestion and patient history.
            models.Index(fields=["patient", "date"], name="appt_patient_date"),
        ]

    def __str__(self):
        return f"{self.number} · {self.patient.full_name}"

    def can_transition(self, new_status) -> bool:
        return new_status in self.TRANSITIONS.get(self.status, set())

    @property
    def starts_at(self):
        return timezone.make_aware(datetime.combine(self.date, self.start_time))

    @property
    def is_past_start(self) -> bool:
        return timezone.now() >= self.starts_at

    @property
    def can_reschedule(self) -> bool:
        return self.status in self.RESCHEDULABLE

    @property
    def duration_minutes(self) -> int:
        start = datetime.combine(self.date, self.start_time)
        end = datetime.combine(self.date, self.end_time)
        return int((end - start).total_seconds() // 60)
