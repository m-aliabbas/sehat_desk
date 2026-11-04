"""What can be edited and deleted to the Recycle bin, who may do it, and what stops it.

Each kind's model uses `core.models.RecycleBinMixin`: `objects` hides deleted rows everywhere,
`all_objects` still sees them, and old records keep pointing at them. A kind lists the module
permissions (any one) that allow editing it; deleting also needs `records.delete`. A `blocker`
explains why a record cannot go yet (stock on the shelf, money owed, other records using it).
`edit_fields` are what the shared edit page (`recyclebin:edit`) offers when the kind has no edit
page of its own (`edit_url`).
"""

from dataclasses import dataclass
from typing import Callable

from django.apps import apps
from django.db.models import Sum
from django.urls import reverse


@dataclass(frozen=True)
class Kind:
    key: str
    model: str  # "app_label.Model"
    label: str
    manage_perm: str | tuple[str, ...]
    list_url: str
    blocker: Callable | None = None
    detail_url: str | None = None
    edit_url: str | None = None  # the kind's own edit page, else the shared one
    edit_fields: tuple[str, ...] = ()

    @property
    def manage_perms(self) -> tuple[str, ...]:
        return (self.manage_perm,) if isinstance(self.manage_perm, str) else self.manage_perm

    def edit_link(self, obj) -> str | None:
        if self.edit_url:
            return reverse(self.edit_url, args=[obj.pk])
        if self.edit_fields:
            return reverse("recyclebin:edit", args=[self.key, obj.pk])
        return None

    @property
    def model_class(self):
        return apps.get_model(self.model)

    def url_for(self, obj) -> str:
        if self.detail_url:
            return reverse(self.detail_url, args=[obj.pk])
        return reverse(self.list_url)


def _product_blocker(product):
    from apps.pharmacy.models import InventoryBatch, PurchaseItem

    left = (
        InventoryBatch.objects.filter(product=product).aggregate(n=Sum("quantity_available"))["n"]
        or 0
    )
    if left:
        return f"{left} still in stock. Write it off or return it to the supplier first."
    if PurchaseItem.objects.filter(
        product=product, purchase__status__in=("draft", "ordered")
    ).exists():
        return "It is on an open purchase. Receive or cancel that purchase first."
    return None


def _category_blocker(category):
    used = category.products.count()
    return f"{used} product{'s' if used != 1 else ''} use it. Move them first." if used else None


def _vendor_blocker(vendor):
    from apps.pharmacy.models import Purchase

    owed = vendor.ledger.aggregate(n=Sum("amount"))["n"] or 0
    if owed:
        return f"The account is not settled ({owed:,.2f}). Settle it first."
    if vendor.purchases.filter(status__in=Purchase.EDITABLE).exists():
        return "It has an open purchase. Receive or cancel it first."
    return None


def _department_blocker(department):
    from apps.masterdata.models import Service

    doctors = department.doctors.filter(is_active=True).count()
    services = Service.objects.filter(department=department, is_active=True).count()
    if doctors or services:
        return f"{doctors} active doctor(s) and {services} service(s) are in it. Move them first."
    return None


def _used_by(count: int, what: str) -> str | None:
    if not count:
        return None
    return f"{count} {what}{'s' if count != 1 else ''} use it. Move them first."


def _specialty_blocker(specialty):
    return _used_by(specialty.doctors.count(), "doctor")


def _service_category_blocker(category):
    return _used_by(category.services.count(), "service")


def _ward_blocker(ward):
    return _used_by(ward.rooms.count(), "room")


def _room_blocker(room):
    return _used_by(room.beds.count(), "bed")


def _bed_blocker(bed):
    from apps.ipd.models import Bed

    if bed.status == Bed.Status.OCCUPIED:
        return "A patient is in this bed. Discharge or transfer them first."
    return None


def _lab_category_blocker(category):
    return _used_by(category.tests.count(), "lab test")


def _lab_test_blocker(test):
    return _used_by(test.panels.count(), "panel")


def _medicine_blocker(medicine):
    return _used_by(medicine.products.count(), "pharmacy product")


def _payment_method_blocker(method):
    if method.code == "cash":
        return "Cash is always needed at the till and the desk."
    return None


