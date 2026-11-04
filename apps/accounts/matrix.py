"""Access matrix: who can view, edit and delete each area (Settings → Access → Access matrix).

Each cell is an existing permission code, so the matrix is another way to set the same codes the
role form and the permission matrix set. Roles act as departments (Pharmacy, Lab, Reception…).
A person can then be given a code their roles lack ("+", `User.extra_permissions`) or have one
their roles give taken away ("–", `User.denied_permissions`). Nobody changes a code they do not
hold themselves, and Super admin (role or person) is never changed.
"""

from dataclasses import dataclass

from django.db import transaction

from apps.audit import services as audit

from . import services
from .models import Permission, Role

COLUMNS = ("view", "edit", "delete")


@dataclass(frozen=True)
class Area:
    group: str
    label: str
    view: str | None = None
    edit: str | None = None
    delete: str | None = None

    def code(self, column: str) -> str | None:
        return getattr(self, column)


AREAS: tuple[Area, ...] = (
    Area("Pharmacy", "Products and stock", "pharmacy.sale", "pharmacy.inventory.manage",
         "pharmacy.product.delete"),
    Area("Pharmacy", "Product categories and units", None, "pharmacy.inventory.manage",
         "pharmacy.setup.delete"),
    Area("Pharmacy", "Suppliers and purchases", None, "pharmacy.inventory.manage",
         "pharmacy.vendor.delete"),
    Area("Pharmacy", "Sales returns and refunds", "pharmacy.sale", "pharmacy.refund"),
    Area("Pharmacy", "Discounts at the till", None, "pharmacy.discount"),
    Area("Pharmacy", "Stock edits (approve)", None, "admin.approvals.manage"),
    Area("Laboratory", "Lab tests and panels", "lab.order.create", "lab.catalog.manage",
         "lab.catalog.delete"),
    Area("Laboratory", "Lab results", "lab.sample.collect", "lab.result.enter"),
    Area("Laboratory", "Lab reports (verify and release)", None, "lab.result.verify"),
    Area("Patients", "Patients", "patient.view", "patient.update"),
    Area("Patients", "Patient documents", "document.view", "document.upload", "document.archive"),
    Area("Billing", "Bills and payments", "billing.view", "billing.payment.collect"),
    Area("Billing", "Bill refunds", "billing.view", "billing.refund"),
    Area("Billing", "Payment methods", None, "admin.organization.manage", "billing.method.delete"),
    Area("Inpatient", "Wards, rooms and beds", "ipd.manage", "ipd.facility.manage",
         "ipd.facility.delete"),
    Area("Setup", "Departments and specialties", None, "admin.masterdata.manage",
         "masterdata.department.delete"),
    Area("Setup", "Services and categories", None, "admin.masterdata.manage",
         "masterdata.service.delete"),
    Area("Setup", "Medicine list", None, "admin.masterdata.manage", "masterdata.medicine.delete"),
    Area("Setup", "Diagnosis codes", None, "admin.masterdata.manage",
         "masterdata.diagnosis.delete"),
    Area("Setup", "Reception counters", "appointment.view", "queue.manage",
         "queue.counter.delete"),
    Area("Finance", "Expenses and categories", "finance.view", "finance.expense.manage",
         "finance.category.delete"),
    Area("Reports", "Operational reports", "reports.view"),
    Area("Reports", "Financial reports", "reports.financial.view"),
    Area("Reports", "Audit log", "admin.audit.view"),
    Area("Records", "Locked fields (full edit)", None, "records.edit_full"),
    Area("Records", "Recycle bin (restore, erase)", "admin.recyclebin.manage"),
)  # fmt: skip

CODES = frozenset(a.code(c) for a in AREAS for c in COLUMNS if a.code(c))


def _labels() -> dict[str, str]:
    return dict(Permission.objects.filter(code__in=CODES).values_list("code", "label"))


