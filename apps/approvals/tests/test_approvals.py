from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.approvals import services
from apps.approvals.models import StockChangeRequest
from apps.pharmacy import stock
from apps.pharmacy.models import InventoryBatch, Product, StockTransaction

TODAY = timezone.localdate()
Status = StockChangeRequest.Status


@pytest.fixture
def pharmacist(make_user):
    return make_user("pharmacy", username="pharma")


@pytest.fixture
def batch(make_request, admin_user, org):
    product = Product.objects.create(
        organization=org, sku="P-1", brand_name="Panadol", pack_size=10, sale_price=Decimal("3")
    )
    return stock.receive(
        request=make_request(admin_user), product=product, batch_number="B1",
        expiry_date=TODAY + timedelta(days=300), quantity=100, purchase_cost=Decimal("2"),
    ).batch  # fmt: skip


def _qty(batch):
    return InventoryBatch.objects.get(pk=batch.pk).quantity_available


def test_pharmacist_write_off_waits_for_approval(make_request, pharmacist, admin_user, batch):
    change = services.request_stock_change(
        make_request(pharmacist), batch, action="damaged", quantity=5, reason="Broken"
    )
    assert change.status == Status.PENDING and _qty(batch) == 100
    services.approve(make_request(admin_user), change)
    change.refresh_from_db()
    assert change.status == Status.APPROVED and _qty(batch) == 95
    assert change.transaction.kind == StockTransaction.Kind.DAMAGED
    assert "asked by" in change.transaction.reason


def test_admin_change_applies_at_once(make_request, admin_user, batch):
    change = services.request_stock_change(
        make_request(admin_user), batch, action="count", quantity=90, reason="Shelf count"
    )
    assert change.status == Status.APPROVED and _qty(batch) == 90


def test_count_applies_the_difference_found(make_request, pharmacist, admin_user, batch):
    change = services.request_stock_change(
        make_request(pharmacist), batch, action="count", quantity=97, reason="Count"
    )
    stock.sell(batch, request=make_request(pharmacist), quantity=10)  # sold after counting
    services.approve(make_request(admin_user), change)
    assert _qty(batch) == 87  # 100 - 10 sold - 3 missing


def test_reject_leaves_stock(make_request, pharmacist, admin_user, batch):
    req = make_request(pharmacist)
    change = services.request_stock_change(req, batch, action="expired", quantity=5, reason="x")
    with pytest.raises(services.ApprovalError):
        services.approve(req, change)  # a pharmacist cannot approve
    with pytest.raises(services.ApprovalError):
        services.reject(make_request(admin_user), change, note="")
    services.reject(make_request(admin_user), change, note="Stock is on the shelf")
    assert StockChangeRequest.objects.get().status == Status.REJECTED and _qty(batch) == 100


def test_batch_details_correction(make_request, pharmacist, admin_user, batch):
    new_expiry = TODAY + timedelta(days=500)
    change = services.request_stock_change(
        make_request(pharmacist), batch, action="details", new_batch_number="b1x",
        new_expiry_date=new_expiry, reason="Typed wrong",
    )  # fmt: skip
    assert InventoryBatch.objects.get(pk=batch.pk).batch_number == "B1"
    services.approve(make_request(admin_user), change)
    fresh = InventoryBatch.objects.get(pk=batch.pk)
    assert (fresh.batch_number, fresh.expiry_date) == ("B1X", new_expiry)


def test_screen_and_bulk(client, make_request, pharmacist, admin_user, batch):
    client.force_login(pharmacist)
    resp = client.post(
        reverse("pharmacy:batch-action", args=[batch.pk]),
        {"action": "damaged", "quantity": "2", "reason": "Broken strip"},
    )
    assert resp.status_code == 302 and _qty(batch) == 100
    assert client.get(reverse("approvals:list")).status_code == 403
    client.force_login(admin_user)
    page = client.get(reverse("approvals:list")).content.decode()
    assert "Broken strip" in page
    ids = list(StockChangeRequest.objects.values_list("pk", flat=True))
    client.post(reverse("approvals:bulk"), {"ids": ids, "decision": "approve"})
    assert _qty(batch) == 98


def test_other_branch_request_is_hidden(client, make_request, pharmacist, admin_user, batch):
    change = services.request_stock_change(
        make_request(pharmacist), batch, action="damaged", quantity=1, reason="x"
    )
    other = make_request(admin_user)
    other.branch = None
    assert services.pending_count(other) == 0
    client.force_login(admin_user)
    resp = client.post(reverse("approvals:approve", args=[change.pk]))
    assert resp.status_code == 302 and _qty(batch) == 99


# --- Correct unit ------------------------------------------------------------------------------


def _syrup(org, make_request, admin_user, qty=50):
    product = Product.objects.create(
        organization=org, sku="S-1", brand_name="Cough syrup", unit="tablet", pack_size=10,
        pack_unit="strip", sale_price=Decimal("8"), purchase_price=Decimal("5"), reorder_level=20,
    )  # fmt: skip
    batch = stock.receive(
        request=make_request(admin_user), product=product, batch_number="SY1",
        expiry_date=TODAY + timedelta(days=300), quantity=qty, purchase_cost=Decimal("5"),
    ).batch  # fmt: skip
    return product, batch


def test_unit_name_fix_keeps_stock(make_request, admin_user, org):
    product, batch = _syrup(org, make_request, admin_user)
    services.request_unit_correction(
        make_request(admin_user), product, unit="Bottle", reason="Typed tablet"
    )
    product.refresh_from_db()
    assert (product.unit, product.pack_size, product.pack_unit) == ("bottle", 1, "bottle")
    assert _qty(batch) == 50 and product.sale_price == Decimal("8")


