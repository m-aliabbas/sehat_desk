from django.apps import AppConfig


class FlowConfig(AppConfig):
    name = "apps.flow"
    label = "flow"
    verbose_name = "Patient flow"

    def ready(self):
        from . import signals  # noqa: F401
