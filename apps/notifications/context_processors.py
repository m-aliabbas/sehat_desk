from .services import for_user


def notifications(request):
    """Unread count and the latest few notifications for the topbar bell."""
    user = getattr(request, "user", None)
    org = getattr(request, "organization", None)
    if user is None or not user.is_authenticated or org is None:
        return {}
    qs = for_user(user, org)
    return {
        "notif_unread": qs.filter(read_at__isnull=True).count(),
        "notif_latest": list(qs[:6]),
    }
