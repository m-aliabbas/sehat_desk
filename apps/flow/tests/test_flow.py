"""Patient flow: the reception gate, routing after the doctor, the pull-by-token fail-safe, one
pharmacy bill for medicine and fees, and the lab's unpaid-fee guard."""

from datetime import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.appointments import visits
from apps.audit.models import AuditLog
from apps.billing.models import Invoice, InvoiceItem
from apps.flow import services
from apps.flow.models import CounterLocation, PatientBillItem, VisitState
from apps.masterdata.models import DoctorSchedule, Service, ServiceCategory
from apps.organizations.models import preferences_for
from apps.pharmacy import pos
from apps.queue import services as queue
from apps.queue.models import QueueToken

pytestmark = pytest.mark.django_db


@pytest.fixture
def clinic(doctor, branch):
    DoctorSchedule.objects.filter(doctor=doctor).delete()
    for day in range(7):
        DoctorSchedule.objects.create(
            doctor=doctor, branch=branch, weekday=day, start_time=time(0), end_time=time(23, 45)
        )
    return doctor


@pytest.fixture
def desk(make_user, make_request):
    return make_request(make_user("reception", username="frontdesk"))


@pytest.fixture
def doc_req(doctor, make_request):
    return make_request(doctor.user)


@pytest.fixture
def pharmacist(make_user, make_request):
    return make_request(make_user("pharmacy", username="pharma"))


@pytest.fixture
def lab_tech(make_user, make_request):
    return make_request(make_user("lab", username="labtech"))


def _settings(org, **values):
    row = services.flow_settings_for(org)
    for key, value in values.items():
        setattr(row, key, value)
    row.save()
    return row


def _visit(req, patient, doctor, **kwargs):
    v = visits.register_visit(
        request=req, patient=patient, doctor=doctor, on_date=timezone.localdate(), **kwargs
    )
    return services.visit_for_token(QueueToken.objects.get(pk=v.token.pk)), v


def _fresh(visit):
    return type(visit).objects.get(pk=visit.pk)


def _see_and_finish(doc_req, doctor, token):
    """The doctor calls, sees and closes the token with no consultation note (paper Rx)."""
    token = queue.call(token, request=doc_req, doctor=doctor)
    token = queue.start(token)
    return queue.complete(token)


def _service(org, name, billed_at, price="300"):
    cat, _ = ServiceCategory.objects.get_or_create(organization=org, name="Clinic")
    return Service.objects.create(
        organization=org,
        category=cat,
        code=name[:6].upper(),
        name=name,
        price=Decimal(price),
        billed_at=billed_at,
    )


# --- Reception gate ----------------------------------------------------------------------------


def test_unpaid_reception_fee_holds_the_token_until_paid(desk, doc_req, clinic, patient, cash):
    visit, v = _visit(desk, patient, clinic)
    assert visit.state == VisitState.TOKEN_ISSUED
    assert visit.print_blank_rx is preferences_for(desk.organization).opd_print_blank_prescription
    item = visit.bill_items.get()
    assert (item.billed_at, item.is_paid, item.invoice_id) == ("RECEPTION", False, v.invoice.pk)

    mine = queue.queue_for(desk.branch, doctor=clinic)
    assert not queue.waiting(mine).exists()
    with pytest.raises(queue.QueueError, match="not paid at reception"):
        queue.call(v.token, request=doc_req, doctor=clinic)

    visits.collect_fee(v.invoice, request=desk, method=cash)
    visit = _fresh(visit)
    assert visit.state == VisitState.IN_QUEUE
    assert visit.bill_items.get().is_paid
    assert queue.waiting(mine).get() == v.token


def test_paid_at_the_desk_goes_straight_into_the_queue(desk, clinic, patient, cash):
    visit, _ = _visit(desk, patient, clinic, collect=True, method=cash)
    assert _fresh(visit).state == VisitState.IN_QUEUE


def test_emergency_is_seen_first_and_pays_later(desk, clinic, patient):
    visit, _ = _visit(desk, patient, clinic, emergency=True)
    assert visit.is_emergency and visit.state == VisitState.IN_QUEUE
    assert visit.bill_items.get().is_paid is False


def test_state_is_protected_from_direct_writes(desk, clinic, patient):
    visit, _ = _visit(desk, patient, clinic)
    with pytest.raises(AttributeError):
        visit.state = VisitState.COMPLETED


# --- After the doctor ---------------------------------------------------------------------------


def test_paper_rx_goes_to_pharmacy_or_closes(desk, doc_req, clinic, patient, cash):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    _see_and_finish(doc_req, clinic, v.token)
    visit = _fresh(visit)
    assert visit.state == VisitState.PHARMACY_QUEUE
    assert visit.is_manual_rx_flagged and not visit.digital_rx_generated


def test_pharmacy_off_for_paper_rx_closes_the_visit(desk, doc_req, clinic, patient, cash):
    _settings(desk.organization, pharmacy_handles_manual_rx=False)
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    _see_and_finish(doc_req, clinic, v.token)
    assert _fresh(visit).state == VisitState.COMPLETED


