from urllib.parse import urlencode, urlsplit

from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.http import HttpResponse
from django.shortcuts import redirect, resolve_url
from django.utils import timezone, translation

from . import services

SESSION_BRANCH_KEY = "sd_branch_id"
# Set on login, cleared on logout. If present while signed out, the session timed out.
HAD_SESSION_COOKIE = "sd_had_session"


class SehatDeskLoginRequiredMiddleware(LoginRequiredMiddleware):
    """Django's login-required default, plus a clean redirect for htmx requests."""

    def handle_no_permission(self, request, view_func):
        if request.headers.get("HX-Request"):
            # Return to the page the user was on, not the fragment URL htmx requested.
            current = urlsplit(request.headers.get("HX-Current-URL", "")).path or "/"
            response = HttpResponse(status=204)
            response["HX-Redirect"] = (
                f"{resolve_url(self.get_login_url(view_func))}?{urlencode({'next': current})}"
            )
            return response
        return super().handle_no_permission(request, view_func)


class WorkspaceMiddleware:
    """Attaches organization, branch and permissions to every signed-in request.

    request.organization, request.branch, request.branches, request.sd_perms
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.branch = None
        request.branches = []
        request.sd_perms = frozenset()

        user = request.user
        if user.is_authenticated and user.organization_id:
            org = user.organization
            request.organization = org
            branches = list(services.accessible_branches(user))
            request.branches = branches
            wanted = request.session.get(SESSION_BRANCH_KEY)
            branch = next((b for b in branches if b.id == wanted), None)
            if branch is None and branches:
                branch = branches[0]
                request.session[SESSION_BRANCH_KEY] = branch.id
            request.branch = branch
            request.sd_perms = services.effective_permissions(user, branch)
            timezone.activate(org.timezone)
            translation.activate(org.language)

        response = self.get_response(request)
        timezone.deactivate()
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        if not user.is_authenticated or getattr(view_func, "sd_workspace_exempt", False):
            return None
        if getattr(view_func, "login_required", True) is False:
            return None  # public views (health checks, login page)
        if user.must_change_password:
            return redirect("accounts:password-change")
        if request.branch is None:
            return redirect("accounts:no-access")
        return None
