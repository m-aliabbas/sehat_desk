from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.pharmacy import purchasing, stock
from apps.pharmacy.models import (
    InventoryBatch,
    Product,
    Purchase,
    StockTransaction,
    Vendor,
    VendorLedgerEntry,
)

D = Decimal
TODAY = timezone.localdate()
Status = Purchase.Status


def _req(rf, user, org, branch):
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    return req


@pytest.fixture
def pharmacist(make_user):
    return make_user("pharmacy", username="pharma")


@pytest.fixture
def req(rf, pharmacist, org, branch):
    return _req(rf, pharmacist, org, branch)


@pytest.fixture
def vendor(org):
    return Vendor.objects.create(organization=org, name="Sehat Medical Supplies")


@pytest.fixture
def ors(org):
    return Product.objects.create(
        organization=org, sku="P-1", brand_name="ORS", unit="sachet", sale_price=D("50"),
        purchase_price=D("30"),
    )  # fmt: skip


@pytest.fixture
def draft(req, vendor, ors):
    purchase = purchasing.create_purchase(request=req, vendor=vendor)
    purchasing.add_item(purchase, request=req, product=ors, quantity=100, unit_cost=D("35"),
                        free_quantity=10)  # fmt: skip
    return purchase


def test_purchase_totals_and_receipt_requirements(req, draft):
    draft.refresh_from_db()
    assert draft.number == "PO-00001" and draft.grand_total == D("3500.00")
    purchasing.set_invoice_details(
        draft, request=req, supplier_invoice_number="", supplier_invoice_date=None,
        discount=D("100"), tax_amount=D("50"),
    )  # fmt: skip
    draft.refresh_from_db()
    assert draft.grand_total == D("3450.00")
    problems = purchasing.receipt_problems(draft)
    assert any("invoice number" in p for p in problems)
    assert any("batch number" in p for p in problems)
    with pytest.raises(purchasing.PurchaseError):
        purchasing.receive(draft, request=req)
    assert not InventoryBatch.objects.exists()  # an unreceived purchase is never stock


def test_receive_posts_stock_and_supplier_ledger(req, draft, vendor, ors):
    purchasing.mark_ordered(draft, request=req)
    item = draft.items.get()
    purchasing.update_receipt(
        item, request=req, batch_number="or-187", expiry_date=TODAY + timedelta(days=400),
        sale_price="55",
    )  # fmt: skip
    purchasing.set_invoice_details(
        draft, request=req, supplier_invoice_number="SMS-9921", supplier_invoice_date=TODAY,
        discount=0, tax_amount=0,
    )  # fmt: skip
    purchase = purchasing.receive(draft, request=req)
    assert purchase.status == Status.RECEIVED
    batch = InventoryBatch.objects.get()
    # 100 bought + 10 free; the free units lower the shelf cost: 3500 / 110.
    assert (batch.batch_number, batch.quantity_available) == ("OR-187", 110)
    assert batch.purchase_cost == D("31.82") and batch.sale_price == D("55")
    txn = StockTransaction.objects.get()
    assert txn.kind == "purchase" and "SMS-9921" in txn.reference
    ors.refresh_from_db()
    assert ors.purchase_price == D("31.82")
    assert vendor.balance == D("3500.00")
    with pytest.raises(purchasing.PurchaseError, match="received"):
        purchasing.add_item(purchase, request=req, product=ors, quantity=1, unit_cost=1)


def test_purchase_return_reduces_stock_and_balance(req, draft, vendor, ors):
    item = draft.items.get()
    purchasing.update_receipt(item, request=req, batch_number="B1",
                              expiry_date=TODAY + timedelta(days=300))  # fmt: skip
    draft.supplier_invoice_number = "INV-1"
    draft.save()
    purchasing.receive(draft, request=req)
    with pytest.raises(purchasing.PurchaseError, match="reason"):
        purchasing.return_items(draft, request=req, quantities={item.pk: 5}, reason="")
    with pytest.raises(purchasing.PurchaseError, match="Only 110"):
        purchasing.return_items(draft, request=req, quantities={item.pk: 111}, reason="x")
    ret = purchasing.return_items(
        draft, request=req, quantities={item.pk: 11}, reason="Damaged", reference="DN-4"
    )
    assert ret.number == "PRN-00001" and ret.amount == D("350.02")  # 11 × 31.82
    assert stock.on_hand(req.branch, ors) == 99
    assert vendor.balance == D("3149.98")
    assert list(vendor.ledger.values_list("kind", flat=True)) == ["purchase", "purchase_return"]


