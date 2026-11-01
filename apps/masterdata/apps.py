from django.apps import AppConfig


class MasterdataConfig(AppConfig):
    name = "apps.masterdata"
    label = "masterdata"
    verbose_name = "Departments, doctors and services"

    def ready(self):
        from . import signals  # noqa: F401