# --- Pharmacy: one bill for medicine and fees --------------------------------------------------


def test_fee_at_pharmacy_is_billed_with_the_sale(
    client, desk, doc_req, pharmacist, clinic, patient, cash
):
    _settings(desk.organization, consultation_fee_billed_at=CounterLocation.PHARMACY)
    visit, v = _visit(desk, patient, clinic)
    assert v.invoice is None  # reception takes nothing
    assert visit.state == VisitState.IN_QUEUE
    injection = _service(desk.organization, "Injection fee", CounterLocation.PHARMACY, "200")
    services.add_charge(request=desk, visit=visit, service=injection)

    _see_and_finish(doc_req, clinic, v.token)
    assert _fresh(visit).state == VisitState.PHARMACY_QUEUE

    cart = pos.attach_patient(pos.open_cart(pharmacist), request=pharmacist, patient=patient)
    t = pos.totals(cart)
    assert t["dues_total"] == clinic.consultation_fee + Decimal("200")
    assert t["grand_total"] == t["dues_total"]
    assert not pos.checkout_problems(cart)

    sale = pos.checkout(
        cart, request=pharmacist, payments=[{"method": cash, "amount": t["grand_total"]}]
    )
    invoice = sale.invoice
    assert invoice.status == Invoice.Status.PAID
    assert set(invoice.items.values_list("kind", flat=True)) == {
        InvoiceItem.Kind.CONSULTATION,
        InvoiceItem.Kind.SERVICE,
    }
    assert invoice.items.get(kind=InvoiceItem.Kind.CONSULTATION).performed_by == clinic.user
    items = PatientBillItem.objects.filter(visit=visit)
    assert all(i.is_paid and i.collected_by_counter == "PHARMACY" for i in items)
    assert _fresh(visit).state == VisitState.COMPLETED

    # The receipt and the sale screen list the fees under the medicine, so they add up.
    client.force_login(pharmacist.user)
    for url in (f"/pharmacy/sales/{sale.pk}/receipt/", f"/pharmacy/sales/{sale.pk}/"):
        body = client.get(url).content.decode()
        assert f"Consultation · {clinic}" in body and "Injection fee" in body
        assert "Procedure fee" not in body and "Consultation fee" in body


# --- Fail-safe: take a patient by token ---------------------------------------------------------


def test_pharmacy_takes_a_patient_the_doctor_did_not_send_on(
    desk, doc_req, pharmacist, clinic, patient, cash
):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    queue.start(queue.call(v.token, request=doc_req, doctor=clinic))
    assert _fresh(visit).state == VisitState.WITH_DOCTOR

    pulled = services.pull_by_token(request=pharmacist, label=v.token.label.lower(), to="PHARMACY")
    assert pulled.state == VisitState.PHARMACY_QUEUE
    assert pulled.pulled_by == pharmacist.user and pulled.is_manual_rx_flagged
    assert QueueToken.objects.get(pk=v.token.pk).status == QueueToken.Status.COMPLETED
    assert queue.current_for(desk.branch, clinic) is None  # the doctor can call the next one
    assert AuditLog.objects.filter(action="flow.pull").exists()


def test_pull_needs_the_counter_permission_and_a_paid_visit(
    desk, pharmacist, lab_tech, clinic, patient
):
    _, v = _visit(desk, patient, clinic)
    with pytest.raises(services.FlowError, match="not allowed"):
        services.pull_by_token(request=lab_tech, label=v.token.label, to="PHARMACY")
    with pytest.raises(services.FlowError, match="not paid at reception"):
        services.pull_by_token(request=pharmacist, label=v.token.label, to="PHARMACY")


def test_signing_moves_the_patient_when_the_doctor_need_not_click(
    desk, doc_req, clinic, patient, cash
):
    _settings(desk.organization, doctor_must_click_next=False)
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    queue.start(queue.call(v.token, request=doc_req, doctor=clinic))
    visit = _fresh(visit)
    assert visit.state == VisitState.WITH_DOCTOR
    assert visit.auto_next_allowed()
    _settings(desk.organization, doctor_must_click_next=True)
    desk.organization._sd_flow_settings = None
    visit = _fresh(visit)
    visit.organization._sd_flow_settings = None
    assert not visit.auto_next_allowed()


# --- Lab guard ----------------------------------------------------------------------------------


def test_lab_is_blocked_while_any_lab_fee_is_unpaid(desk, clinic, patient, cash):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    blood = _service(desk.organization, "Blood draw", CounterLocation.CENTRAL_CASH)
    blood_lab = _service(desk.organization, "Lab handling", CounterLocation.LAB)
    services.add_charge(request=desk, visit=visit, service=blood)  # not a lab fee
    services.add_charge(request=desk, visit=visit, service=blood_lab)
    order = SimpleNamespace(encounter_id=None, patient=patient, branch=desk.branch)

    with pytest.raises(services.FlowError, match="Lab handling"):
        services.lab_guard(order)

    invoice = services.counter_invoice(request=desk, visit=visit, counter="LAB")
    assert invoice.status == Invoice.Status.UNPAID
    with pytest.raises(services.FlowError):
        services.lab_guard(order)  # billed is not paid
    visits.collect_fee(invoice, request=desk, method=cash)
    services.lab_guard(order)


