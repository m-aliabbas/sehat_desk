from django.conf import settings
from django.db import models


class AuditLogImmutable(Exception):
    """Raised when code tries to change or delete an audit event."""


class AuditLogQuerySet(models.QuerySet):
    """Bulk changes are refused here too; the database triggers (migration 0002) back this up."""

    def update(self, **kwargs):
        raise AuditLogImmutable("Audit events cannot be changed.")

    def delete(self):
        raise AuditLogImmutable("Audit events cannot be deleted.")

    _raw_delete = delete


class AuditLog(models.Model):
    """Append-only record of sensitive actions (PRD §18). Never edited or deleted by the app.

    Store IDs and changed field values for staff/config records. Do not put patient clinical
    content in `changes`; record which fields changed instead.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    action = models.CharField(max_length=60, db_index=True)  # e.g. "staff.create"
    entity = models.CharField(max_length=60, blank=True)  # e.g. "User"
    record_id = models.CharField(max_length=40, blank=True)
    summary = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(default=dict, blank=True)  # {"field": [old, new]}
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(
                fields=["organization", "actor", "-created_at"],
                name="audit_audit_organiz_actor_idx",
            ),
            models.Index(
                fields=["organization", "entity", "record_id"],
                name="audit_audit_organiz_entity_idx",
            ),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.entity}#{self.record_id}"

    def save(self, *args, **kwargs):
        """Insert only. An existing event is never rewritten (PRD §18)."""
        if self.pk is not None and not self._state.adding:
            raise AuditLogImmutable("Audit events cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable("Audit events cannot be deleted.")
