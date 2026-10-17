from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from . import services


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    services.record(request, "auth.login", user, actor=user, organization=user.organization)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        services.record(request, "auth.logout", user, actor=user, organization=user.organization)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    # Only the attempted username is kept; Django already strips the password from credentials.
    services.record(
        request, "auth.login_failed", summary=f"username={credentials.get('username', '')}"[:255]
    )
