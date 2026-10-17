"""N+1 guard (Phase 28): list pages must run the same number of queries for 3 rows as for 12.

A page whose query count grows with the rows is doing one query per row; that is what makes
lists slow on a busy day. Each case builds rows with a factory, measures, adds more, measures
again.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.appointments.tests.test_appointments import _appt
from apps.audit import services as audit
from apps.billing import services as billing
from apps.ipd.tests.helpers import make_request
from apps.lab import services as lab
from apps.lab.tests.test_lab import catalogue, tech  # noqa: F401 (fixtures)
from apps.patients.services import register_patient
from apps.pharmacy.models import Product
from apps.queue import services as queue

_counter = {"n": 0}


def _patient(org, branch, user):
    _counter["n"] += 1
    n = _counter["n"]
    return register_patient(
        organization=org, branch=branch, user=user,
        data={"full_name": f"Patient {n}", "gender": "female", "phone": f"0300{n:07d}"},
    )  # fmt: skip


def _queries(client, url) -> int:
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    assert response.status_code == 200, url
    return len(ctx.captured_queries)


@pytest.fixture
def factories(rf, org, branch, admin_user, doctor, department, catalogue, tech):  # noqa: F811
    req = make_request(rf, admin_user, org, branch)
    lab_req = make_request(rf, tech, org, branch)
    day = timezone.localdate() + timedelta(days=1)

    def patients(n):
        for _ in range(n):
            _patient(org, branch, admin_user)

    def appointments(n):
        for _ in range(n):
            p = _patient(org, branch, admin_user)
            _appt(org, branch, p, doctor, admin_user, day, start=(timezone.datetime(2000, 1, 1, 9)
                  + timedelta(minutes=15 * (_counter["n"] % 20))).time())  # fmt: skip

    def tokens(n):
        for _ in range(n):
            p = _patient(org, branch, admin_user)
            queue.issue_token(request=req, patient=p, department=department, doctor=doctor)

    def invoices(n):
        for _ in range(n):
            p = _patient(org, branch, admin_user)
            inv = billing.create_invoice(request=req, patient=p)
            billing.add_item(
                inv, request=req, description="Consultation", unit_price=Decimal("500")
            )
            billing.issue(inv, request=req)

    def lab_orders(n):
        for _ in range(n):
            p = _patient(org, branch, admin_user)
            lab.create_order(request=lab_req, patient=p, tests=[catalogue["hb"], catalogue["glu"]])

    def products(n):
        start = Product.objects.count()
        for i in range(n):
            Product.objects.create(organization=org, sku=f"S-{start + i}", brand_name=f"Med {i}")

    def audit_rows(n):
        for i in range(n):
            audit.record(req, "patient.view", summary=f"view {i}")

    return {
        "patients:list": patients,
        "appointments:list": appointments,
        "queue:desk": tokens,
        "billing:invoice-list": invoices,
        "lab:worklist": lab_orders,
        "pharmacy:product-list": products,
        "audit:log": audit_rows,
        "accounts:staff-list": None,  # staff grow with make_user below
    }


@pytest.mark.parametrize(
    "url_name",
    ["patients:list", "appointments:list", "queue:desk", "billing:invoice-list", "lab:worklist",
     "pharmacy:product-list", "audit:log"],
)  # fmt: skip
def test_list_pages_do_not_query_per_row(admin_client, factories, url_name):
    url = reverse(url_name)
    if url_name == "appointments:list":
        url += f"?date={(timezone.localdate() + timedelta(days=1)).isoformat()}"
    make = factories[url_name]
    make(3)
    _queries(admin_client, url)  # warm caches (permissions, settings rows)
    small = _queries(admin_client, url)
    make(9)
    large = _queries(admin_client, url)
    assert large <= small, f"{url_name}: {small} queries for 3 rows, {large} for 12"


def test_staff_list_does_not_query_per_row(admin_client, make_user):
    url = reverse("accounts:staff-list")
    for i in range(3):
        make_user("reception", username=f"s{i}")
    _queries(admin_client, url)
    small = _queries(admin_client, url)
    for i in range(3, 12):
        make_user("doctor", username=f"s{i}")
    assert _queries(admin_client, url) <= small


def test_dashboard_query_budget(admin_client, factories):
    for key in ("patients:list", "queue:desk", "billing:invoice-list", "lab:worklist"):
        factories[key](3)
    _queries(admin_client, reverse("core:dashboard"))
    small = _queries(admin_client, reverse("core:dashboard"))
    for key in ("patients:list", "queue:desk", "billing:invoice-list", "lab:worklist"):
        factories[key](6)
    large = _queries(admin_client, reverse("core:dashboard"))
    assert large <= small and large < 80, (small, large)


def test_trend_collection_matches_day_totals(rf, org, branch, admin_user, patient):
    """The fast per-day trend must agree with the cashier's day closing."""
    from apps.billing.models import PaymentMethod

    req = make_request(rf, admin_user, org, branch)
    billing.ensure_payment_methods(org)
    cash = PaymentMethod.objects.get(organization=org, code="cash")
    inv = billing.create_invoice(request=req, patient=patient)
    billing.add_item(inv, request=req, description="Consultation", unit_price=Decimal("800"))
    payment = billing.receive_payment(inv, request=req, method=cash, amount=Decimal("800"))
    billing.refund(payment, request=req, amount=Decimal("300"), reason="Overcharged")
    today = timezone.localdate()
    by_day = billing.net_by_day(branch, today - timedelta(days=2), today)
    assert by_day[today] == billing.day_totals(branch, today)["net"] == Decimal("500.00")
    assert by_day[today - timedelta(days=1)] == 0
