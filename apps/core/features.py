"""Module switches (Settings → Preferences). A switched-off module disappears from navigation
and its pages answer 404 on the server, for every role. Data is kept; switching back on
restores everything.
"""

from django.shortcuts import render

# feature -> (URL namespaces it owns, extra view names it owns, nav item keys)
FEATURES = {
    "lab": (("lab",), ("reports:lab-dashboard",), ("laboratory",)),
    "pharmacy": (("pharmacy",), ("reports:pharmacy-dashboard",), ("pharmacy", "inventory")),
    "ipd": (("ipd", "nursing"), (), ("ipd", "nursing")),
}
LABELS = {"lab": "Laboratory", "pharmacy": "Pharmacy and inventory", "ipd": "Inpatient and nursing"}


def enabled_features(organization) -> dict[str, bool]:
    if organization is None:
        return dict.fromkeys(FEATURES, True)
    from apps.organizations.models import preferences_for

    prefs = preferences_for(organization)
    return {key: getattr(prefs, f"feature_{key}") for key in FEATURES}


def is_enabled(organization, key: str) -> bool:
    return enabled_features(organization)[key]


def disabled_nav_keys(organization) -> set[str]:
    flags = enabled_features(organization)
    return {nav for key, (_ns, _v, navs) in FEATURES.items() if not flags[key] for nav in navs}


def feature_for(match) -> str | None:
    if match is None:
        return None
    for key, (namespaces, view_names, _nav) in FEATURES.items():
        if match.view_name in view_names or set(match.namespaces) & set(namespaces):
            return key
    return None


class FeatureMiddleware:
    """After WorkspaceMiddleware (needs request.organization)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        key = feature_for(getattr(request, "resolver_match", None))
        org = getattr(request, "organization", None)
        if key and org is not None and not is_enabled(org, key):
            return render(
                request, "core/module_disabled.html", {"feature": LABELS[key]}, status=404
            )
        return None