KINDS = {
    k.key: k
    for k in (
        Kind(
            "product",
            "pharmacy.Product",
            "Product",
            "pharmacy.inventory.manage",
            "pharmacy:product-list",
            _product_blocker,
            "pharmacy:product-detail",
            edit_url="pharmacy:product-edit",
        ),
        Kind(
            "product_category",
            "pharmacy.ProductCategory",
            "Product category",
            "pharmacy.inventory.manage",
            "pharmacy:categories",
            _category_blocker,
            edit_fields=("name", "default_tax_percent", "requires_prescription", "tax_note"),
        ),
        Kind(
            "uom",
            "pharmacy.UnitOfMeasure",
            "Unit of measure",
            "pharmacy.inventory.manage",
            "pharmacy:uom",
            edit_fields=("name",),
        ),
        Kind(
            "vendor",
            "pharmacy.Vendor",
            "Supplier",
            "pharmacy.inventory.manage",
            "pharmacy:vendor-list",
            _vendor_blocker,
            "pharmacy:vendor-detail",
            edit_url="pharmacy:vendor-edit",
        ),
        Kind(
            "department",
            "masterdata.Department",
            "Department",
            "admin.masterdata.manage",
            "masterdata:department-list",
            _department_blocker,
            edit_url="masterdata:department-edit",
        ),
        Kind(
            "service",
            "masterdata.Service",
            "Service",
            "admin.masterdata.manage",
            "masterdata:service-list",
            edit_url="masterdata:service-edit",
        ),
        Kind(
            "specialty",
            "masterdata.Specialty",
            "Specialty",
            "admin.masterdata.manage",
            "masterdata:department-list",
            _specialty_blocker,
            edit_fields=("name",),
        ),
        Kind(
            "service_category",
            "masterdata.ServiceCategory",
            "Service category",
            "admin.masterdata.manage",
            "masterdata:service-list",
            _service_category_blocker,
            edit_fields=("name",),
        ),
        Kind(
            "expense_category",
            "finance.ExpenseCategory",
            "Expense category",
            "finance.expense.manage",
            "finance:category-list",
            edit_fields=("name", "segment"),
        ),
        Kind(
            "payment_method",
            "billing.PaymentMethod",
            "Payment method",
            "admin.organization.manage",
            "billing:settings",
            _payment_method_blocker,
            edit_fields=("name", "requires_reference", "sort_order"),
        ),
        Kind(
            "ward",
            "ipd.Ward",
            "Ward",
            "ipd.facility.manage",
            "ipd:setup",
            _ward_blocker,
            edit_url="ipd:setup-ward",
        ),
        Kind(
            "room",
            "ipd.Room",
            "Room",
            "ipd.facility.manage",
            "ipd:setup",
            _room_blocker,
            edit_url="ipd:setup-room",
        ),
        Kind(
            "bed",
            "ipd.Bed",
            "Bed",
            "ipd.facility.manage",
            "ipd:setup",
            _bed_blocker,
            edit_fields=("room", "code"),
        ),
        Kind(
            "lab_category",
            "lab.LabTestCategory",
            "Lab test category",
            "lab.catalog.manage",
            "lab:catalog",
            _lab_category_blocker,
            edit_fields=("name", "sort_order"),
        ),
        Kind(
            "lab_test",
            "lab.LabTest",
            "Lab test",
            "lab.catalog.manage",
            "lab:catalog",
            _lab_test_blocker,
            edit_url="lab:test-edit",
        ),
        Kind(
            "lab_panel",
            "lab.LabPanel",
            "Lab panel",
            "lab.catalog.manage",
            "lab:catalog",
            edit_url="lab:panel-edit",
        ),
        Kind(
            "diagnosis_code",
            "encounters.DiagnosisCode",
            "Diagnosis code",
            "admin.masterdata.manage",
            "encounters:diagnosis-code-list",
            edit_fields=("code", "title"),
        ),
        Kind(
            "medicine",
            "prescriptions.Medicine",
            "Medicine",
            ("admin.masterdata.manage", "pharmacy.inventory.manage"),
            "prescriptions:medicine-list",
            _medicine_blocker,
            edit_url="prescriptions:medicine-edit",
        ),
        Kind(
            "counter",
            "queue.Counter",
            "Reception counter",
            "queue.manage",
            "queue:counters",
            edit_fields=("code", "name", "assigned_staff"),
        ),
    )
}


def kind_of(obj) -> Kind | None:
    label = obj._meta.label
    return next((k for k in KINDS.values() if k.model == label), None)
