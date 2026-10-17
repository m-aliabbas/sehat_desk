from django.conf import settings

from .features import enabled_features
from .navigation import build_nav


def shell(request):
    """Data the app shell needs on every page: navigation, organization, branch, permissions."""
    ctx = {"workspace_env": settings.SEHATDESK_ENV}
    if getattr(request, "user", None) is None or not request.user.is_authenticated:
        return ctx
    ctx.update(
        {
            "nav_items": build_nav(request),
            "organization": getattr(request, "organization", None),
            "current_branch": getattr(request, "branch", None),
            "branches": getattr(request, "branches", []),
            # Template permission helper: {% if "patient.create" in sd_perms %}. UI only.
            "sd_perms": getattr(request, "sd_perms", frozenset()),
            # Module switches: {% if features.lab %}. UI only; FeatureMiddleware enforces.
            "features": enabled_features(getattr(request, "organization", None)),
        }
    )
    return ctx
