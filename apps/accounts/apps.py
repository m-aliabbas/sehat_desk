from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Staff accounts and roles"

    def ready(self):
        from . import signals  # noqa: F401
