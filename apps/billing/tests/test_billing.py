from datetime import time
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.audit.models import AuditLog
from apps.billing import services
from apps.billing.models import (
    BillingSettings,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentMethod,
    Refund,
)

D = Decimal
Status = Invoice.Status


@pytest.fixture
def bill_request(rf, admin_user, org, branch):
    """A request-like object for calling services directly, as a Super admin."""
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = admin_user, org, branch
    req.sd_perms = effective_permissions(admin_user, branch)
    return req


@pytest.fixture
def role_request(rf, org, branch, make_user):
    from apps.accounts.services import effective_permissions

    def _make(role):
        user = make_user(role)
        req = rf.post("/")
        req.user, req.organization, req.branch = user, org, branch
        req.sd_perms = effective_permissions(user, branch)
        return req

    return _make


@pytest.fixture
def service(org):
    from apps.masterdata.models import Service, ServiceCategory

    cat = ServiceCategory.objects.create(organization=org, name="Lab")
    return Service.objects.create(
        organization=org, category=cat, code="CBC", name="CBC", price=D("800")
    )


@pytest.fixture
def cash(org):
    services.ensure_payment_methods(org)
    return PaymentMethod.objects.get(organization=org, code="cash")


@pytest.fixture
def card(org):
    services.ensure_payment_methods(org)
    return PaymentMethod.objects.get(organization=org, code="card")


def _invoice(req, patient, *lines):
    inv = services.create_invoice(request=req, patient=patient)
    for desc, price, qty in lines:
        services.add_item(inv, request=req, description=desc, unit_price=D(price), quantity=qty)
    inv.refresh_from_db()
    return inv


def _appt(org, branch, patient, doctor, user, fee="1500"):
    return Appointment.objects.create(
        organization=org, branch=branch, number=f"A-{Appointment.objects.count() + 1}",
        patient=patient, doctor=doctor, department=doctor.department,
        date=timezone.localdate(), start_time=time(9), end_time=time(9, 30), fee=D(fee),
        created_by=user,
    )  # fmt: skip


# --- Invoice numbering and totals --------------------------------------------------------------


def test_invoice_numbers_use_the_receipt_prefix(bill_request, patient, org):
    org.receipt_prefix = "SMC"
    org.save()
    a = services.create_invoice(request=bill_request, patient=patient)
    b = services.create_invoice(request=bill_request, patient=patient)
    assert (a.number, b.number) == ("SMC-00001", "SMC-00002")
    assert a.status == Status.DRAFT


def test_totals_with_line_discount_tax_and_invoice_discount(bill_request, patient, org):
    BillingSettings.objects.create(organization=org, default_tax_percent=D("10"))
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1), ("Dressing", "250", 2))
    assert (inv.subtotal, inv.tax_total, inv.grand_total) == (D("2000"), D("200"), D("2200"))

    dressing = inv.items.get(description="Dressing")
    services.set_line_discount(dressing, request=bill_request, amount="100", reason="Staff family")
    inv.refresh_from_db()
    # Dressing: 500 - 100 = 400, tax 40 → 440. Consultation 1650. Total 2090.
    assert (inv.line_discount_total, inv.tax_total, inv.grand_total) == (
        D("100"),
        D("190"),
        D("2090"),
    )

    services.set_invoice_discount(inv, request=bill_request, amount="90", reason="Welfare")
    inv.refresh_from_db()
    assert inv.grand_total == D("2000") and inv.discount_total == D("190")
    assert inv.discount_by == bill_request.user


def test_service_item_uses_branch_price(bill_request, patient, service, branch):
    from apps.masterdata.models import ServiceBranchPrice

    ServiceBranchPrice.objects.create(service=service, branch=branch, price=D("650"))
    inv = services.create_invoice(request=bill_request, patient=patient)
    item = services.add_item(inv, request=bill_request, service=service, quantity=2)
    assert (item.description, item.unit_price, item.line_total) == ("CBC", D("650"), D("1300"))
    assert item.kind == InvoiceItem.Kind.SERVICE


