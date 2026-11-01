from django.db.models.signals import post_migrate
from django.dispatch import receiver


@receiver(post_migrate)
def load_standard_lists(sender, app_config=None, **kwargs):
    # Runs once per migrate: an update brings new entries from defaults.json to every hospital.
    if app_config is None or app_config.label != "masterdata":
        return
    from .defaults import load_for_all

    load_for_all()
