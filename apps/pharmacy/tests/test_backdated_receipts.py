"""Supplier bills entered late: an optional received date dates the stock, the purchase and the
supplier ledger on that day; the Receive stock screen also keeps the invoice date."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.pharmacy import purchasing, stock
from apps.pharmacy.models import StockTransaction, VendorLedgerEntry
from apps.pharmacy.tests.test_purchasing import (  # noqa: F401
    draft,
    ors,
    pharmacist,
    req,
    vendor,
)
from apps.pharmacy.tests.test_workflows import _product, request_  # noqa: F401

pytestmark = pytest.mark.django_db

TODAY = timezone.localdate()


def _ready(req, draft):
    item = draft.items.get()
    purchasing.update_receipt(
        item, request=req, batch_number="B-1", expiry_date=TODAY + timedelta(days=400)
    )
    return draft


def test_received_on_dates_stock_purchase_and_supplier_ledger(req, draft):
    earlier = TODAY - timedelta(days=5)
    purchase = purchasing.receive(_ready(req, draft), request=req, received_on=earlier)
    assert timezone.localtime(purchase.received_at).date() == earlier
    txn = StockTransaction.objects.get()
    assert timezone.localtime(txn.created_at).date() == earlier
    entry = VendorLedgerEntry.objects.get(purchase=purchase)
    assert timezone.localtime(entry.created_at).date() == earlier


def test_no_received_date_is_now(req, draft):
    purchase = purchasing.receive(_ready(req, draft), request=req, received_on=None)
    assert timezone.localtime(purchase.received_at).date() == TODAY


def test_future_received_date_is_refused(req, draft):
    with pytest.raises(purchasing.PurchaseError, match="future"):
        purchasing.receive(_ready(req, draft), request=req, received_on=TODAY + timedelta(days=1))
    assert not StockTransaction.objects.exists()


def test_purchase_page_posts_with_a_received_date(admin_client, draft, req):
    _ready(req, draft)
    earlier = TODAY - timedelta(days=3)
    response = admin_client.post(
        f"/inventory/purchases/{draft.pk}/receive/", {"received_on": earlier.isoformat()}
    )
    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.status == "received"
    assert timezone.localtime(draft.received_at).date() == earlier
    log = AuditLog.objects.get(action="purchase.receive")
    assert f"received {earlier:%d %b %Y}" in log.summary


def test_receive_stock_screen_takes_invoice_and_received_dates(admin_client, org, request_):
    product = _product(org)
    later = (TODAY + timedelta(days=300)).isoformat()
    earlier, bill_day = TODAY - timedelta(days=4), TODAY - timedelta(days=6)
    data = {"rows": "1", "product": [product.pk], "qty": ["1"], "unit": ["unit"], "free": [""],
            "cost": ["3"], "tax": [""], "batch": ["X1"], "expiry": [later], "sale_price": [""],
            "reference": "INV-77", "invoice_date": bill_day.isoformat(),
            "received_on": earlier.isoformat()}  # fmt: skip
    assert admin_client.post("/inventory/receive/", data).status_code == 302
    txn = StockTransaction.objects.get(product=product)
    assert txn.reference == f"INV-77 · inv {bill_day:%d-%m-%Y}"
    assert timezone.localtime(txn.created_at).date() == earlier
    assert stock.on_hand(request_.branch, product) == 1


def test_receive_stock_screen_refuses_future_dates(admin_client, org, request_):
    product = _product(org)
    later = (TODAY + timedelta(days=300)).isoformat()
    data = {"rows": "1", "product": [product.pk], "qty": ["1"], "unit": ["unit"], "free": [""],
            "cost": ["3"], "tax": [""], "batch": ["X1"], "expiry": [later], "sale_price": [""],
            "received_on": (TODAY + timedelta(days=2)).isoformat()}  # fmt: skip
    page = admin_client.post("/inventory/receive/", data)
    assert page.status_code == 200 and "future" in page.content.decode()
    data.update(received_on="", invoice_date=(TODAY + timedelta(days=2)).isoformat())
    page = admin_client.post("/inventory/receive/", data)
    assert page.status_code == 200 and "future" in page.content.decode()
    assert stock.on_hand(request_.branch, product) == 0