def test_appointment_invoice_starts_with_consultation_fee(
    bill_request, org, branch, patient, doctor, admin_user
):
    appt = _appt(org, branch, patient, doctor, admin_user)
    inv = services.create_invoice(request=bill_request, patient=patient, appointment=appt)
    assert inv.items.get().kind == InvoiceItem.Kind.CONSULTATION
    assert inv.grand_total == D("1500")
    with pytest.raises(services.BillingError, match="already billed"):
        services.create_invoice(request=bill_request, patient=patient, appointment=appt)


def test_discount_needs_permission_and_reason(role_request, bill_request, patient):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    reception = role_request("reception")
    with pytest.raises(services.BillingError, match="not allowed"):
        services.set_invoice_discount(inv, request=reception, amount="100", reason="x")
    with pytest.raises(services.BillingError, match="reason"):
        services.set_invoice_discount(inv, request=bill_request, amount="100", reason=" ")
    with pytest.raises(services.BillingError, match="between"):
        services.set_invoice_discount(inv, request=bill_request, amount="2000", reason="x")


def test_issued_invoice_items_are_locked(bill_request, patient):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    services.issue(inv, request=bill_request)
    inv.refresh_from_db()
    assert inv.status == Status.UNPAID and inv.balance_due == D("1500")
    with pytest.raises(services.BillingError, match="locked"):
        services.add_item(inv, request=bill_request, description="X", unit_price=D("1"))
    with pytest.raises(services.BillingError, match="locked"):
        services.remove_item(inv.items.get())


def test_cannot_issue_empty_invoice(bill_request, patient):
    inv = services.create_invoice(request=bill_request, patient=patient)
    with pytest.raises(services.BillingError, match="at least one item"):
        services.issue(inv, request=bill_request)


# --- Payments ----------------------------------------------------------------------------------


def test_partial_and_multiple_payments(bill_request, patient, cash, card):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1), ("CBC", "800", 1))
    p1 = services.receive_payment(inv, request=bill_request, method=cash, amount="1000")
    inv.refresh_from_db()
    assert inv.status == Status.PARTIALLY_PAID and inv.balance_due == D("1300")
    assert inv.issued_at is not None  # paying a draft issues it
    p2 = services.receive_payment(
        inv, request=bill_request, method=card, amount="1300", reference="TX-77"
    )
    inv.refresh_from_db()
    assert inv.status == Status.PAID and inv.balance_due == 0
    assert (p1.number, p2.number) == ("RCT-00001", "RCT-00002")
    assert inv.payments.count() == 2


def test_overpayment_and_bad_amounts_are_refused(bill_request, patient, cash):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    with pytest.raises(services.BillingError, match="balance due"):
        services.receive_payment(inv, request=bill_request, method=cash, amount="1500.01")
    with pytest.raises(services.BillingError, match="greater than zero"):
        services.receive_payment(inv, request=bill_request, method=cash, amount="0")
    services.receive_payment(inv, request=bill_request, method=cash, amount="1500")
    with pytest.raises(services.BillingError, match="paid"):
        services.receive_payment(inv, request=bill_request, method=cash, amount="1")


def test_reference_required_for_card(bill_request, patient, card):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    with pytest.raises(services.BillingError, match="reference"):
        services.receive_payment(inv, request=bill_request, method=card, amount="100")


def test_same_payment_form_twice_charges_once(bill_request, patient, cash):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    a = services.receive_payment(
        inv, request=bill_request, method=cash, amount="500", idempotency_key="k1"
    )
    b = services.receive_payment(
        inv, request=bill_request, method=cash, amount="500", idempotency_key="k1"
    )
    inv.refresh_from_db()
    assert a.pk == b.pk and inv.amount_paid == D("500") and Payment.objects.count() == 1


