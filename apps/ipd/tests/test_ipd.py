from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.billing import services as billing
from apps.billing.models import Invoice, PaymentMethod
from apps.ipd import services
from apps.ipd.models import Admission, Bed, BedOccupancy, DischargeSummary
from apps.masterdata.models import Service, ServiceCategory
from apps.patients.services import register_patient

from .helpers import admit, make_request, make_ward


@pytest.fixture
def req(rf, admin_user, org, branch):
    return make_request(rf, admin_user, org, branch)


@pytest.fixture
def ward(org, branch):
    return make_ward(org, branch)


@pytest.fixture
def beds(ward):
    return ward[2]


@pytest.fixture
def patient2(org, branch, admin_user):
    return register_patient(
        organization=org,
        branch=branch,
        user=admin_user,
        data={"full_name": "Bilal Hussain", "gender": "male", "phone": "03007654321"},
    )


@pytest.fixture
def cash(org):
    billing.ensure_payment_methods(org)
    return PaymentMethod.objects.get(organization=org, code="cash")


def test_add_beds_skips_taken_labels(ward):
    _, room, beds = ward
    assert [b.code for b in beds] == ["GW-01", "GW-02", "GW-03"]
    more = services.add_beds(room, prefix="gw", count=2)
    assert [b.code for b in more] == ["GW-04", "GW-05"]


def test_admit_occupies_bed_and_opens_draft_invoice(req, patient, doctor, beds):
    adm = admit(req, patient, doctor, beds[0], attendant={"name": "Ali Khan", "phone": "0300"})
    beds[0].refresh_from_db()
    assert adm.number.startswith("ADM-")
    assert adm.status == Admission.Status.ADMITTED and adm.current_bed == beds[0]
    assert beds[0].status == Bed.Status.OCCUPIED
    assert adm.invoice.status == Invoice.Status.DRAFT and adm.invoice.patient == patient
    assert adm.occupancies.get().daily_rate == Decimal("1000")


