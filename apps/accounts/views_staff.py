"""Staff directory and account management. Staff are organization-level records."""

from axes.utils import reset as axes_reset
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.audit import services as audit
from apps.organizations.models import Branch

from . import services
from .decorators import has_perm, permission_required
from .forms import AssignmentForm, ResetPasswordForm, StaffCreateForm, StaffForm
from .models import Role, RoleAssignment, User

STAFF_AUDIT_FIELDS = [
    "first_name",
    "last_name",
    "username",
    "email",
    "phone",
    "employee_code",
    "designation",
]


def _staff_qs(request):
    return User.objects.filter(organization=request.organization)


def _get_staff(request, pk):
    # Another organization's user is a 404, not a 403.
    return get_object_or_404(_staff_qs(request), pk=pk)


def _assignable_roles(request):
    """Active roles in this org whose permissions the actor fully holds (no escalation)."""
    held = services.all_permissions_anywhere(request.user)
    roles = Role.objects.filter(organization=request.organization, is_active=True).prefetch_related(
        "permissions"
    )
    allowed = [r.pk for r in roles if {p.code for p in r.permissions.all()} <= held]
    return Role.objects.filter(pk__in=allowed)


def _crumbs(*extra):
    return [{"label": "Staff", "url": "/staff/"}, *extra]


@require_GET
@permission_required("admin.users.view")
def staff_list(request):
    qs = _staff_qs(request).prefetch_related("assignments__role", "assignments__branch")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    branch_id = request.GET.get("branch", "")
    role_id = request.GET.get("role", "")
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(username__icontains=q)
            | Q(employee_code__icontains=q)
            | Q(designation__icontains=q)
        )
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    if branch_id.isdigit():
        qs = qs.filter(
            Q(assignments__branch_id=branch_id) | Q(assignments__branch__isnull=True)
        ).distinct()
    if role_id.isdigit():
        qs = qs.filter(assignments__role_id=role_id).distinct()

    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    ctx = {
        "page_obj": page,
        "q": q,
        "status": status,
        "branch_id": branch_id,
        "role_id": role_id,
        "branches": Branch.objects.filter(organization=request.organization),
        "roles": Role.objects.filter(organization=request.organization),
        "can_manage": has_perm(request, "admin.users.manage"),
    }
    template = "accounts/staff/_table.html" if request.htmx else "accounts/staff/list.html"
    return render(request, template, ctx)


@require_GET
@permission_required("admin.users.view")
def staff_detail(request, pk):
    staff = _get_staff(request, pk)
    can_manage = has_perm(request, "admin.users.manage")
    ctx = {
        "staff": staff,
        "assignments": staff.assignments.select_related("role", "branch"),
        "can_manage": can_manage,
        "breadcrumbs": _crumbs({"label": str(staff)}),
        "is_self": staff == request.user,
    }
    if can_manage:
        ctx["assignment_form"] = AssignmentForm(
            user=staff,
            organization=request.organization,
            assignable_roles=_assignable_roles(request),
        )
        ctx["reset_form"] = ResetPasswordForm(user=staff)
    return render(request, "accounts/staff/detail.html", ctx)


