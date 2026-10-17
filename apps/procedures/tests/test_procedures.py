from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.billing import services as billing
from apps.billing.models import Invoice, InvoiceItem
from apps.masterdata.models import Service, ServiceCategory
from apps.procedures import services
from apps.procedures.models import PatientProcedure

D = Decimal


@pytest.fixture
def category(org):
    return ServiceCategory.objects.create(organization=org, name="Procedures")


@pytest.fixture
def ecg(org, category):
    return Service.objects.create(
        organization=org,
        category=category,
        code="ECG",
        name="ECG",
        price=D("1500"),
        is_procedure=True,
    )


@pytest.fixture
def fixed(org, category):
    return Service.objects.create(
        organization=org,
        category=category,
        code="ECHO",
        name="ECHO",
        price=D("4000"),
        is_procedure=True,
        price_editable=False,
    )


@pytest.fixture
def doctor_req(make_request, doctor):
    return make_request(doctor.user)


def test_order_issues_invoice_with_procedure_line_and_doctor(doctor_req, patient, ecg, doctor):
    proc = services.order(request=doctor_req, patient=patient, service=ecg, performed_by=doctor)
    assert proc.number.startswith("PRC-")
    assert proc.unit_price == proc.list_price == D("1500.00")
    inv = proc.invoice
    assert inv.status == Invoice.Status.UNPAID and inv.grand_total == D("1500.00")
    line = inv.items.get()
    assert line.kind == InvoiceItem.Kind.PROCEDURE
    assert line.performed_by == doctor.user
    assert line.service == ecg


def test_dental_price_changed_per_patient_keeps_list_price(doctor_req, patient, ecg):
    proc = services.order(
        request=doctor_req,
        patient=patient,
        service=ecg,
        quantity=2,
        unit_price="1200",
        price_reason="Senior citizen",
        notes="tooth 36",
    )
    assert proc.list_price == D("1500.00") and proc.unit_price == D("1200.00")
    assert proc.price_changed and proc.total == D("2400.00")
    assert proc.invoice.grand_total == D("2400.00")
    assert "tooth 36" in proc.invoice.items.get().description


def test_price_change_needs_reason(doctor_req, patient, ecg):
    with pytest.raises(services.ProcedureError, match="reason"):
        services.order(request=doctor_req, patient=patient, service=ecg, unit_price="1000")


def test_price_change_needs_permission(make_request, make_user, patient, ecg):
    req = make_request(make_user("reception"))
    assert "procedure.order" in req.sd_perms
    with pytest.raises(services.ProcedureError, match="not allowed"):
        services.order(
            request=req, patient=patient, service=ecg, unit_price="1000", price_reason="x"
        )
    services.order(request=req, patient=patient, service=ecg)  # list price is fine


def test_fixed_price_procedure_refuses_change(doctor_req, patient, fixed):
    with pytest.raises(services.ProcedureError, match="fixed"):
        services.order(
            request=doctor_req,
            patient=patient,
            service=fixed,
            unit_price="3000",
            price_reason="discount",
        )


def test_branch_price_is_the_list_price(doctor_req, patient, ecg, branch):
    from apps.masterdata.models import ServiceBranchPrice

    ServiceBranchPrice.objects.create(service=ecg, branch=branch, price=D("1700"))
    proc = services.order(request=doctor_req, patient=patient, service=ecg)
    assert proc.list_price == D("1700.00")


def test_non_procedure_service_is_refused(doctor_req, patient, org, category):
    svc = Service.objects.create(
        organization=org, category=category, code="DRS", name="Dressing", price=D("300")
    )
    with pytest.raises(services.ProcedureError):
        services.order(request=doctor_req, patient=patient, service=svc)


def test_cancel_voids_unpaid_invoice_but_not_paid(
    doctor_req, patient, ecg, admin_user, make_request, cash
):
    proc = services.order(request=doctor_req, patient=patient, service=ecg)
    services.cancel(proc, request=doctor_req, reason="Patient left")
    proc.refresh_from_db()
    assert proc.status == PatientProcedure.Status.CANCELLED
    assert proc.invoice.status == Invoice.Status.CANCELLED

    paid = services.order(request=doctor_req, patient=patient, service=ecg)
    billing.receive_payment(
        paid.invoice, request=make_request(admin_user), method=cash, amount=D("1500")
    )
    with pytest.raises(services.ProcedureError, match="Refund"):
        services.cancel(paid, request=doctor_req, reason="Mistake")


def test_complete_once(doctor_req, patient, ecg):
    proc = services.order(request=doctor_req, patient=patient, service=ecg)
    services.complete(proc, request=doctor_req, findings="Sinus rhythm")
    proc.refresh_from_db()
    assert proc.status == PatientProcedure.Status.COMPLETED and proc.findings == "Sinus rhythm"
    with pytest.raises(services.ProcedureError):
        services.complete(proc, request=doctor_req)


def test_views_order_and_detail(client, doctor, patient, ecg):
    client.force_login(doctor.user)
    page = client.get(reverse("procedures:procedure-create") + f"?patient={patient.pk}")
    assert page.status_code == 200 and b"ECG" in page.content
    resp = client.post(
        reverse("procedures:procedure-create"),
        {
            "patient": patient.pk,
            "service": ecg.pk,
            "performed_by": doctor.pk,
            "quantity": 1,
            "unit_price": "1000",
            "price_reason": "Staff family",
        },
    )
    proc = PatientProcedure.objects.get()
    assert resp.status_code == 302 and resp.url == reverse(
        "procedures:procedure-detail", args=[proc.pk]
    )
    assert proc.unit_price == D("1000.00")
    assert AuditLog.objects.filter(action="procedure.price_change").exists()
    assert client.get(reverse("procedures:procedure-list")).status_code == 200
    assert client.get(resp.url).status_code == 200


def test_order_from_consultation(client, doctor, patient, ecg, make_request):
    from apps.encounters import services as encounters

    encounter, _ = encounters.start(request=make_request(doctor.user), patient=patient)
    client.force_login(doctor.user)
    page = client.get(reverse("encounters:detail", args=[encounter.pk]))
    assert b"sec-procedures" in page.content
    client.post(
        reverse("procedures:procedure-encounter-order", args=[encounter.pk]),
        {"service": ecg.pk, "performed_by": doctor.pk, "quantity": 1},
    )
    proc = PatientProcedure.objects.get()
    assert proc.encounter == encounter and proc.performed_by == doctor


def test_other_branch_cannot_open_procedure(client, doctor_req, patient, ecg, branch2, make_user):
    proc = services.order(request=doctor_req, patient=patient, service=ecg)
    other = make_user("doctor", branch=branch2)
    client.force_login(other)
    assert client.get(reverse("procedures:procedure-detail", args=[proc.pk])).status_code == 404
