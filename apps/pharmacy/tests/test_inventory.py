from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.pharmacy import stock
from apps.pharmacy.models import InventoryBatch, Product, StockTransaction
from apps.prescriptions.models import Medicine

Kind = StockTransaction.Kind
TODAY = timezone.localdate()


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
def product(org):
    return Product.objects.create(
        organization=org, sku="P-1", brand_name="Panadol", strength="500 mg",
        generic_name="Paracetamol", pack_size=10, sale_price=Decimal("3"), reorder_level=20,
    )  # fmt: skip


def _receive(req, product, batch, days, qty, cost="2"):
    return stock.receive(
        request=req, product=product, batch_number=batch,
        expiry_date=TODAY + timedelta(days=days), quantity=qty, purchase_cost=Decimal(cost),
    )  # fmt: skip


def _backdate_expiry(batch, days_ago):
    InventoryBatch.objects.filter(pk=batch.pk).update(expiry_date=TODAY - timedelta(days=days_ago))


def test_receive_creates_batch_and_ledger(req, product):
    txn = _receive(req, product, "b-1", 200, 100)
    batch = txn.batch
    assert (batch.batch_number, batch.quantity_available, batch.quantity_received) == (
        "B-1",
        100,
        100,
    )
    assert txn.kind == Kind.PURCHASE and txn.balance_after == 100
    again = _receive(req, product, "B-1", 200, 50)
    batch.refresh_from_db()
    assert batch.quantity_available == 150 and again.balance_after == 150
    with pytest.raises(stock.StockError, match="expiry"):
        _receive(req, product, "B-1", 300, 5)
    with pytest.raises(stock.StockError, match="expired"):
        _receive(req, product, "B-2", 0, 5)


def test_unlogged_mutation_is_refused(req, product):
    batch = _receive(req, product, "B1", 100, 10).batch
    batch.quantity_available = 999
    with pytest.raises(ValidationError, match="ledger"):
        batch.save()


def test_fefo_allocation_skips_expired(req, product, branch):
    late = _receive(req, product, "LATE", 300, 30).batch
    early = _receive(req, product, "EARLY", 60, 10).batch
    old = _receive(req, product, "OLD", 10, 50).batch
    _backdate_expiry(old, 1)
    plan = stock.fefo_allocate(branch, product, 25)
    assert [(b.pk, q) for b, q in plan] == [(early.pk, 10), (late.pk, 15)]
    assert stock.on_hand(branch, product) == 40
    with pytest.raises(stock.StockError, match="Only 40"):
        stock.fefo_allocate(branch, product, 41)


def test_sale_negative_stock_and_return(req, product):
    batch = _receive(req, product, "B1", 100, 10).batch
    sale = stock.sell(batch, request=req, quantity=8, reference="SALE-1")
    batch.refresh_from_db()
    assert batch.quantity_available == 2 and sale.quantity == -8
    with pytest.raises(stock.StockError, match="Only 2"):
        stock.sell(batch, request=req, quantity=3)
    stock.sale_return(sale, request=req, quantity=5, reason="Wrong strength")
    with pytest.raises(stock.StockError, match="Only 3"):
        stock.sale_return(sale, request=req, quantity=4, reason="again")
    batch.refresh_from_db()
    assert batch.quantity_available == 7


def test_expired_batch_cannot_be_sold(req, product):
    batch = _receive(req, product, "B1", 100, 10).batch
    _backdate_expiry(batch, 1)
    batch.refresh_from_db()
    with pytest.raises(stock.StockError, match="expired"):
        stock.sell(batch, request=req, quantity=1)
    stock.write_off(batch, request=req, quantity=10, kind=Kind.EXPIRED, reason="Expired")
    batch.refresh_from_db()
    assert batch.quantity_available == 0


