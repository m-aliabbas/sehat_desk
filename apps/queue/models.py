"""Reception counters and OPD queue tokens.

Token numbers run per branch, per department, per day (A-001, A-002, ...). A token sits in one
doctor's queue, or in the department queue when no doctor is chosen. Counter IDs identify reception
desks and never replace clinic token IDs (design kit data contracts).
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.scoping import BranchScopedModel


class Counter(BranchScopedModel):
    code = models.CharField(max_length=10, help_text="Stable desk ID, e.g. 01.")
    name = models.CharField(max_length=80, help_text="e.g. Main reception")
    assigned_staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_open = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "code"], name="uniq_counter_code_per_branch")
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


class QueueToken(BranchScopedModel):
    class Status(models.TextChoices):
        WAITING = "waiting", "Waiting"
        CALLED = "called", "Called"
        IN_CONSULTATION = "in_consultation", "In consultation"
        SKIPPED = "skipped", "Skipped"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Priority(models.TextChoices):
        NORMAL = "normal", "Normal"
        URGENT = "urgent", "Urgent"
        EMERGENCY = "emergency", "Emergency"
        SPECIAL = "special", "Special priority"

    # Lower is served first. Special (elderly, disabled, ...) needs the priority.special permission.
    PRIORITY_RANK = {
        Priority.EMERGENCY: 0,
        Priority.SPECIAL: 1,
        Priority.URGENT: 2,
        Priority.NORMAL: 3,
    }
    OPEN_STATUSES = (Status.WAITING, Status.CALLED, Status.IN_CONSULTATION, Status.SKIPPED)

    date = models.DateField(db_index=True)
    department = models.ForeignKey(
        "masterdata.Department", on_delete=models.PROTECT, related_name="tokens"
    )
    doctor = models.ForeignKey(
        "masterdata.DoctorProfile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="tokens",
        help_text="Empty = department queue; any doctor of the department can call it.",
    )
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="tokens")
    appointment = models.OneToOneField(
        "appointments.Appointment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="token",
    )
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=12)
    room = models.CharField(max_length=40, blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    priority_rank = models.PositiveSmallIntegerField(default=3, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)

    # Timestamps: queued_at moves when a skipped token is put back in line.
    queued_at = models.DateTimeField(default=timezone.now)
    called_at = models.DateTimeField(null=True, blank=True)
    call_count = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    skipped_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)
    called_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["priority_rank", "queued_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "department", "date", "number"], name="uniq_token_number_per_day"
            )
        ]
        indexes = [
            models.Index(fields=["branch", "date", "status"]),
            models.Index(fields=["doctor", "date", "status"], name="token_doctor_day"),
        ]

    def __str__(self):
        return self.label

    def save(self, *args, **kwargs):
        self.priority_rank = self.PRIORITY_RANK.get(self.priority, 3)
        super().save(*args, **kwargs)

    @property
    def wait_minutes(self) -> int:
        """Minutes from issue until called (or until now while still waiting)."""
        end = self.called_at or timezone.now()
        return max(0, int((end - self.created_at).total_seconds() // 60))

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES
