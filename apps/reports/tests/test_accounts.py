"""Statement reports: every new report renders, filters work, money adds up, access holds."""

from decimal import Decimal

import pytest

from apps.pharmacy.models import Vendor, VendorLedgerEntry
from apps.reports import engine

pytestmark = pytest.mark.django_db

NEW = [
    "supplier-statement", "supplier-balances", "supplier-invoices", "supplier-paid",
    "supplier-returns", "must-purchase", "customer-invoices", "customer-products",
    "customer-returns", "sale-detail", "profit-by-product", "sale-by-salesperson",
    "user-collection", "doctor-work", "staff-pay", "profit-and-loss", "stock-value-by-supplier",
]  # fmt: skip


@pytest.mark.parametrize("key", NEW)
def test_report_renders_and_exports(admin_client, key):
    assert key in engine.REPORTS
    assert admin_client.get(f"/reports/{key}/").status_code == 200
    assert admin_client.get(f"/reports/{key}/?format=csv").status_code == 200


def test_supplier_statement_has_a_running_balance(admin_client, org, branch, admin_user):
    vendor = Vendor.objects.create(organization=org, name="Acme")
    other = Vendor.objects.create(organization=org, name="Other")
    for v, amount in ((vendor, "100"), (vendor, "-40"), (other, "999")):
        VendorLedgerEntry.objects.create(
            organization=org, branch=branch, vendor=v, kind="adjustment",
            amount=Decimal(amount), created_by=admin_user,
        )  # fmt: skip
    page = admin_client.get(f"/reports/supplier-statement/?vendor={vendor.pk}")
    rows = page.context["rows"]
    assert [r["balance"] for r in rows] == [Decimal("100"), Decimal("60")]


def test_other_organization_vendor_is_ignored(admin_client, branch, admin_user):
    from apps.organizations.models import Organization

    stranger = Organization.objects.create(name="Other", patient_id_prefix="OT")
    vendor = Vendor.objects.create(organization=stranger, name="Hidden")
    page = admin_client.get(f"/reports/supplier-statement/?vendor={vendor.pk}")
    assert page.context["f"].vendor is None


def test_money_reports_refuse_reception(client, make_user):
    client.force_login(make_user("reception", username="r9"))
    assert client.get("/reports/supplier-statement/").status_code == 403


def _day_rows(admin_client, key):
    return admin_client.get(f"/reports/{key}/").context["rows"]


def test_daily_purchases_and_sale_purchase_add_up(admin_client, org, branch, admin_user):
    from django.utils import timezone

    from apps.pharmacy.models import Purchase

    vendor = Vendor.objects.create(organization=org, name="Acme")
    Purchase.objects.create(
        organization=org, branch=branch, vendor=vendor, number="PO-1",
        status=Purchase.Status.RECEIVED, received_at=timezone.now(),
        grand_total=Decimal("500"), created_by=admin_user,
    )  # fmt: skip
    VendorLedgerEntry.objects.create(
        organization=org, branch=branch, vendor=vendor, kind="payment",
        amount=Decimal("-200"), created_by=admin_user,
    )  # fmt: skip
    day = _day_rows(admin_client, "daily-purchases")[0]
    assert (day["purchased"], day["net"], day["paid"]) == (
        Decimal("500"), Decimal("500"), Decimal("200"),
    )  # fmt: skip
    day = _day_rows(admin_client, "daily-sale-purchase")[0]
    assert (day["sales"], day["purchases"]) == (Decimal("0"), Decimal("500"))
    assert day["difference"] == Decimal("-500")


@pytest.mark.parametrize("key", ["daily-sales", "daily-purchases", "daily-sale-purchase"])
def test_daily_reports_render_and_export(admin_client, key):
    assert admin_client.get(f"/reports/{key}/").status_code == 200
    assert admin_client.get(f"/reports/{key}/?format=csv").status_code == 200


def test_inventory_manager_opens_the_ledger_but_not_sales(client, make_user, branch):
    from apps.accounts.services import effective_permissions

    user = make_user("pharmacy", username="ph9")
    assert "pharmacy.inventory.manage" in effective_permissions(user, branch)
    assert "reports.financial.view" not in effective_permissions(user, branch)
    client.force_login(user)
    assert client.get("/reports/supplier-statement/").status_code == 200
    assert client.get("/reports/daily-purchases/").status_code == 200
    assert client.get("/reports/daily-sales/").status_code == 403


def test_all_menu_lists_the_daily_reports(admin_client):
    html = admin_client.get("/menu/").content.decode()
    for key in ("daily-sale-purchase", "daily-sales", "daily-purchases", "supplier-statement"):
        assert f"/reports/{key}/" in html
