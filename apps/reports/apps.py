from django.apps import AppConfig


class ReportsConfig(AppConfig):
    name = "apps.reports"
    label = "reports"
    verbose_name = "Reports and dashboards"

    def ready(self):
        from . import definitions  # noqa: F401  (registers every report)
