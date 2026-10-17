from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "SehatDesk core"

    def ready(self):
        from . import monitoring

        monitoring.connect()