def test_unit_conversion_waits_then_converts_stock_and_prices(
    make_request, pharmacist, admin_user, org, branch
):
    product, batch = _syrup(org, make_request, admin_user)
    change = services.request_unit_correction(
        make_request(pharmacist), product, unit="bottle", factor=10, reason="5 bottles, not 50"
    )
    assert change.status == Status.PENDING and change.summary.startswith("tablet → bottle")
    product.refresh_from_db()
    assert product.unit == "tablet" and _qty(batch) == 50
    services.approve(make_request(admin_user), change)
    product.refresh_from_db()
    fresh = InventoryBatch.objects.get(pk=batch.pk)
    assert product.unit == "bottle" and fresh.quantity_available == 5
    assert product.sale_price == Decimal("80") and product.purchase_price == Decimal("50")
    assert fresh.purchase_cost == Decimal("50") and product.reorder_level == 2
    assert stock.ledger_mismatches(branch) == []  # the adjustment row keeps the ledger whole
    adj = StockTransaction.objects.filter(batch=batch, kind="adjustment").get()
    assert adj.quantity == -45 and "10 tablet = 1 bottle" in adj.reason


def test_unit_conversion_refuses_part_units(make_request, pharmacist, admin_user, org):
    product, _ = _syrup(org, make_request, admin_user, qty=45)
    with pytest.raises(services.ApprovalError, match="not a whole number"):
        services.request_unit_correction(
            make_request(pharmacist), product, unit="bottle", factor=10, reason="x"
        )


def test_correct_unit_screen(client, make_request, pharmacist, admin_user, org):
    product, batch = _syrup(org, make_request, admin_user)
    client.force_login(pharmacist)
    url = reverse("pharmacy:product-unit", args=[product.pk])
    page = client.get(reverse("pharmacy:product-detail", args=[product.pk])).content.decode()
    assert "Wrong unit? Correct it" in page
    client.post(url, {"unit": "bottle", "factor": "10", "pack_size": "1", "reason": "Syrup"})
    assert StockChangeRequest.objects.get().status == Status.PENDING
    page = client.get(reverse("pharmacy:product-detail", args=[product.pk])).content.decode()
    assert "Unit correction waiting approval" in page
    client.force_login(admin_user)
    assert "Whole product" in client.get(reverse("approvals:list")).content.decode()
    client.post(reverse("approvals:approve", args=[StockChangeRequest.objects.get().pk]))
    assert _qty(batch) == 5


def _grant(user, *codes):
    from apps.accounts.models import Permission

    user.extra_permissions.add(*Permission.objects.filter(code__in=codes))


def test_write_off_and_delete_waits_then_clears_stock(make_request, pharmacist, admin_user, batch):
    _grant(pharmacist, "pharmacy.product.delete")
    product = batch.product
    change = services.request_delete(
        make_request(pharmacist), product, write_off_as="damaged", reason="Discontinued"
    )
    assert change.status == Status.PENDING and _qty(batch) == 100
    assert Product.objects.filter(pk=product.pk).exists()
    with pytest.raises(services.ApprovalError, match="already waiting"):
        services.request_delete(
            make_request(pharmacist), product, write_off_as="damaged", reason="Again"
        )
    stock.sell(batch, request=make_request(pharmacist), quantity=10)  # sold while waiting
    services.approve(make_request(admin_user), change)
    assert _qty(batch) == 0
    assert not Product.objects.filter(pk=product.pk).exists()
    gone = Product.all_objects.get(pk=product.pk)
    assert gone.deleted_reason == "Discontinued"
    off = StockTransaction.objects.get(product=product, kind=StockTransaction.Kind.DAMAGED)
    assert off.quantity == -90 and "asked by" in off.reason


def test_write_off_and_delete_needs_delete_permission(make_request, pharmacist, batch):
    with pytest.raises(services.ApprovalError, match="not allowed"):
        services.request_delete(
            make_request(pharmacist), batch.product, write_off_as="expired", reason="x"
        )


def test_write_off_and_delete_refuses_stock_in_another_branch(
    make_request, make_user, admin_user, batch, branch2
):
    other = make_request(admin_user)
    other.branch = branch2
    stock.receive(
        request=other, product=batch.product, batch_number="B2",
        expiry_date=TODAY + timedelta(days=300), quantity=7, purchase_cost=Decimal("2"),
    )  # fmt: skip
    with pytest.raises(services.ApprovalError, match="another branch"):
        services.request_delete(
            make_request(admin_user), batch.product, write_off_as="expired", reason="x"
        )
    assert _qty(batch) == 100


def test_delete_button_leads_to_write_off_page(client, admin_user, batch):
    client.force_login(admin_user)
    product = batch.product
    resp = client.post(reverse("recyclebin:delete", args=["product", product.pk]))
    url = reverse("pharmacy:product-delete", args=[product.pk])
    assert resp.status_code == 302 and resp.url == url
    assert "Write off and delete" in client.get(url).content.decode()
    resp = client.post(url, {"write_off_as": "expired", "reason": "Wrong entry"})
    assert resp.url == reverse("pharmacy:product-list")
    assert _qty(batch) == 0 and not Product.objects.filter(pk=product.pk).exists()
    assert StockChangeRequest.objects.get().status == Status.APPROVED
