"""Settings → Hospital and Branches."""

from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.security import same_origin_frame

from . import field_rules
from .forms import BranchForm, ModulesForm, OrganizationForm, PreferencesForm, PrintLayoutForm
from .models import Branch, PrintLayout, preferences_for, print_layout_for

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
def field_rules_page(request):
    """Settings → Fields & tax: hidden, optional or required, and default tax rates."""
    prefs = preferences_for(request.organization)
    if request.method == "POST":
        rules = {
            key: request.POST[f"rule:{key}"]
            for key, choices in field_rules.ALLOWED.items()
            if request.POST.get(f"rule:{key}") in choices
        }
        try:
            sale = Decimal(request.POST.get("default_sale_tax_percent") or 0)
            gst = Decimal(request.POST.get("default_purchase_gst_percent") or 0)
            if not (0 <= sale <= 100 and 0 <= gst <= 100):
                raise ValueError
        except (ArithmeticError, ValueError):
            messages.error(request, "Tax rates must be between 0 and 100.")
            return redirect("organizations:field-rules")
        changes = {}
        if rules != prefs.field_rules:
            changes["field_rules"] = [prefs.field_rules, rules]
        if sale != prefs.default_sale_tax_percent:
            changes["default_sale_tax_percent"] = [str(prefs.default_sale_tax_percent), str(sale)]
        if gst != prefs.default_purchase_gst_percent:
            changes["default_purchase_gst_percent"] = [
                str(prefs.default_purchase_gst_percent),
                str(gst),
            ]
        prefs.field_rules = rules
        prefs.default_sale_tax_percent, prefs.default_purchase_gst_percent = sale, gst
        prefs.save()
        if changes:
            audit.record(request, "organization.field_rules", request.organization, changes=changes)
        messages.success(request, "Saved.")
        return redirect("organizations:field-rules")
    groups = {}
    for key, label, group, choices in field_rules.FIELDS:
        groups.setdefault(group, []).append(
            {
                "key": key,
                "label": label,
                "value": field_rules.rule(request.organization, key),
                "choices": [(c, field_rules.LABELS[c]) for c in choices],
            }
        )
    return render(
        request,
        "organizations/field_rules.html",
        {"groups": groups.items(), "prefs": prefs, "settings_tab": "fields"},
    )


def _document(request) -> str:
    wanted = request.GET.get("doc", "")
    return wanted if wanted in PrintLayout.Document.values else PrintLayout.Document.PRESCRIPTION


@permission_required("admin.organization.manage")
def print_layouts(request):
    """Settings → Print layouts: paper, spacing, text size, logo, header and footer per document."""
    document = _document(request)
    layout = print_layout_for(request.organization, document)
    form = PrintLayoutForm(request.POST or None, request.FILES or None, instance=layout)
    if request.method == "POST" and form.is_valid():
        before = audit.snapshot(layout, form.changed_data)
        form.save()
        audit.record(
            request,
            "organization.print_layout",
            request.organization,
            changes={k: [v, str(getattr(layout, k))] for k, v in before.items()},
            summary=document,
        )
        messages.success(request, f"{layout.get_document_display()} layout saved.")
        return redirect(f"{reverse('organizations:print-layouts')}?doc={document}")
    return render(
        request,
        "organizations/print_layouts.html",
        {
            "form": form,
            "layout": layout,
            "documents": PrintLayout.Document.choices,
            "document": document,
            "settings_tab": "print",
        },
    )


class _SampleDoctor:
    """Stands in for a doctor on the prescription preview."""

    qualifications = "MBBS, FCPS"
    specialty = "Physician"
    department = "General Medicine"
    registration_number = "12345"

    def __str__(self):
        return "Dr. Sample Name"


@same_origin_frame
@permission_required("admin.organization.manage")
def print_layout_preview(request):
    """A sample document with the saved layout, shown in the editor."""
    document = _document(request)
    return render(
        request,
        "organizations/print_layout_preview.html",
        {
            "document": document,
            "L": print_layout_for(request.organization, document),
            "sample_doctor": _SampleDoctor(),
        },
    )


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


@require_POST
@permission_required("admin.organization.manage")
def modules(request):
    """Module switches on the All menu page. Unticked checkboxes post nothing, so this is off."""
    prefs = preferences_for(request.organization)
    before = audit.snapshot(prefs, ModulesForm.Meta.fields)
    form = ModulesForm(request.POST, instance=prefs)
    if form.is_valid():
        form.save()
        changes = audit.diff(before, prefs, ModulesForm.Meta.fields)
        if changes:
            audit.record(request, "organization.modules", request.organization, changes=changes)
        messages.success(request, "Modules saved.")
    return redirect("core:menu")


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