def _shared() -> dict[str, list[str]]:
    """For each code, the other cells it also switches (one code can sit in several rows)."""
    where: dict[str, list[str]] = {}
    for a in AREAS:
        for c in COLUMNS:
            if code := a.code(c):
                where.setdefault(code, []).append(a.label)
    return where


def grid(on: frozenset, held: frozenset, *, base: frozenset | None = None) -> list:
    """Rows grouped by area group. `on` is what the role or person has. With `base` (what a
    person's roles give), each cell also says whether it is a "+" or "–" override."""
    labels, shared = _labels(), _shared()
    groups: list[tuple[str, list]] = []
    for a in AREAS:
        cells = []
        for c in COLUMNS:
            code = a.code(c)
            if not code:
                cells.append(None)
                continue
            others = [x for x in shared[code] if x != a.label]
            cell = {
                "code": code,
                "on": code in on,
                "can": code in held,
                "title": labels.get(code, code)
                + (f" (same switch as {', '.join(others)})" if others else ""),
            }
            if base is not None:
                cell["mark"] = (
                    "+" if code in on and code not in base
                    else "–" if code not in on and code in base
                    else ""
                )  # fmt: skip
            cells.append(cell)
        if not groups or groups[-1][0] != a.group:
            groups.append((a.group, []))
        groups[-1][1].append({"area": a, "cells": cells})
    return groups


def _check(held, before, after):
    blocked = (before ^ after) - held
    if blocked:
        raise services.PermissionChangeError(
            "You can only change permissions you hold yourself: " + ", ".join(sorted(blocked))
        )


@transaction.atomic
def save_role(request, role: Role, ticked) -> tuple[set, set]:
    """Set the role's matrix codes to the ticked ones; codes outside the matrix are kept."""
    if role.is_locked:
        raise services.PermissionChangeError("Super admin always has everything.")
    held = services.all_permissions_anywhere(request.user)
    current = services.role_codes(role)
    before = current & CODES
    after = {c for c in ticked if c in CODES}
    _check(held, before, after)
    added, removed = after - before, before - after
    if added:
        role.permissions.add(*Permission.objects.filter(code__in=added))
    if removed:
        role.permissions.remove(*Permission.objects.filter(code__in=removed))
    if added or removed:
        audit.record(
            request, "role.update", role,
            changes={"added": sorted(added), "removed": sorted(removed)}, summary="access matrix",
        )  # fmt: skip
    return added, removed


def person_base(user) -> frozenset:
    """What the person's roles give, in any branch."""
    role_ids = services.active_assignments(user).values("role_id")
    return frozenset(
        Permission.objects.filter(roles__in=role_ids).values_list("code", flat=True).distinct()
    )


def person_on(user) -> frozenset:
    return services.all_permissions_anywhere(user) & CODES


@transaction.atomic
def save_person(request, user, ticked) -> tuple[set, set]:
    """Ticked codes the roles lack become "+", unticked codes the roles give become "–"."""
    if services.is_super_admin(user):
        raise services.PermissionChangeError("A super admin always has everything.")
    held = services.all_permissions_anywhere(request.user)
    before = person_on(user)
    after = {c for c in ticked if c in CODES}
    _check(held, before, after)
    base = person_base(user) & CODES
    extra = {c for c in after if c not in base}
    denied = {c for c in base if c not in after}
    perms = {p.code: p for p in Permission.objects.filter(code__in=CODES)}
    user.extra_permissions.remove(*[p for c, p in perms.items() if c not in extra])
    user.extra_permissions.add(*[perms[c] for c in extra])
    user.denied_permissions.remove(*[p for c, p in perms.items() if c not in denied])
    user.denied_permissions.add(*[perms[c] for c in denied])
    added, removed = after - before, before - after
    if added or removed:
        audit.record(
            request, "staff.access_matrix", user,
            changes={"added": sorted(added), "removed": sorted(removed)},
        )  # fmt: skip
    return added, removed