def test_cancel_and_opening_balance(req, draft, vendor):
    with pytest.raises(purchasing.PurchaseError, match="reason"):
        purchasing.cancel(draft, request=req, reason="")
    purchasing.cancel(draft, request=req, reason="Supplier could not deliver")
    draft.refresh_from_db()
    assert draft.status == Status.CANCELLED
    purchasing.post_opening_balance(vendor, request=req, amount="1200")
    with pytest.raises(purchasing.PurchaseError, match="already"):
        purchasing.post_opening_balance(vendor, request=req, amount="5")
    assert vendor.balance == D("1200.00")
    assert VendorLedgerEntry.objects.get().kind == "opening"


def test_cashier_cannot_purchase(rf, make_user, org, branch, vendor):
    cashier = _req(rf, make_user("cashier"), org, branch)
    with pytest.raises(purchasing.PurchaseError, match="not allowed"):
        purchasing.create_purchase(request=cashier, vendor=vendor)


def test_purchase_belongs_to_branch(req, rf, pharmacist, org, branch2, draft, ors):
    other = _req(rf, pharmacist, org, branch2)
    with pytest.raises(purchasing.PurchaseError, match="another branch"):
        purchasing.add_item(draft, request=other, product=ors, quantity=1, unit_cost=1)


# --- Views -------------------------------------------------------------------------------------


def test_purchasing_screens(client, pharmacist, ors):
    client.force_login(pharmacist)
    resp = client.post(
        reverse("pharmacy:vendor-create"),
        {"name": "Ali Pharma", "payment_terms_days": "30", "opening_balance": "500"},
    )
    vendor = Vendor.objects.get()
    assert resp.status_code == 302 and vendor.balance == D("500.00")
    resp = client.post(reverse("pharmacy:purchase-create"), {"vendor": vendor.pk})
    purchase = Purchase.objects.get()
    assert resp.url == reverse("pharmacy:purchase-detail", args=[purchase.pk])
    client.post(
        reverse("pharmacy:purchase-detail", args=[purchase.pk]),
        {"form": "item", "product": ors.pk, "packs": "2", "loose_units": "0",
         "free_quantity": "0", "unit_cost": "30", "batch_number": "x1",
         "expiry_date": (TODAY + timedelta(days=200)).isoformat()},
    )  # fmt: skip
    assert purchase.items.get().quantity == 2  # ORS pack size is 1
    client.post(
        reverse("pharmacy:purchase-details", args=[purchase.pk]),
        {"supplier_invoice_number": "AP-1", "discount": "0", "tax_amount": "0"},
    )
    assert client.get(reverse("pharmacy:purchase-detail", args=[purchase.pk])).status_code == 200
    client.post(reverse("pharmacy:purchase-receive", args=[purchase.pk]))
    purchase.refresh_from_db()
    assert purchase.status == Status.RECEIVED and stock.on_hand(purchase.branch, ors) == 2
    item = purchase.items.get()
    resp = client.post(
        reverse("pharmacy:purchase-return", args=[purchase.pk]),
        {f"qty_{item.pk}": "1", "reason": "Leaking sachet"},
    )
    assert resp.status_code == 302 and purchase.returns.count() == 1
    for name in ("vendor-list", "purchase-list", "purchase-report"):
        assert client.get(reverse(f"pharmacy:{name}")).status_code == 200
    assert client.get(reverse("pharmacy:vendor-detail", args=[vendor.pk])).status_code == 200
    csv = client.get(reverse("pharmacy:purchase-report"), {"format": "csv"})
    assert "AP-1" in csv.content.decode()


def test_purchasing_permissions(client, make_user):
    client.force_login(make_user("reception"))
    assert client.get(reverse("pharmacy:vendor-list")).status_code == 403
    client.force_login(make_user("cashier"))  # financial reports only
    assert client.get(reverse("pharmacy:purchase-list")).status_code == 403
    assert client.get(reverse("pharmacy:purchase-report")).status_code == 200
