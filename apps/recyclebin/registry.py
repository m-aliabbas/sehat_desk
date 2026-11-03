"""What can be deleted to the Recycle bin, who may do it, and what stops it.

Each kind's model uses `core.models.RecycleBinMixin`: `objects` hides deleted rows everywhere,
`all_objects` still sees them, and old records keep pointing at them. A kind lists the module
permission needed besides `records.delete`, and a `blocker` that explains why a record cannot
go yet (stock still on the shelf, money still owed, other records still using it).
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
    manage_perm: str
    list_url: str
    blocker: Callable | None = None
    detail_url: str | None = None

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
        ),
        Kind(
            "product_category",
            "pharmacy.ProductCategory",
            "Product category",
            "pharmacy.inventory.manage",
            "pharmacy:categories",
            _category_blocker,
        ),
        Kind(
            "uom",
            "pharmacy.UnitOfMeasure",
            "Unit of measure",
            "pharmacy.inventory.manage",
            "pharmacy:uom",
        ),
        Kind(
            "vendor",
            "pharmacy.Vendor",
            "Supplier",
            "pharmacy.inventory.manage",
            "pharmacy:vendor-list",
            _vendor_blocker,
            "pharmacy:vendor-detail",
        ),
        Kind(
            "department",
            "masterdata.Department",
            "Department",
            "admin.masterdata.manage",
            "masterdata:department-list",
            _department_blocker,
        ),
        Kind(
            "service",
            "masterdata.Service",
            "Service",
            "admin.masterdata.manage",
            "masterdata:service-list",
        ),
    )
}


def kind_of(obj) -> Kind | None:
    label = obj._meta.label
    return next((k for k in KINDS.values() if k.model == label), None)
