"""Roles and the permission matrix (Settings → Roles & permissions)."""

from itertools import groupby

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.audit import services as audit

from . import services
from .decorators import permission_required
from .forms import RoleForm
from .models import Permission, Role


def _roles(request):
    return Role.objects.filter(organization=request.organization)


def _grouped_permissions():
    perms = list(Permission.objects.all())
    return [(module, list(items)) for module, items in groupby(perms, key=lambda p: p.module)]


def _crumbs(*extra):
    return [
        {"label": "Settings", "url": "/settings/"},
        {"label": "Roles & permissions", "url": "/settings/roles/"},
        *extra,
    ]


@permission_required("admin.roles.manage")
def role_list(request):
    # System roles first, then custom roles, each alphabetically.
    roles = sorted(
        _roles(request).prefetch_related("permissions"), key=lambda r: (not r.is_system, r.name)
    )
    rows = [
        {
            "role": r,
            "perm_count": r.permissions.count(),
            "member_count": r.assignments.values("user").distinct().count(),
        }
        for r in roles
    ]
    return render(request, "accounts/roles/list.html", {"rows": rows, "settings_tab": "roles"})


@permission_required("admin.roles.manage")
def role_create(request):
    return _role_form(request, Role(organization=request.organization))


@permission_required("admin.roles.manage")
def role_edit(request, pk):
    role = get_object_or_404(_roles(request), pk=pk)
    if role.is_locked:
        messages.info(request, "Super admin always has every permission and cannot be edited.")
        return redirect("accounts:role-list")
    return _role_form(request, role)


def _role_form(request, role):
    held = services.all_permissions_anywhere(request.user)
    form = RoleForm(
        request.POST or None, instance=role, organization=request.organization, grantable_codes=held
    )
    if request.method == "POST" and form.is_valid():
        before = set(role.permissions.values_list("code", flat=True)) if role.pk else set()
        with transaction.atomic():
            creating = role.pk is None
            role = form.save()
            after = set(role.permissions.values_list("code", flat=True))
            audit.record(
                request,
                "role.create" if creating else "role.update",
                role,
                changes={"added": sorted(after - before), "removed": sorted(before - after)},
            )
        messages.success(request, f"Role “{role.name}” saved.")
        return redirect("accounts:role-list")
    selected = {str(v) for v in (form["permissions"].value() or [])}
    return render(
        request,
        "accounts/roles/form.html",
        {
            "form": form,
            "role": role,
            "groups": _grouped_permissions(),
            "selected": selected,
            "held": held,
            "settings_tab": "roles",
            "breadcrumbs": _crumbs({"label": role.name if role.pk else "New role"}),
        },
    )


@require_POST
@permission_required("admin.roles.manage")
def role_toggle_active(request, pk):
    role = get_object_or_404(_roles(request), pk=pk)
    if role.is_locked:
        messages.error(request, "Super admin cannot be deactivated.")
    else:
        role.is_active = not role.is_active
        role.save(update_fields=["is_active"])
        audit.record(request, "role.activate" if role.is_active else "role.deactivate", role)
        messages.success(
            request, f"“{role.name}” is now {'active' if role.is_active else 'inactive'}."
        )
    return redirect("accounts:role-list")


@permission_required("admin.roles.manage")
def permission_matrix(request):
    """Roles × permissions grid. Saving changes several roles in one transaction."""
    roles = list(_roles(request).filter(is_active=True).prefetch_related("permissions"))
    held = services.all_permissions_anywhere(request.user)
    current = {r.pk: {p.code for p in r.permissions.all()} for r in roles}

    if request.method == "POST":
        perms = {p.code: p for p in Permission.objects.all()}
        submitted = {
            r.pk: {c for c in request.POST.getlist(f"role_{r.pk}") if c in perms} for r in roles
        }
        blocked = set()
        for r in roles:
            if r.is_locked:
                continue
            blocked |= (submitted[r.pk] ^ current[r.pk]) - held
        if blocked:
            messages.error(
                request,
                "You can only change permissions you hold yourself: " + ", ".join(sorted(blocked)),
            )
        else:
            with transaction.atomic():
                for r in roles:
                    if r.is_locked or submitted[r.pk] == current[r.pk]:
                        continue
                    r.permissions.set([perms[c] for c in submitted[r.pk]])
                    audit.record(
                        request,
                        "role.update",
                        r,
                        changes={
                            "added": sorted(submitted[r.pk] - current[r.pk]),
                            "removed": sorted(current[r.pk] - submitted[r.pk]),
                        },
                        summary="permission matrix",
                    )
            messages.success(request, "Permissions saved.")
            return redirect("accounts:permission-matrix")

    groups = [
        (
            module,
            [
                {
                    "perm": p,
                    "editable": p.code in held,
                    "cells": [{"role": r, "checked": p.code in current[r.pk]} for r in roles],
                }
                for p in perms
            ],
        )
        for module, perms in _grouped_permissions()
    ]
    return render(
        request,
        "accounts/roles/matrix.html",
        {
            "roles": roles,
            "groups": groups,
            "settings_tab": "matrix",
            "breadcrumbs": _crumbs({"label": "Permission matrix"}),
        },
    )
