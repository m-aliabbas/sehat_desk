"""Settings → Hospital and Branches."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit

from .forms import BranchForm, OrganizationForm, PreferencesForm
from .models import Branch, preferences_for

ORG_AUDIT_FIELDS = [f for f in OrganizationForm.Meta.fields if f != "logo"]
BRANCH_AUDIT_FIELDS = ["name", "code", "city", "phone", "address", "is_main", "is_active"]


def _crumbs(*extra):
    return [{"label": "Settings", "url": "/settings/"}, *extra]


@permission_required(
    "admin.organization.manage",
    "admin.branches.manage",
    "admin.roles.manage",
    "admin.masterdata.manage",
    "admin.audit.view",
    any_of=True,
)
def settings_home(request):
    for code, url in [
        ("admin.organization.manage", "organizations:hospital"),
        ("admin.branches.manage", "organizations:branch-list"),
        ("admin.masterdata.manage", "masterdata:department-list"),
        ("admin.roles.manage", "accounts:role-list"),
        ("admin.audit.view", "audit:log"),
    ]:
        if has_perm(request, code):
            return redirect(url)
    return redirect("core:dashboard")


@permission_required("admin.organization.manage")
def hospital_settings(request):
    org = request.organization
    before = audit.snapshot(org, ORG_AUDIT_FIELDS)
    old_logo = org.logo.name
    form = OrganizationForm(request.POST or None, request.FILES or None, instance=org)
    if request.method == "POST" and form.is_valid():
        form.save()
        changes = audit.diff(before, org, ORG_AUDIT_FIELDS)
        if org.logo.name != old_logo:
            changes["logo"] = [old_logo, org.logo.name]
        if changes:
            audit.record(request, "organization.update", org, changes=changes)
        messages.success(request, "Hospital settings saved.")
        return redirect("organizations:hospital")
    return render(
        request,
        "organizations/hospital.html",
        {"form": form, "settings_tab": "hospital", "breadcrumbs": _crumbs({"label": "Hospital"})},
    )


PREFERENCE_FIELDS = PreferencesForm.Meta.fields


@permission_required("admin.organization.manage")
def preferences(request):
    """Settings → Preferences: module switches and hospital-wide defaults."""
    prefs = preferences_for(request.organization)
    before = audit.snapshot(prefs, PREFERENCE_FIELDS)
    form = PreferencesForm(request.POST or None, instance=prefs)
    if request.method == "POST" and form.is_valid():
        form.save()
        changes = audit.diff(before, prefs, PREFERENCE_FIELDS)
        if changes:
            audit.record(request, "organization.preferences", request.organization, changes=changes)
        messages.success(request, "Preferences saved.")
        return redirect("organizations:preferences")
    return render(
        request,
        "organizations/preferences.html",
        {
            "form": form,
            "settings_tab": "preferences",
            "breadcrumbs": _crumbs({"label": "Preferences"}),
        },
    )


def _branches(request):
    return Branch.objects.filter(organization=request.organization)


@permission_required("admin.branches.manage")
def branch_list(request):
    return render(
        request,
        "organizations/branch_list.html",
        {"branches": _branches(request), "settings_tab": "branches"},
    )


@permission_required("admin.branches.manage")
def branch_create(request):
    return _branch_form(request, Branch(organization=request.organization))


@permission_required("admin.branches.manage")
def branch_edit(request, pk):
    return _branch_form(request, get_object_or_404(_branches(request), pk=pk))


def _branch_form(request, branch):
    creating = branch.pk is None
    before = {} if creating else audit.snapshot(branch, BRANCH_AUDIT_FIELDS)
    form = BranchForm(request.POST or None, instance=branch, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        branch = form.save()
        if branch.is_main:
            _branches(request).exclude(pk=branch.pk).update(is_main=False)
        audit.record(
            request,
            "branch.create" if creating else "branch.update",
            branch,
            changes=audit.snapshot(branch, BRANCH_AUDIT_FIELDS)
            if creating
            else audit.diff(before, branch, BRANCH_AUDIT_FIELDS),
        )
        messages.success(request, f"Branch “{branch.name}” saved.")
        return redirect("organizations:branch-list")
    return render(
        request,
        "organizations/branch_form.html",
        {
            "form": form,
            "branch": branch,
            "settings_tab": "branches",
            "breadcrumbs": _crumbs(
                {"label": "Branches", "url": "/settings/branches/"},
                {"label": branch.name if not creating else "New branch"},
            ),
        },
    )


@require_POST
@permission_required("admin.branches.manage")
def branch_toggle_active(request, pk):
    branch = get_object_or_404(_branches(request), pk=pk)
    if branch.is_active and branch.is_main:
        messages.error(
            request, "The main branch cannot be deactivated. Make another branch main first."
        )
    elif branch.is_active and branch == request.branch and len(request.branches) <= 1:
        messages.error(request, "You cannot deactivate the only branch you can work in.")
    else:
        branch.is_active = not branch.is_active
        branch.save(update_fields=["is_active", "updated_at"])
        audit.record(
            request, "branch.activate" if branch.is_active else "branch.deactivate", branch
        )
        messages.success(
            request, f"“{branch.name}” is now {'active' if branch.is_active else 'inactive'}."
        )
    return redirect("organizations:branch-list")