def test_disabled_method_cannot_be_used(bill_request, patient, cash):
    cash.is_active = False
    cash.save()
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    with pytest.raises(services.BillingError, match="active payment method"):
        services.receive_payment(inv, request=bill_request, method=cash, amount="100")


# --- Refunds and cancellation ------------------------------------------------------------------


def test_refund_against_original_payment(bill_request, patient, cash):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    pay = services.receive_payment(inv, request=bill_request, method=cash, amount="1500")
    r = services.refund(pay, request=bill_request, amount="500", reason="Cancelled service")
    inv.refresh_from_db()
    assert (r.number, r.method, r.payment) == ("RF-00001", cash, pay)
    assert inv.status == Status.PAID and inv.amount_refunded == D("500")
    assert inv.balance_due == 0  # a refund does not reopen the balance
    with pytest.raises(services.BillingError, match="can still be refunded"):
        services.refund(pay, request=bill_request, amount="1000.01", reason="x")
    services.refund(pay, request=bill_request, amount="1000", reason="Doctor absent")
    inv.refresh_from_db()
    assert inv.status == Status.REFUNDED and inv.refundable == 0


def test_refund_needs_permission_and_reason(role_request, bill_request, patient, cash):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    pay = services.receive_payment(inv, request=bill_request, method=cash, amount="1500")
    with pytest.raises(services.BillingError, match="not allowed"):
        services.refund(pay, request=role_request("reception"), amount="100", reason="x")
    with pytest.raises(services.BillingError, match="reason"):
        services.refund(pay, request=bill_request, amount="100", reason="")
    assert not Refund.objects.exists()


def test_cancel_rules(bill_request, role_request, patient, cash):
    draft = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    with pytest.raises(services.BillingError, match="reason"):
        services.cancel(draft, request=bill_request, reason="")
    services.cancel(draft, request=role_request("reception"), reason="Created by mistake")
    draft.refresh_from_db()
    assert draft.status == Status.CANCELLED and draft.balance_due == 0

    paid = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    pay = services.receive_payment(paid, request=bill_request, method=cash, amount="1500")
    with pytest.raises(services.BillingError, match="Refund the payments"):
        services.cancel(paid, request=bill_request, reason="x")
    services.refund(pay, request=bill_request, amount="1500", reason="Visit cancelled")
    services.cancel(paid, request=bill_request, reason="Visit cancelled")

    issued = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    services.issue(issued, request=bill_request)
    with pytest.raises(services.BillingError, match="Only staff"):
        services.cancel(issued, request=role_request("reception"), reason="x")


def test_day_totals_by_method(bill_request, patient, cash, card):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1), ("CBC", "800", 1))
    p = services.receive_payment(inv, request=bill_request, method=cash, amount="1000")
    services.receive_payment(inv, request=bill_request, method=card, amount="800", reference="T1")
    services.refund(p, request=bill_request, amount="200", reason="Overcharged")
    t = services.day_totals(bill_request.branch)
    assert (t["collected"], t["refunded"], t["net"], t["count"]) == (
        D("1800"),
        D("200"),
        D("1600"),
        2,
    )
    assert t["by_method"]["Cash"]["net"] == D("800")
    assert t["by_method"]["Card"]["net"] == D("800")


# --- Views: permissions, scoping and the full flow ---------------------------------------------


