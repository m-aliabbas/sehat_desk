"""Shared model conventions for every SehatDesk app.

- TimeStampedModel: created_at / updated_at on every table.
- ArchivableModel: soft delete. Clinical and financial records are archived, never hard-deleted.
  List views must call .active(). The default manager returns archived rows too.
"""

from django.db import models, transaction
from django.db.models import F
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ArchivableQuerySet(models.QuerySet):
    def active(self):
        return self.filter(archived_at__isnull=True)

    def archived(self):
        return self.filter(archived_at__isnull=False)


class ArchivableModel(TimeStampedModel):
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ArchivableQuerySet.as_manager()

    class Meta:
        abstract = True

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def archive(self) -> None:
        self.archived_at = timezone.now()
        self.save(update_fields=["archived_at", "updated_at"])

    def restore(self) -> None:
        self.archived_at = None
        self.save(update_fields=["archived_at", "updated_at"])


class Sequence(models.Model):
    """Gap-tolerant counters per organization, e.g. MRNs ("patient") and appointment numbers.

    Use next_number(); never read and increment last_value yourself.
    """

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="+"
    )
    key = models.CharField(max_length=40)
    last_value = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "key"], name="uniq_sequence_per_org")
        ]

    def __str__(self):
        return f"{self.key}={self.last_value}"


def next_number(organization, key: str) -> int:
    """Atomically take the next number. The UPDATE ... SET last_value = last_value + 1 locks the
    row on PostgreSQL and serializes writers on SQLite, so two desks never get the same number."""
    with transaction.atomic():
        seq, _ = Sequence.objects.get_or_create(organization=organization, key=key)
        Sequence.objects.filter(pk=seq.pk).update(last_value=F("last_value") + 1)
        seq.refresh_from_db(fields=["last_value"])
        return seq.last_value


class ErrorReport(models.Model):
    """An unexpected server exception or a browser script error (Settings → System).

    Holds no patient data: the path without its query string, the exception type and stack
    frames (never the exception message), and for browser errors a short redacted message.
    """

    class Source(models.TextChoices):
        SERVER = "server", "Server"
        BROWSER = "browser", "Browser"

    RETENTION_DAYS = 90

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    branch = models.ForeignKey(
        "organizations.Branch", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    method = models.CharField(max_length=10, blank=True)
    path = models.CharField(max_length=300, blank=True)
    view_name = models.CharField(max_length=150, blank=True)
    error_type = models.CharField(max_length=200)
    message = models.CharField(max_length=300, blank=True)  # browser errors only, redacted
    stack = models.TextField(blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["organization", "-occurred_at"])]

    def __str__(self):
        return f"{self.get_source_display()} {self.error_type} at {self.path}"