def test_adjustment_and_purchase_return(req, product):
    batch = _receive(req, product, "B1", 100, 10).batch
    adj = stock.adjust_to_count(batch, request=req, counted=7, reason="Shelf count")
    assert adj.quantity == -3 and adj.balance_after == 7
    with pytest.raises(stock.StockError, match="matches"):
        stock.adjust_to_count(batch, request=req, counted=7, reason="x")
    stock.purchase_return(batch, request=req, quantity=2, reason="Damaged in transit")
    with pytest.raises(stock.StockError, match="reason"):
        stock.write_off(batch, request=req, quantity=1, kind=Kind.DAMAGED, reason="")
    totals = sum(StockTransaction.objects.filter(batch=batch).values_list("quantity", flat=True))
    batch.refresh_from_db()
    assert totals == batch.quantity_available == 5  # ledger always explains the balance


def test_cashier_cannot_change_stock(rf, make_user, org, branch, product):
    with pytest.raises(stock.StockError, match="not allowed"):
        _receive(_req(rf, make_user("cashier"), org, branch), product, "B1", 100, 1)


def test_alerts(req, product, branch, org):
    _receive(req, product, "SOON", 30, 5)
    old = _receive(req, product, "OLD", 30, 4).batch
    _backdate_expiry(old, 5)
    assert list(stock.low_stock(branch, org)) == [product]  # 5 saleable <= reorder 20
    assert [b.batch_number for b in stock.near_expiry(branch)] == ["SOON"]
    assert [b.batch_number for b in stock.expired(branch)] == ["OLD"]


def test_batches_are_per_branch(req, rf, pharmacist, org, branch2, product):
    batch = _receive(req, product, "B1", 100, 10).batch
    other = _req(rf, pharmacist, org, branch2)
    with pytest.raises(stock.StockError, match="another branch"):
        stock.sell(batch, request=other, quantity=1)
    assert stock.on_hand(branch2, product) == 0


# --- Views -------------------------------------------------------------------------------------


def test_product_and_receive_views(client, pharmacist, org):
    client.force_login(pharmacist)
    resp = client.post(
        reverse("pharmacy:product-create"),
        {"brand_name": "Augmentin", "generic_name": "Co-amoxiclav", "strength": "625 mg",
         "unit": "tablet", "pack_size": "6", "purchase_price": "40", "sale_price": "55",
         "tax_percent": "0", "reorder_level": "12", "create_medicine": "on", "sku": "",
         "barcode": "8964000000017"},
    )  # fmt: skip
    product = Product.objects.get(brand_name="Augmentin")
    assert resp.status_code == 302 and product.sku == "P-00001"
    assert product.medicine == Medicine.objects.get(brand_name="Augmentin")
    resp = client.post(
        reverse("pharmacy:receive"),
        {"product": product.pk, "batch_number": "aug-77", "packs": "5", "loose_units": "2",
         "expiry_date": (TODAY + timedelta(days=400)).isoformat(), "purchase_cost": "40",
         "reference": "INV-9"},
    )  # fmt: skip
    assert resp.status_code == 302
    batch = InventoryBatch.objects.get()
    assert batch.quantity_available == 32 and batch.sale_price == Decimal("55")
    detail = client.get(reverse("pharmacy:product-detail", args=[product.pk]))
    assert "Dispense next" in detail.content.decode()
    client.post(
        reverse("pharmacy:batch-action", args=[batch.pk]),
        {"action": "damaged", "quantity": "2", "reason": "Broken strip"},
    )
    batch.refresh_from_db()
    assert batch.quantity_available == 30
    for url in ("pharmacy:dashboard", "pharmacy:product-list", "pharmacy:ledger"):
        assert client.get(reverse(url)).status_code == 200
    assert (
        client.get(reverse("pharmacy:product-list") + "?q=8964000000017")
        .content.decode()
        .count("Augmentin")
    )


def test_inventory_permissions(client, make_user, product):
    client.force_login(make_user("reception"))
    assert client.get(reverse("pharmacy:dashboard")).status_code == 403
    client.force_login(make_user("cashier"))
    assert client.get(reverse("pharmacy:receive")).status_code == 403
