"""In-app notifications (Phase 19) and the records for external messages (SMS, WhatsApp,
email) that later phases will send.

One Notification row per recipient: a message to "every pharmacist in this branch" is fanned
out when it is raised, so read state is per person. dedupe_key stops the same alert being
raised twice for one person (e.g. the daily low-stock alert).

Keep patient detail out of notifications: name the record (MRN, order number) and link to it;
the linked page applies its own permission checks.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel
from apps.core.scoping import OrgScopedModel


class Event(models.TextChoices):
    APPOINTMENT_UPCOMING = "appointment_upcoming", "Appointment upcoming"
    APPOINTMENT_CANCELLED = "appointment_cancelled", "Appointment cancelled"
    LAB_READY = "lab_ready", "Lab result ready"
    LAB_CRITICAL = "lab_critical", "Critical lab result"
    LOW_STOCK = "low_stock", "Pharmacy low stock"
    EXPIRY = "expiry", "Medicine expiry"
    REMINDER = "reminder", "Workflow reminder"
    GENERAL = "general", "General"


class Level(models.TextChoices):
    INFO = "info", "Info"
    WARNING = "warning", "Needs attention"
    CRITICAL = "critical", "Critical"


class Notification(TimeStampedModel):
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    event = models.CharField(max_length=24, choices=Event.choices, default=Event.GENERAL)
    level = models.CharField(max_length=8, choices=Level.choices, default=Level.INFO)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=300, blank=True)
    url = models.CharField(max_length=300, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "read_at", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="uniq_notification_dedupe",
            )
        ]

    def __str__(self):
        return self.title

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class Channel(models.TextChoices):
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"
    EMAIL = "email", "Email"


class MessageTemplate(OrgScopedModel):
    """Hospital wording for an external message. Placeholders look like {patient_name}; only
    the names listed for the event are filled in (see channels.PLACEHOLDERS)."""

    event = models.CharField(max_length=24, choices=Event.choices)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    subject = models.CharField(max_length=160, blank=True, help_text="Email only.")
    body = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["event", "channel"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "event", "channel"], name="uniq_message_template"
            )
        ]

    def __str__(self):
        return f"{self.get_event_display()} · {self.get_channel_display()}"


class MessageDelivery(OrgScopedModel):
    """One external message and what happened to it."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped (channel not set up)"

    event = models.CharField(max_length=24, choices=Event.choices)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    to = models.CharField(max_length=160)
    subject = models.CharField(max_length=160, blank=True)
    body = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    provider = models.CharField(max_length=40, blank=True)
    provider_message_id = models.CharField(max_length=120, blank=True)
    error = models.CharField(max_length=300, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel} {self.status}"
