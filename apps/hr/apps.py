from django.apps import AppConfig


class HrConfig(AppConfig):
    name = "apps.hr"
    label = "hr"
    verbose_name = "HR & payroll"

    def ready(self):
        from . import signals  # noqa: F401