@permission_required("admin.users.manage")
def staff_create(request):
    form = StaffCreateForm(
        request.POST or None,
        organization=request.organization,
        assignable_roles=_assignable_roles(request),
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save(commit=False)
            user.organization = request.organization
            user.set_password(form.cleaned_data["password1"])
            user.must_change_password = True
            user.save()
            RoleAssignment.objects.create(
                user=user,
                role=form.cleaned_data["role"],
                branch=form.cleaned_data["branch"],
                created_by=request.user,
            )
            audit.record(
                request,
                "staff.create",
                user,
                summary=f"role={form.cleaned_data['role'].name}",
                changes=audit.snapshot(user, STAFF_AUDIT_FIELDS),
            )
        messages.success(
            request, f"{user} can now sign in and will be asked to set a new password."
        )
        return redirect("accounts:staff-detail", pk=user.pk)
    return render(
        request,
        "accounts/staff/form.html",
        {"form": form, "breadcrumbs": _crumbs({"label": "Add staff"}), "creating": True},
    )


@permission_required("admin.users.manage")
def staff_edit(request, pk):
    staff = _get_staff(request, pk)
    before = audit.snapshot(staff, STAFF_AUDIT_FIELDS)
    form = StaffForm(request.POST or None, instance=staff, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        form.save()
        changes = audit.diff(before, staff, STAFF_AUDIT_FIELDS)
        if changes:
            audit.record(request, "staff.update", staff, changes=changes)
        messages.success(request, "Staff details saved.")
        return redirect("accounts:staff-detail", pk=staff.pk)
    return render(
        request,
        "accounts/staff/form.html",
        {
            "form": form,
            "staff": staff,
            "breadcrumbs": _crumbs(
                {"label": str(staff), "url": f"/staff/{staff.pk}/"}, {"label": "Edit"}
            ),
        },
    )


@require_POST
@permission_required("admin.users.manage")
def staff_toggle_active(request, pk):
    staff = _get_staff(request, pk)
    if staff == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("accounts:staff-detail", pk=pk)
    if (
        staff.is_active
        and services.is_super_admin(staff)
        and services.super_admin_count(request.organization) <= 1
    ):
        messages.error(
            request, "This is the last active Super admin. Add another before deactivating."
        )
        return redirect("accounts:staff-detail", pk=pk)
    if services.is_super_admin(staff) and not services.is_super_admin(request.user):
        messages.error(request, "Only a Super admin can change another Super admin's status.")
        return redirect("accounts:staff-detail", pk=pk)
    staff.is_active = not staff.is_active
    staff.save(update_fields=["is_active"])
    action = "staff.activate" if staff.is_active else "staff.deactivate"
    audit.record(request, action, staff)
    messages.success(request, f"{staff} is now {'active' if staff.is_active else 'inactive'}.")
    return redirect("accounts:staff-detail", pk=pk)


@require_POST
@permission_required("admin.users.manage")
def staff_reset_password(request, pk):
    staff = _get_staff(request, pk)
    if services.is_super_admin(staff) and not services.is_super_admin(request.user):
        messages.error(request, "Only a Super admin can reset a Super admin's password.")
        return redirect("accounts:staff-detail", pk=pk)
    form = ResetPasswordForm(request.POST, user=staff)
    if form.is_valid():
        staff.set_password(form.cleaned_data["password1"])
        staff.must_change_password = staff != request.user
        staff.save(update_fields=["password", "must_change_password"])
        axes_reset(username=staff.username)
        audit.record(request, "staff.password_reset", staff)
        messages.success(request, f"Password reset. {staff} must choose a new one at next sign-in.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("accounts:staff-detail", pk=pk)


@require_POST
@permission_required("admin.users.manage")
def staff_unlock(request, pk):
    staff = _get_staff(request, pk)
    cleared = axes_reset(username=staff.username)
    audit.record(request, "staff.unlock", staff, summary=f"cleared={cleared}")
    messages.success(request, f"Sign-in lockout cleared for {staff}.")
    return redirect("accounts:staff-detail", pk=pk)


@require_POST
@permission_required("admin.users.manage")
def assignment_add(request, pk):
    staff = _get_staff(request, pk)
    form = AssignmentForm(
        request.POST,
        user=staff,
        organization=request.organization,
        assignable_roles=_assignable_roles(request),
    )
    if form.is_valid():
        a = RoleAssignment.objects.create(
            user=staff,
            role=form.cleaned_data["role"],
            branch=form.cleaned_data["branch"],
            created_by=request.user,
        )
        audit.record(
            request,
            "staff.role_assign",
            staff,
            summary=f"{a.role.name} @ {a.branch or 'All branches'}",
        )
        messages.success(request, f"Added {a.role.name} ({a.branch or 'All branches'}).")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("accounts:staff-detail", pk=pk)


@require_POST
@permission_required("admin.users.manage")
def assignment_remove(request, pk, assignment_pk):
    staff = _get_staff(request, pk)
    a = get_object_or_404(
        RoleAssignment.objects.select_related("role", "branch"), pk=assignment_pk, user=staff
    )
    held = services.all_permissions_anywhere(request.user)
    if not services.can_grant_role(held, a.role):
        messages.error(request, "You cannot remove a role with permissions you do not hold.")
        return redirect("accounts:staff-detail", pk=pk)
    if (
        a.role.is_locked
        and a.branch is None
        and staff.is_active
        and services.super_admin_count(request.organization) <= 1
    ):
        messages.error(request, "This is the last Super admin. Assign another before removing it.")
        return redirect("accounts:staff-detail", pk=pk)
    label = f"{a.role.name} @ {a.branch or 'All branches'}"
    a.delete()
    audit.record(request, "staff.role_remove", staff, summary=label)
    messages.success(request, f"Removed {label}.")
    return redirect("accounts:staff-detail", pk=pk)
