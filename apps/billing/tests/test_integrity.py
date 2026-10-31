"""Ledger check, correction from records, patient statement and printed copies."""

from decimal import Decimal

import pytest

from apps.billing import integrity, services
from apps.billing.models import Invoice

pytestmark = pytest.mark.django_db


@pytest.fixture
def paid_invoice(make_request, admin_user, patient, cash):
    request = make_request(admin_user)
    invoice = services.create_invoice(request=request, patient=patient)
    services.add_item(invoice, request=request, description="X-ray", unit_price=Decimal("500"))
    services.issue(invoice, request=request)
    services.receive_payment(invoice, request=request, method=cash, amount=Decimal("200"))
    return invoice, request


def test_clean_books_report_nothing(paid_invoice, branch):
    assert integrity.all_problems(branch) == []


def test_mismatch_is_found_and_corrected_without_moving_money(paid_invoice, branch, admin_client):
    invoice, _ = paid_invoice
    Invoice.objects.filter(pk=invoice.pk).update(amount_paid=Decimal("300"))
    problems = integrity.all_problems(branch)
    assert len(problems) == 1 and "300" in problems[0].detail
    assert "Correct from records" in admin_client.get("/billing/check/").content.decode()
    admin_client.post(f"/billing/{invoice.pk}/repair/")
    invoice.refresh_from_db()
    assert invoice.amount_paid == Decimal("200") and invoice.payments.count() == 1
    assert integrity.all_problems(branch) == []


def test_statement_balance_equals_balance_due_after_refund(paid_invoice, branch, patient):
    invoice, request = paid_invoice
    payment = invoice.payments.get()
    services.refund(payment, request=request, amount=Decimal("50"), reason="Over-charged")
    invoice.refresh_from_db()
    statement = integrity.patient_statement(patient, branch)
    assert statement["balance"] == invoice.balance_due == Decimal("300")


def test_two_copies_print(paid_invoice, admin_client):
    invoice, _ = paid_invoice
    html = admin_client.get(f"/billing/{invoice.pk}/print/?copies=2").content.decode()
    assert "Patient copy" in html and "Office copy" in html
