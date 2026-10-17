"""Every URL in the project, checked automatically (Phase 26/27).

New views are covered the moment they are added to a urls.py: anonymous visitors must be sent
to sign-in, and a signed-in user without permissions must be refused. Public and personal
pages are listed explicitly below, so opening a new one is a visible, reviewed decision.
"""

import re

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.accounts.models import Role, RoleAssignment

# Reachable without signing in (each has its own protection).
PUBLIC = {
    "accounts:login",
    "accounts:setup",  # 404 once any user exists
    "accounts:password-reset",
    "accounts:password-reset-done",
    "accounts:password-reset-confirm",
    "accounts:password-reset-complete",
    "core:health-live",
    "core:health-ready",
    "core:health-metrics",  # loopback only, else 404
    "core:client-error",  # same-origin POST only
    "queue:display",  # secret per-branch key, token numbers only
    "?media",  # organization logos only
}

# Open to any signed-in staff member: their own account, inbox and shell.
PERSONAL = {
    "core:dashboard",
    "accounts:logout",
    "accounts:password-change",
    "accounts:workspace",
    "accounts:switch-branch",
    "accounts:no-access",
    "notifications:center",
    "notifications:read-all",
    "notifications:open",
    "notifications:read",
    "search:page",
    "search:suggest",
    "search:recent-clear",
    "core:module-placeholder",
}

_CONVERTERS = re.compile(r"<(?:(\w+):)?(\w+)>")


def _walk(patterns, prefix="", namespace=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            ns = f"{namespace}:{p.namespace}".strip(":") if p.namespace else namespace
            yield from _walk(p.url_patterns, prefix + str(p.pattern), ns)
        elif isinstance(p, URLPattern):
            name = f"{namespace}:{p.name}" if p.name else "?media"
            yield name, prefix + str(p.pattern)


def _concrete(route: str) -> str:
    if route.startswith("^"):  # the media regex
        return "/media/organization-logos/x.png"
    return "/" + _CONVERTERS.sub(lambda m: "1" if m.group(1) == "int" else "x", route)


ROUTES = sorted(set(_walk(get_resolver().url_patterns)))


def test_inventory_is_complete():
    names = {n for n, _ in ROUTES}
    assert len(ROUTES) > 200
    assert (PUBLIC | PERSONAL) - names == set(), "allow-list names a URL that no longer exists"


@pytest.mark.parametrize("name, route", [r for r in ROUTES if r[0] not in PUBLIC])
def test_anonymous_visitors_are_sent_to_sign_in(client, admin_user, name, route):
    url = _concrete(route)
    login = reverse("accounts:login")
    for method in ("get", "post"):
        response = getattr(client, method)(url)
        assert response.status_code in (302, 404, 405), (method, url, response.status_code)
        if response.status_code == 302:
            assert response.url.startswith(login), (method, url, response.url)


@pytest.fixture
def nobody(org, branch, make_user):
    """Signed-in staff member whose only role grants nothing."""
    role = Role.objects.create(organization=org, name="Visitor")
    user = make_user(username="nobody")
    RoleAssignment.objects.create(user=user, role=role, branch=None)
    return user


@pytest.mark.parametrize("name, route", [r for r in ROUTES if r[0] not in PUBLIC | PERSONAL])
def test_staff_without_permissions_are_refused(client, nobody, admin_user, name, route):
    client.force_login(nobody)
    url = _concrete(route)
    for method in ("get", "post"):
        response = getattr(client, method)(url)
        assert response.status_code in (403, 404, 405), (method, url, response.status_code)
