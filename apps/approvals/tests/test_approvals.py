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
