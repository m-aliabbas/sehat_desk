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
        return wrapper

    return decorator


def workspace_exempt(view):
    """Allow a signed-in view to run without a branch or before a forced password change."""
    view.sd_workspace_exempt = True
    return view


def has_perm(request, code: str) -> bool:
    return code in getattr(request, "sd_perms", frozenset())
