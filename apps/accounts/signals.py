from django.db.models.signals import post_migrate
from django.dispatch import receiver


@receiver(post_migrate)
def sync_permission_catalogue(sender, app_config=None, **kwargs):
    # Runs once per migrate, after the accounts tables exist.
    if app_config is None or app_config.label != "accounts":
        return
    from .services import sync_permissions

    sync_permissions()
