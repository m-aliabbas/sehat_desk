"""Type a new entry into a dropdown and it is saved there, with no trip to another page.

`core.forms.StyledFormMixin` marks any dropdown of one of these models with data-create-kind, and
app.js offers "+ Add" to people holding the permission (`allowed_kinds`, read from the page).
"""

import re
from dataclasses import dataclass, field

from django.apps import apps
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit


@dataclass(frozen=True)
class Kind:
    model: str
    permission: str
    audit_action: str
    extra: dict = field(default_factory=dict)


KINDS = {
    "department": Kind("masterdata.Department", "admin.masterdata.manage", "department.create"),
    "specialty": Kind("masterdata.Specialty", "admin.masterdata.manage", "specialty.create"),
    "service-category": Kind(
        "masterdata.ServiceCategory", "admin.masterdata.manage", "service_category.create"
    ),
    "lab-category": Kind("lab.LabTestCategory", "lab.catalog.manage", "lab.category.create"),
    "product-category": Kind(
        "pharmacy.ProductCategory", "pharmacy.inventory.manage", "product_category.create"
    ),
    "expense-category": Kind(
        "finance.ExpenseCategory",
        "finance.expense.manage",
        "expense.category_create",
        {"segment": "hospital"},
    ),
}


def allowed_kinds(perms) -> str:
    return " ".join(key for key, kind in KINDS.items() if kind.permission in perms)


def _department_code(organization, name) -> str:
    """Letters from the name, numbered if another department already has them."""
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:6] or "DEPT"
    model = apps.get_model("masterdata.Department")
    code, n = base, 1
    while model.objects.filter(organization=organization, code=code).exists():
        n += 1
        code = f"{base}{n}"
    return code


@require_POST
@permission_required(*sorted({k.permission for k in KINDS.values()}), any_of=True)
def quick_add(request, kind):
    spec = KINDS.get(kind)
    if spec is None or spec.permission not in request.sd_perms:
        return JsonResponse({"error": "You are not allowed to add this."}, status=403)
    model = apps.get_model(spec.model)
    limit = model._meta.get_field("name").max_length
    name = " ".join(request.POST.get("name", "").split())
    if not 2 <= len(name) <= limit:
        return JsonResponse({"error": f"Use 2 to {limit} characters."}, status=400)
    with transaction.atomic():
        row = model.objects.filter(organization=request.organization, name__iexact=name).first()
        if row:
            # Typed again: bring back one that was switched off, never make a second.
            if getattr(row, "is_active", True) is False:
                row.is_active = True
                row.save(update_fields=["is_active", "updated_at"])
                audit.record(request, f"{spec.audit_action.rsplit('.', 1)[0]}.activate", row)
        else:
            values = dict(spec.extra)
            if kind == "department":
                values["code"] = _department_code(request.organization, name)
            row = model.objects.create(organization=request.organization, name=name, **values)
            audit.record(request, spec.audit_action, row, summary=row.name)
    return JsonResponse({"id": row.pk, "name": str(row)})