def test_double_bed_allocation_is_prevented(req, patient, patient2, doctor, beds):
    admit(req, patient, doctor, beds[0])
    with pytest.raises(services.IpdError, match="occupied"):
        admit(req, patient2, doctor, beds[0])
    # The database refuses a second open occupancy even if the service checks were bypassed.
    other = Admission.objects.create(
        organization=req.organization,
        branch=req.branch,
        number="X-1",
        patient=patient2,
        admitting_doctor=doctor,
        department=doctor.department,
        admission_reason="x",
        created_by=req.user,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        BedOccupancy.objects.create(bed=beds[0], admission=other)


def test_patient_cannot_have_two_open_admissions(req, patient, doctor, beds):
    admit(req, patient, doctor, beds[0])
    with pytest.raises(services.IpdError, match="already admitted"):
        admit(req, patient, doctor, beds[1])


def test_admission_needs_ipd_permission(rf, make_user, org, branch, patient, doctor, beds):
    cashier = make_request(rf, make_user("cashier"), org, branch)
    with pytest.raises(services.IpdError, match="not allowed"):
        admit(cashier, patient, doctor, beds[0])


def test_bed_and_ward_transfer(req, org, branch, patient, doctor, beds):
    adm = admit(req, patient, doctor, beds[0])
    move = services.transfer(adm, request=req, to_bed=beds[1], reason="Closer to the station")
    assert move.kind == "bed"
    _, _, icu_beds = make_ward(org, branch, code="ICU", beds=1, rate="5000")
    move2 = services.transfer(adm, request=req, to_bed=icu_beds[0], reason="Needs ICU care")
    assert move2.kind == "ward"
    adm.refresh_from_db()
    assert adm.current_bed == icu_beds[0]
    statuses = dict(Bed.objects.values_list("code", "status"))
    assert statuses["GW-01"] == "cleaning" and statuses["GW-02"] == "cleaning"
    assert statuses["ICU-01"] == "occupied"
    assert adm.occupancies.filter(ended_at__isnull=True).count() == 1
    assert adm.occupancies.count() == 3 and adm.transfers.count() == 2
    with pytest.raises(services.IpdError, match="cleaning"):
        services.transfer(adm, request=req, to_bed=beds[0], reason="back")


def test_bed_status_rules(req, patient, doctor, beds):
    admit(req, patient, doctor, beds[0])
    with pytest.raises(services.IpdError, match="occupied"):
        services.set_bed_status(beds[0], request=req, status="available")
    with pytest.raises(services.IpdError, match="note"):
        services.set_bed_status(beds[1], request=req, status="maintenance")
    services.set_bed_status(beds[1], request=req, status="maintenance", note="Broken rail")
    stats = services.occupancy(Bed.objects.filter(pk__in=[b.pk for b in beds]))
    assert stats["occupied"] == 1 and stats["usable"] == 2 and stats["percent"] == 50


def test_deposit_is_idempotent_and_needs_collect_permission(
    rf, make_user, org, branch, req, patient, doctor, beds, cash
):
    adm = admit(req, patient, doctor, beds[0])
    d1 = services.record_deposit(adm, request=req, method=cash, amount="5000", idempotency_key="k1")
    d2 = services.record_deposit(adm, request=req, method=cash, amount="5000", idempotency_key="k1")
    assert d1.pk == d2.pk and adm.deposits.count() == 1
    nurse = make_request(rf, make_user("nurse"), org, branch)
    with pytest.raises(services.IpdError, match="not allowed"):
        services.record_deposit(adm, request=nurse, method=cash, amount="100")


def _service(org, price="2500"):
    cat = ServiceCategory.objects.create(organization=org, name="Procedures")
    return Service.objects.create(
        organization=org, category=cat, name="Dressing", code="DRS", price=Decimal(price)
    )


def test_final_bill_posts_bed_days_and_applies_deposit(req, org, patient, doctor, beds, cash):
    adm = admit(req, patient, doctor, beds[0], admitted_at=timezone.now() - timedelta(days=2))
    BedOccupancy.objects.filter(admission=adm).update(started_at=adm.admitted_at)
    services.add_charge(adm, request=req, service=_service(org), quantity=1)
    services.record_deposit(adm, request=req, method=cash, amount="10000")
    assert services.billing_summary(adm)["bed_estimate"] == Decimal("2000")
    services.finalize_bill(adm, request=req)
    inv = Invoice.objects.get(pk=adm.invoice_id)
    assert inv.grand_total == Decimal("4500.00")  # 2 bed-days × 1000 + 2500 dressing
    assert inv.status == Invoice.Status.PAID and inv.payments.get().reference.startswith("DEP-")
    dep = adm.deposits.get()
    assert dep.applied_amount == Decimal("4500.00") and dep.unapplied == Decimal("5500.00")
    with pytest.raises(services.IpdError, match="finalized"):
        services.add_charge(adm, request=req, description="Late", unit_price=10)
    assert services.return_deposit_balance(adm, request=req) == Decimal("5500.00")


def test_deposit_counts_on_the_day_taken_and_return_on_the_day_returned(
    req, org, patient, doctor, beds, cash
):
    from apps.ipd.models import AdmissionDeposit

    adm = admit(req, patient, doctor, beds[0])
    services.add_charge(adm, request=req, service=_service(org), quantity=1)
    dep = services.record_deposit(adm, request=req, method=cash, amount="10000")
    two_days_ago = timezone.now() - timedelta(days=2)
    AdmissionDeposit.objects.filter(pk=dep.pk).update(created_at=two_days_ago)
    services.finalize_bill(adm, request=req)
    services.return_deposit_balance(adm, request=req)

    then = billing.day_totals(req.branch, timezone.localdate(two_days_ago))
    assert then["collected"] == Decimal("10000") and then["refunded"] == Decimal("0")
    assert then["by_method"]["Cash"]["deposits"] == 1
    today = billing.day_totals(req.branch)
    # The deposit applied to the bill today is not cash in again; the returned balance is out.
    assert today["collected"] == Decimal("0") and today["count"] == 0
    assert today["refunded"] == Decimal("6500.00") and today["net"] == Decimal("-6500.00")
    assert today["by_method"]["Cash"]["returned"] == Decimal("6500.00")


def test_discharge_workflow_signs_summary_and_frees_bed(req, patient, doctor, beds, cash):
    adm = admit(req, patient, doctor, beds[0])
    with pytest.raises(services.IpdError, match="diagnosis"):
        services.plan_discharge(adm, request=req)
    services.save_discharge_draft(
        adm,
        request=req,
        data={
            "discharge_diagnosis": "Typhoid fever",
            "discharge_condition": "improved",
            "discharge_medicines": [{"medicine": "Cefixime", "dose": "400 mg"}],
        },
    )
    services.plan_discharge(adm, request=req)
    with pytest.raises(services.IpdError, match="Finalize"):
        services.release(adm, request=req)
    services.finalize_bill(adm, request=req)  # 1 bed-day, unpaid
    with pytest.raises(services.IpdError, match="reason"):
        services.release(adm, request=req)  # super admin may override, but needs a reason
    services.release(adm, request=req, override_reason="Paid by employer next week")
    adm.refresh_from_db()
    beds[0].refresh_from_db()
    assert adm.status == Admission.Status.DISCHARGED and beds[0].status == Bed.Status.CLEANING
    v1 = adm.summary_versions.get()
    assert v1.version == 1 and v1.content["discharge_diagnosis"] == "Typhoid fever"
    v2 = services.amend_summary(
        adm, request=req, data={"discharge_diagnosis": "Enteric fever"}, reason="Wording"
    )
    assert v2.version == 2
    assert DischargeSummary.objects.get(pk=v1.pk).content["discharge_diagnosis"] == "Typhoid fever"


def test_outstanding_bill_blocks_release_without_override(
    rf, make_user, org, branch, req, patient, doctor, beds
):
    adm = admit(req, patient, doctor, beds[0])
    services.save_discharge_draft(adm, request=req, data={"discharge_diagnosis": "x"})
    services.plan_discharge(adm, request=req)
    services.finalize_bill(adm, request=req)
    nurse = make_request(rf, make_user("nurse"), org, branch)
    with pytest.raises(services.IpdError, match="unpaid"):
        services.release(adm, request=nurse)


def test_cancel_admission_frees_bed_and_voids_bill(req, patient, doctor, beds):
    adm = admit(req, patient, doctor, beds[0])
    services.cancel_admission(adm, request=req, reason="Wrong patient")
    beds[0].refresh_from_db()
    adm.refresh_from_db()
    assert adm.status == Admission.Status.CANCELLED and beds[0].status == Bed.Status.AVAILABLE
    assert adm.invoice.status == Invoice.Status.CANCELLED


def test_medication_orders_and_rounds(req, patient, doctor, beds, make_user, rf, org, branch):
    adm = admit(req, patient, doctor, beds[0])
    order = services.order_medication(
        adm,
        request=req,
        data={"medicine_name": "Paracetamol 500 mg", "dose": "1 g", "frequency": "TDS"},
    )
    services.stop_medication(order, request=req, reason="Afebrile")
    order.refresh_from_db()
    assert order.status == "stopped"
    doc_req = make_request(rf, doctor.user, org, branch)
    note = services.add_round(adm, request=doc_req, findings="Afebrile, eating well")
    services.mark_round_error(note, request=doc_req, reason="Wrong patient")
    note.refresh_from_db()
    assert note.entered_in_error and note.findings == "Afebrile, eating well"


# --- Views -------------------------------------------------------------------------------------


def test_board_admit_and_every_chart_tab_render(admin_client, req, patient, doctor, beds):
    assert admin_client.get(reverse("ipd:board")).status_code == 200
    response = admin_client.post(
        f"{reverse('ipd:admit')}?patient={patient.pk}",
        {
            "patient": patient.pk,
            "doctor": doctor.pk,
            "department": doctor.department_id,
            "bed": beds[0].pk,
            "admission_reason": "Chest pain",
            "deposit_amount": "",
        },
    )
    adm = Admission.objects.get(patient=patient)
    assert response.status_code == 302 and response["Location"].endswith(
        f"/ipd/admissions/{adm.pk}/"
    )
    assert AuditLog.objects.filter(action="ipd.admit", record_id=str(adm.pk)).exists()
    for tab in ["overview", "rounds", "nursing", "vitals", "medication", "orders", "billing",
                "transfers", "documents"]:  # fmt: skip
        r = admin_client.get(reverse("ipd:admission-detail", args=[adm.pk]), {"tab": tab})
        assert r.status_code == 200, tab
    for name in ["ipd:discharge", "ipd:admission-transfer", "ipd:summary", "ipd:admission-print"]:
        assert admin_client.get(reverse(name, args=[adm.pk])).status_code == 200, name
    assert admin_client.get(reverse("ipd:admission-list")).status_code == 200
    assert admin_client.get(reverse("ipd:setup")).status_code == 200
    assert admin_client.get(reverse("ipd:bed-detail", args=[beds[0].pk])).status_code == 200


def test_discharge_view_steps(admin_client, req, patient, doctor, beds):
    adm = admit(req, patient, doctor, beds[0])
    url = reverse("ipd:discharge", args=[adm.pk])
    admin_client.post(
        url,
        {
            "action": "save",
            "plan": "1",
            "discharge_type": "routine",
            "discharge_diagnosis": "Gastroenteritis",
            "med_name": ["ORS"],
            "med_dose": ["1 sachet"],
            "med_frequency": ["TDS"],
            "med_duration": ["3 days"],
            "med_instructions": [""],
        },
    )
    adm.refresh_from_db()
    assert adm.status == "discharge_planned" and adm.discharge_medicines[0]["medicine"] == "ORS"
    admin_client.post(url, {"action": "finalize"})
    admin_client.post(url, {"action": "release", "override": "Corporate panel"})
    adm.refresh_from_db()
    assert adm.status == "discharged"
    assert admin_client.get(reverse("ipd:summary", args=[adm.pk])).status_code == 200


def test_ipd_is_scoped_and_permission_checked(client, make_user, req, patient, doctor, beds,
                                              org, branch2):  # fmt: skip
    adm = admit(req, patient, doctor, beds[0])
    cashier = make_user("cashier")
    client.force_login(cashier)
    assert client.get(reverse("ipd:board")).status_code == 403
    nurse = make_user("nurse", branch=branch2)
    client.force_login(nurse)
    # A nurse of another branch cannot open this branch's chart (404, not revealed).
    assert client.get(reverse("ipd:admission-detail", args=[adm.pk])).status_code == 404
    assert client.get(reverse("ipd:setup")).status_code == 403
