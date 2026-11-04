"""Backend permission guards. Hiding a button is never enough; every view uses one of these."""

from functools import wraps

from django.core.exceptions import PermissionDenied


def permission_required(*codes: str, any_of: bool = False):
    """Require all listed permission codes (or any one with any_of=True) in the current branch."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            held = getattr(request, "sd_perms", frozenset())
            ok = any(c in held for c in codes) if any_of else all(c in held for c in codes)
            if not ok:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        wrapper.sd_permissions = codes
        wrapper.sd_any_of = any_of
        return wrapper

    return decorator


def workspace_exempt(view):
    """Allow a signed-in view to run without a branch or before a forced password change."""
    view.sd_workspace_exempt = True
    return view


def has_perm(request, code: str) -> bool:
    return code in getattr(request, "sd_perms", frozenset())


def can_open(request, url: str) -> bool:
    """Whether this person passes the permission guard of the view behind a local URL. Used to
    show a link only to people it will not refuse (breadcrumbs, cross-module links)."""
    from django.urls import Resolver404, resolve

    try:
        func = resolve((url or "").split("?")[0]).func
    except Resolver404:
        return False
    codes = getattr(func, "sd_permissions", None)
    if not codes:
        return True
    held = getattr(request, "sd_perms", frozenset())
    if getattr(func, "sd_any_of", False):
        return any(c in held for c in codes)
    return all(c in held for c in codes)
