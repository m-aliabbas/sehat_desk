"""Sign-in, first-run setup, passwords and branch (workspace) selection."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.core.security import rate_limit

from . import services
from .decorators import workspace_exempt
from .forms import LoginForm, SetupForm, StyledPasswordChangeForm, StyledSetPasswordForm
from .middleware import HAD_SESSION_COOKIE, SESSION_BRANCH_KEY
from .models import User


def setup_needed() -> bool:
    return not User.objects.exists()


# --- First-run setup ---------------------------------------------------------------


@login_not_required
@rate_limit("setup", limit=10, seconds=900)
def setup(request):
    """Shown once on a fresh install, then permanently disabled (404) once any user exists."""
    if not setup_needed():
        raise Http404
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        org, branch, user = services.create_organization_with_admin(
            org_name=data["org_name"],
            branch_name=data["branch_name"],
            branch_code=data["branch_code"],
            city=data["city"],
            admin_data={
                "username": data["username"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "email": data["email"],
                "password": data["password1"],
            },
        )
        audit.record(request, "setup.complete", org, actor=user, organization=org)
        login(request, user, backend="apps.accounts.backends.SehatDeskBackend")
        messages.success(request, f"{org.name} is set up. Next, add your staff and branches.")
        response = redirect("core:dashboard")
        response.set_cookie(HAD_SESSION_COOKIE, "1", httponly=True, samesite="Lax")
        return response
    return render(request, "accounts/setup.html", {"form": form})


# --- Login / logout ------------------------------------------------------------------


class LoginView(auth_views.LoginView):
    form_class = LoginForm
    redirect_authenticated_user = True

    # Overriding dispatch drops Django's own marker, so re-apply it.
    @method_decorator(login_not_required)
    def dispatch(self, request, *args, **kwargs):
        if setup_needed():
            return redirect("accounts:setup")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["session_expired"] = (
            self.request.method == "GET" and self.request.COOKIES.get(HAD_SESSION_COOKIE) == "1"
        )
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        response.set_cookie(HAD_SESSION_COOKIE, "1", httponly=True, samesite="Lax")
        return response

    def get_success_url(self):
        url = self.get_redirect_url()
        if url:
            return url
        # Several branches: let the user pick where they are working today.
        if len(services.accessible_branches(self.request.user)[:2]) > 1:
            return reverse_lazy("accounts:workspace")
        return reverse_lazy("core:dashboard")


class LogoutView(auth_views.LogoutView):
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        response.delete_cookie(HAD_SESSION_COOKIE)
        return response


logout_view = workspace_exempt(LogoutView.as_view())


# --- Passwords ------------------------------------------------------------------------


@workspace_exempt
def password_change(request):
    """Own password change. Forced after an admin reset (must_change_password)."""
    form = StyledPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        update_session_auth_hash(request, user)
        audit.record(request, "auth.password_change", user)
        messages.success(request, "Your password has been changed.")
        return redirect("core:dashboard")
    return render(
        request,
        "accounts/password_change.html",
        {"form": form, "forced": request.user.must_change_password},
    )


_reset_kwargs = {"extra_context": {"email_configured": settings.EMAIL_CONFIGURED}}

# Reset requests send email and confirm pages test tokens: limit both per computer.
password_reset = login_not_required(
    rate_limit("password-reset", limit=5, seconds=900)(
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            email_template_name="accounts/email/password_reset.txt",
            subject_template_name="accounts/email/password_reset_subject.txt",
            success_url=reverse_lazy("accounts:password-reset-done"),
            **_reset_kwargs,
        )
    )
)
password_reset_done = login_not_required(
    auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_done.html")
)
password_reset_confirm = login_not_required(
    rate_limit("password-reset-confirm", limit=10, seconds=900)(
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            form_class=StyledSetPasswordForm,
            success_url=reverse_lazy("accounts:password-reset-complete"),
        )
    )
)
password_reset_complete = login_not_required(
    auth_views.PasswordResetCompleteView.as_view(
        template_name="accounts/password_reset_complete.html"
    )
)


# --- Workspace (branch) ------------------------------------------------------------------


@workspace_exempt
def no_access(request):
    """Signed in, but no active role in any active branch ("Access not assigned")."""
    if request.branch is not None:
        return redirect("core:dashboard")
    return render(request, "accounts/no_access.html", status=403)


def workspace(request):
    """Choose the branch to work in when the user has more than one."""
    rows = [
        {"branch": b, "roles": _roles_in_branch(request.user, b), "current": b == request.branch}
        for b in request.branches
    ]
    return render(request, "accounts/workspace.html", {"rows": rows})


def _roles_in_branch(user, branch):
    return sorted(
        {
            a.role.name
            for a in user.assignments.select_related("role")
            if a.role.is_active and (a.branch_id is None or a.branch_id == branch.id)
        }
    )


@require_POST
def switch_branch(request):
    try:
        branch_id = int(request.POST.get("branch", ""))
    except ValueError:
        branch_id = None
    branch = next((b for b in request.branches if b.id == branch_id), None)
    if branch is None:
        messages.error(request, "You do not have access to that branch.")
    else:
        request.session[SESSION_BRANCH_KEY] = branch.id
        audit.record(request, "auth.branch_switch", branch)
        messages.success(request, f"Now working in {branch.name}.")
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
        next_url = reverse_lazy("core:dashboard")
    return redirect(next_url)
