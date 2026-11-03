from django.conf import settings

from apps.masterdata.quick_add import allowed_kinds
from apps.recyclebin.services import deletable_keys

from . import licensing
from .features import enabled_features
from .navigation import build_nav


def public_organization():
    """The hospital on this server, for signed-out pages. Nothing when none or several exist."""
    from apps.organizations.models import Organization

    orgs = list(Organization.objects.order_by("id")[:2])
    return orgs[0] if len(orgs) == 1 else None


def shell(request):
    """Data the app shell needs on every page: navigation, organization, branch, permissions."""
    ctx = {"workspace_env": settings.SEHATDESK_ENV}
    if getattr(request, "user", None) is None or not request.user.is_authenticated:
        ctx["public_organization"] = public_organization  # called only if a template uses it
        return ctx
    ctx.update(
        {
            "nav_items": build_nav(request),
            "organization": getattr(request, "organization", None),
            "current_branch": getattr(request, "branch", None),
            "branches": getattr(request, "branches", []),
            # Template permission helper: {% if "patient.create" in sd_perms %}. UI only.
            "sd_perms": getattr(request, "sd_perms", frozenset()),
            "quick_add_kinds": allowed_kinds(getattr(request, "sd_perms", frozenset())),
            # Module switches: {% if features.lab %}. UI only; FeatureMiddleware enforces.
            "features": enabled_features(getattr(request, "organization", None)),
            "license": licensing.status(),
            # Kinds this person may delete to the Recycle bin: {% if "product" in deletable %}.
            "deletable": deletable_keys(request),
        }
    )
    return ctx