def test_cashier_flow_through_views(client, make_user, patient, service, org, cash):
    client.force_login(make_user("cashier"))
    r = client.post("/billing/new/", {"patient": patient.pk})
    inv = Invoice.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/billing/{inv.pk}/"
    client.post(f"/billing/{inv.pk}/items/", {"service": service.pk, "quantity": 2})
    client.post(
        f"/billing/{inv.pk}/items/",
        {"service": "", "description": "Dressing", "unit_price": "250", "quantity": 1},
    )
    inv.refresh_from_db()
    assert inv.grand_total == D("1850")

    # Payment modal over htmx: a successful payment redirects the page with the receipt ready.
    assert client.get(f"/billing/{inv.pk}/pay/", HTTP_HX_REQUEST="true").status_code == 200
    r = client.post(
        f"/billing/{inv.pk}/pay/",
        {"method": cash.pk, "amount": "1000", "key": "abc"},
        HTTP_HX_REQUEST="true",
    )
    payment = Payment.objects.get()
    assert r["HX-Redirect"] == f"/billing/{inv.pk}/?receipt={payment.pk}"
    inv.refresh_from_db()
    assert inv.status == Status.PARTIALLY_PAID

    # Invalid payment re-renders the modal with the error.
    r = client.post(
        f"/billing/{inv.pk}/pay/",
        {"method": cash.pk, "amount": "5000", "key": "def"},
        HTTP_HX_REQUEST="true",
    )
    assert r.status_code == 200 and b"balance due" in r.content

    for url in [
        f"/billing/{inv.pk}/",
        f"/billing/{inv.pk}/print/",
        f"/payments/{payment.pk}/receipt/",
        "/billing/",
        "/billing/?view=outstanding",
        "/payments/",
        f"/patients/{patient.pk}/?tab=billing",
    ]:
        assert client.get(url).status_code == 200, url
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"invoice.create", "invoice.item_add", "payment.create"} <= actions
    # Paying a draft issues it; the payment's audit entry records that status change.
    log = AuditLog.objects.get(action="payment.create")
    assert log.changes["status"] == ["draft", "partially_paid"]


def test_refund_view_records_audit(admin_client, bill_request, patient, cash):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    pay = services.receive_payment(inv, request=bill_request, method=cash, amount="1500")
    assert admin_client.get(f"/billing/{inv.pk}/refund/").status_code == 200
    admin_client.post(
        f"/billing/{inv.pk}/refund/",
        {"payment": pay.pk, "amount": "300", "reason": "Test not done", "key": "r1"},
    )
    log = AuditLog.objects.get(action="refund.create")
    assert "Test not done" in log.summary and log.changes["amount_refunded"] == ["0.00", "300.00"]


def test_role_permissions(client, make_user, bill_request, patient, cash):
    inv = _invoice(bill_request, patient, ("Consultation", "1500", 1))
    pay = services.receive_payment(inv, request=bill_request, method=cash, amount="1500")
    client.force_login(make_user("doctor"))
    assert client.get("/billing/").status_code == 403
    assert client.get(f"/payments/{pay.pk}/receipt/").status_code == 403
    client.force_login(make_user("reception"))
    assert client.get("/billing/").status_code == 200
    assert client.get(f"/billing/{inv.pk}/refund/").status_code == 403
    assert client.post(f"/billing/{inv.pk}/discount/", {"amount": "1"}).status_code == 403
    client.force_login(make_user("hospital_admin"))
    assert client.get("/billing/new/").status_code == 403  # view-only


def test_invoices_are_branch_and_org_scoped(
    admin_client, org, branch2, patient, admin_user, bill_request
):
    from apps.organizations.models import Organization

    other_branch = Invoice.objects.create(
        organization=org, branch=branch2, number="INV-X1", patient=patient, created_by=admin_user
    )
    assert admin_client.get(f"/billing/{other_branch.pk}/").status_code == 404
    other_org = Organization.objects.create(name="Other")
    from apps.organizations.models import Branch

    ob = Branch.objects.create(organization=other_org, name="O", code="O")
    stranger = Invoice.objects.create(
        organization=other_org, branch=ob, number="INV-X2", patient=patient, created_by=admin_user
    )
    assert admin_client.get(f"/billing/{stranger.pk}/").status_code == 404
    assert admin_client.post(f"/billing/{stranger.pk}/pay/", {}).status_code == 404


