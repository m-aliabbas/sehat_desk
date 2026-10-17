from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "apps.audit"
    label = "audit"
    verbose_name = "Audit trail"

    def ready(self):
        from . import signals  # noqa: F401