def test_emergency_lab_work_is_never_blocked(desk, clinic, patient):
    visit, _ = _visit(desk, patient, clinic, emergency=True)
    services.add_charge(
        request=desk, visit=visit, service=_service(desk.organization, "Lab fee", "LAB")
    )
    services.lab_guard(SimpleNamespace(encounter_id=None, patient=patient, branch=desk.branch))


# --- Screens ------------------------------------------------------------------------------------


def test_settings_screen_saves_and_keeps_preferences_in_step(admin_client, org):
    assert admin_client.get("/settings/patient-flow/").status_code == 200
    res = admin_client.post(
        "/settings/patient-flow/",
        {
            "consultation_fee_billed_at": "PHARMACY",
            "rx_print_at": "DOCTOR",
            "doctor_must_click_next": "on",
            "pharmacy_handles_manual_rx": "on",
        },
    )
    assert res.status_code == 302
    row = services.HospitalFlowSetting.objects.get(organization=org)
    assert row.consultation_fee_billed_at == "PHARMACY" and not row.doctor_generates_digital_rx
    assert row.rx_print_at == "DOCTOR"
    prefs = type(preferences_for(org)).objects.get(organization=org)
    assert not prefs.opd_collect_fee_upfront and not prefs.opd_print_blank_prescription


def test_settings_screen_is_admin_only(client, make_user):
    client.force_login(make_user("reception", username="r1"))
    assert client.get("/settings/patient-flow/").status_code == 403


def test_board_lists_todays_visits(client, make_user, desk, clinic, patient):
    _, v = _visit(desk, patient, clinic)
    client.force_login(make_user("reception", username="r2"))
    page = client.get("/flow/")
    assert page.status_code == 200
    body = page.content.decode()
    assert v.token.label in body and "Bill at" not in body  # already on the fee invoice


# --- Printing the doctor's prescription -------------------------------------------------------


def _signed_rx_visit(client, desk, doc_req, clinic, patient, cash):
    """Paid visit, the doctor writes one medicine and presses Checked · Next (follow=True)."""
    from apps.encounters import services as encounters
    from apps.prescriptions import services as rx_services
    from apps.prescriptions.models import Medicine

    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    token = queue.call(v.token, request=doc_req, doctor=clinic)
    encounter, _ = encounters.start(request=doc_req, patient=patient, token=token)
    medicine = Medicine.objects.create(
        organization=desk.organization, brand_name="Panadol", generic_name="Paracetamol"
    )
    rx_services.add_item(encounter, request=doc_req, medicine=medicine, data={"dose": "1"})
    client.force_login(clinic.user)
    page = client.post(f"/queue/tokens/{token.pk}/complete-next/", follow=True)
    return page, rx_services.prescription_for(encounter)


@pytest.mark.parametrize(
    ("where", "at_desk", "at_pharmacy"),
    [("NONE", False, False), ("DOCTOR", True, False), ("PHARMACY", False, True),
     ("BOTH", True, True)],
)  # fmt: skip
def test_prescription_prints_where_the_hospital_chose(
    client, make_user, desk, doc_req, clinic, patient, cash, where, at_desk, at_pharmacy
):
    _settings(desk.organization, rx_print_at=where)
    page, rx = _signed_rx_visit(client, desk, doc_req, clinic, patient, cash)
    assert page.status_code == 200 and rx.sent_to_pharmacy_at
    assert ("data-pending-print" in page.content.decode()) is at_desk
    # The next page does not print it again.
    assert "data-pending-print" not in client.get("/queue/doctor/").content.decode()

    client.force_login(make_user("pharmacy", username="pharma2"))
    first = client.get(f"/pharmacy/dispense/{rx.pk}/").content.decode()
    again = client.get(f"/pharmacy/dispense/{rx.pk}/").content.decode()
    assert ("data-pending-print" in first) is at_pharmacy
    assert "data-pending-print" not in again  # once per prescription at the pharmacy
    rx.refresh_from_db()
    assert bool(rx.pharmacy_printed_at) is at_pharmacy


def test_print_page_prints_itself_inside_a_frame(client, desk, doc_req, clinic, patient, cash):
    _, rx = _signed_rx_visit(client, desk, doc_req, clinic, patient, cash)
    res = client.get(f"/prescriptions/{rx.pk}/print/?print=1")
    assert res["X-Frame-Options"] == "SAMEORIGIN"
    assert "window.print()" in res.content.decode()
    assert "window.print());" not in client.get(f"/prescriptions/{rx.pk}/print/").content.decode()
