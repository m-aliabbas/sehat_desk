from django.apps import AppConfig


class LabConfig(AppConfig):
    name = "apps.lab"
    label = "lab"
    verbose_name = "Laboratory"

    def ready(self):
        from . import signals  # noqa: F401