def test_billing_settings_page(admin_client, org):
    assert admin_client.get("/settings/billing/").status_code == 200
    assert PaymentMethod.objects.filter(organization=org).count() == 6
    admin_client.post(
        "/settings/billing/",
        {
            "form": "settings",
            "tax_label": "GST",
            "default_tax_percent": "5",
            "max_discount_percent": "100",
            "receipt_message": "Thank you.",
        },
    )
    assert services.settings_for(org).default_tax_percent == D("5")
    admin_client.post("/settings/billing/", {"form": "method", "name": "Raast"})
    raast = PaymentMethod.objects.get(organization=org, name="Raast")
    admin_client.post(f"/settings/billing/methods/{raast.pk}/status/")
    raast.refresh_from_db()
    assert not raast.is_active


def _ward_admission(req, patient, doctor):
    from apps.ipd.tests.helpers import admit, make_ward

    _, _, beds = make_ward(req.organization, req.branch)
    return admit(req, patient, doctor, beds[0])


def test_day_totals_count_deposits_once_and_returns_as_money_out(
    bill_request, patient, doctor, cash, card
):
    from apps.ipd import services as ipd

    adm = _ward_admission(bill_request, patient, doctor)
    ipd.record_deposit(adm, request=bill_request, method=cash, amount="3000")
    ipd.record_deposit(adm, request=bill_request, method=card, amount="2000", reference="T9")
    t = services.day_totals(bill_request.branch)
    assert (t["collected"], t["net"], t["count"]) == (D("5000"), D("5000"), 0)
    assert t["deposits"]["received_count"] == 2
    assert t["by_method"]["Cash"]["collected"] == D("3000")
    assert t["by_method"]["Card"]["deposits"] == 1

    # Bill = 3000 + 1000 for one bed-day.
    ipd.add_charge(adm, request=bill_request, description="Dressing", unit_price=3000)
    ipd.finalize_bill(adm, request=bill_request)
    assert Payment.objects.filter(invoice=adm.invoice).count() == 2  # deposits applied as payments
    t = services.day_totals(bill_request.branch)
    assert (t["collected"], t["count"]) == (D("5000"), 0)  # applied payments not counted again

    assert ipd.return_deposit_balance(adm, request=bill_request) == D("1000.00")
    t = services.day_totals(bill_request.branch)
    assert (t["collected"], t["refunded"], t["net"]) == (D("5000"), D("1000"), D("4000"))
    assert t["deposits"]["returned"] == D("1000") and t["deposits"]["returned_count"] == 1
    assert t["by_method"]["Card"]["returned"] == D("1000")
    assert t["by_method"]["Card"]["net"] == D("1000")


def test_day_totals_deposits_stay_in_their_branch(bill_request, patient, doctor, cash, branch2):
    from apps.ipd import services as ipd

    adm = _ward_admission(bill_request, patient, doctor)
    ipd.record_deposit(adm, request=bill_request, method=cash, amount="3000")
    assert services.day_totals(branch2)["collected"] == D("0")
    assert services.day_totals(bill_request.branch)["collected"] == D("3000")


def test_payment_list_shows_deposits_and_marks_applied_payments(
    client, admin_user, bill_request, patient, doctor, cash
):
    from apps.ipd import services as ipd

    adm = _ward_admission(bill_request, patient, doctor)
    dep = ipd.record_deposit(adm, request=bill_request, method=cash, amount="3000")
    # Bill = 500 + 1000 for one bed-day.
    ipd.add_charge(adm, request=bill_request, description="Dressing", unit_price=500)
    ipd.finalize_bill(adm, request=bill_request)
    ipd.return_deposit_balance(adm, request=bill_request)
    client.force_login(admin_user)
    r = client.get("/payments/")
    body = r.content.decode()
    assert r.status_code == 200
    assert "Inpatient deposits received" in body and dep.number in body
    assert "Deposit balances returned" in body and "−1,500.00" in body
    assert "Deposit applied · counted when taken" in body
    assert r.context["totals"]["net"] == D("1500")
