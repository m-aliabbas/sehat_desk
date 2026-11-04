"""Patient flow: the reception gate, routing after the doctor, the pull-by-token fail-safe, one
pharmacy bill for medicine and fees, and the lab's unpaid-fee guard."""

from datetime import time, timedelta
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


def _order_for(token, priority="routine"):
    """Enough of a LabOrder for the guard: one written in this token's consultation."""
    encounter = SimpleNamespace(token_id=token.pk, token=token)
    return SimpleNamespace(
        encounter_id=1, encounter=encounter, priority=priority, number="LAB-T", pk=0
    )


def test_lab_is_blocked_while_any_lab_fee_is_unpaid(desk, clinic, patient, cash):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    blood = _service(desk.organization, "Blood draw", CounterLocation.CENTRAL_CASH)
    blood_lab = _service(desk.organization, "Lab handling", CounterLocation.LAB)
    services.add_charge(request=desk, visit=visit, service=blood)  # not a lab fee
    services.add_charge(request=desk, visit=visit, service=blood_lab)
    order = _order_for(v.token)

    with pytest.raises(services.FlowError, match="Lab handling"):
        services.lab_guard(order)

    invoice = services.counter_invoice(request=desk, visit=visit, counter="LAB")
    assert invoice.status == Invoice.Status.UNPAID
    with pytest.raises(services.FlowError):
        services.lab_guard(order)  # billed is not paid
    visits.collect_fee(invoice, request=desk, method=cash)
    services.lab_guard(order)


def test_emergency_lab_work_is_never_blocked(desk, clinic, patient):
    visit, v = _visit(desk, patient, clinic, emergency=True)
    services.add_charge(
        request=desk, visit=visit, service=_service(desk.organization, "Lab fee", "LAB")
    )
    services.lab_guard(_order_for(v.token))


# --- Screens ------------------------------------------------------------------------------------


def test_settings_screen_saves_and_keeps_preferences_in_step(admin_client, org):
    assert admin_client.get("/settings/patient-flow/").status_code == 200
    res = admin_client.post(
        "/settings/patient-flow/",
        {
            "consultation_fee_billed_at": "PHARMACY",
            "rx_print_at": "DOCTOR",
            "lab_tests_billed_at": "LAB",
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


# --- Review fixes: procedures and lab tests follow their counter ----------------------------

from apps.lab.tests.test_lab import catalogue  # noqa: E402, F401 (fixture)


def _consult(doc_req, clinic, patient, token):
    from apps.encounters import services as encounters

    token = queue.call(token, request=doc_req, doctor=clinic)
    encounter, _ = encounters.start(request=doc_req, patient=patient, token=token)
    return encounter


def _procedure(org, billed_at, price="1000"):
    svc = _service(org, f"ECG {billed_at}", billed_at, price)
    svc.is_procedure = True
    svc.save()
    return svc


def test_procedure_paid_at_pharmacy_goes_on_the_till_bill(
    desk, doc_req, pharmacist, clinic, patient, cash
):
    from apps.procedures import services as procedures

    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounter = _consult(doc_req, clinic, patient, v.token)
    ecg = _procedure(desk.organization, CounterLocation.PHARMACY)
    proc = procedures.order(request=doc_req, patient=patient, service=ecg, encounter=encounter)
    assert proc.invoice_id is None  # no separate bill
    queue.complete(QueueToken.objects.get(pk=v.token.pk))
    assert _fresh(visit).state == VisitState.PHARMACY_QUEUE

    cart = pos.attach_patient(pos.open_cart(pharmacist), request=pharmacist, patient=patient)
    total = pos.totals(cart)["grand_total"]
    assert total == Decimal("1000")
    sale = pos.checkout(cart, request=pharmacist, payments=[{"method": cash, "amount": total}])
    proc.refresh_from_db()
    assert proc.invoice_id == sale.invoice_id
    assert sale.invoice.items.get().kind == InvoiceItem.Kind.PROCEDURE
    assert _fresh(visit).state == VisitState.COMPLETED


def test_unpaid_procedure_at_central_cash_holds_the_exit_until_paid(
    desk, doc_req, clinic, patient, cash
):
    from apps.procedures import services as procedures

    _settings(desk.organization, pharmacy_handles_manual_rx=False)
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounter = _consult(doc_req, clinic, patient, v.token)
    ecg = _procedure(desk.organization, CounterLocation.CENTRAL_CASH)
    proc = procedures.order(request=doc_req, patient=patient, service=ecg, encounter=encounter)
    assert proc.invoice.status == Invoice.Status.UNPAID
    assert visit.bill_items.filter(procedure=proc, invoice=proc.invoice).exists()
    from apps.encounters import services as encounters

    encounters.complete(encounter, request=doc_req)
    assert _fresh(visit).state == VisitState.PAYMENT_DUE
    visits.collect_fee(proc.invoice, request=desk, method=cash)
    assert _fresh(visit).state == VisitState.COMPLETED


def test_lab_tests_paid_at_pharmacy_send_the_patient_there_first(
    desk,
    doc_req,
    pharmacist,
    lab_tech,
    clinic,
    patient,
    cash,
    catalogue,  # noqa: F811
):
    from apps.encounters import services as encounters
    from apps.lab import services as lab

    _settings(desk.organization, lab_tests_billed_at="PHARMACY")
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounter = _consult(doc_req, clinic, patient, v.token)
    order = lab.create_order(
        request=doc_req, patient=patient, tests=[catalogue["hb"]], encounter=encounter
    )
    assert order.invoice_id is None
    encounters.complete(encounter, request=doc_req)
    assert _fresh(visit).state == VisitState.PHARMACY_QUEUE
    with pytest.raises(lab.LabError, match="blocked"):
        lab.collect_sample(order.samples.get(), request=lab_tech)

    cart = pos.attach_patient(pos.open_cart(pharmacist), request=pharmacist, patient=patient)
    total = pos.totals(cart)["grand_total"]
    sale = pos.checkout(cart, request=pharmacist, payments=[{"method": cash, "amount": total}])
    order.refresh_from_db()
    assert order.invoice_id == sale.invoice_id
    assert _fresh(visit).state == VisitState.LAB_QUEUE
    lab.collect_sample(order.samples.get(), request=lab_tech)
    assert _fresh(visit).state == VisitState.COMPLETED


def test_urgent_lab_order_collects_with_a_reason(
    desk,
    doc_req,
    lab_tech,
    clinic,
    patient,
    cash,
    catalogue,  # noqa: F811
):
    from apps.lab import services as lab

    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounter = _consult(doc_req, clinic, patient, v.token)
    order = lab.create_order(
        request=doc_req, patient=patient, tests=[catalogue["hb"]], encounter=encounter,
        priority="urgent",
    )  # fmt: skip
    with pytest.raises(lab.LabError, match="give a reason"):
        lab.collect_sample(order.samples.get(), request=lab_tech)
    lab.collect_sample(order.samples.get(), request=lab_tech, unpaid_reason="Chest pain")
    assert AuditLog.objects.filter(action="lab.unpaid_override").exists()
    assert lab.start_processing(order, request=lab_tech) == 1  # not asked again


def test_lab_desk_order_keeps_the_labs_own_rules(desk, clinic, patient, cash, catalogue):  # noqa: F811
    from apps.lab import services as lab

    visit, _ = _visit(desk, patient, clinic, collect=True, method=cash)
    services.add_charge(
        request=desk, visit=visit, service=_service(desk.organization, "Lab fee", "LAB")
    )
    tech = desk  # reception may not collect; only the guard is under test
    order = lab.create_order(request=_lab_req(desk), patient=patient, tests=[catalogue["glu"]])
    assert services.visit_for_lab_order(order) is None
    services.lab_guard(order, collecting=True)
    assert tech


def _lab_req(desk):
    from apps.accounts.services import effective_permissions

    desk.sd_perms = effective_permissions(desk.user, desk.branch) | {"lab.order.create"}
    return desk


# --- Review fixes: reception ----------------------------------------------------------------


def test_refund_before_the_doctor_puts_the_token_back_on_hold(desk, clinic, patient, cash):
    from apps.billing import services as billing

    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    assert _fresh(visit).state == VisitState.IN_QUEUE
    payment = v.invoice.payments.get()
    billing.refund(payment, request=desk, amount=payment.amount, reason="Left", method=cash)
    assert _fresh(visit).state == VisitState.TOKEN_ISSUED
    assert not queue.waiting(queue.queue_for(desk.branch, doctor=clinic)).exists()


def test_desk_and_doctor_queue_show_held_tokens(client, make_user, desk, clinic, patient):
    _, v = _visit(desk, patient, clinic)
    client.force_login(make_user("reception", username="r3"))
    page = client.get("/reception/").content.decode()
    assert "Pay first" in page and "Collect fee" in page
    client.force_login(clinic.user)
    assert "1 more waiting to pay at reception" in client.get("/queue/doctor/").content.decode()


def test_check_in_makes_the_fee_invoice(client, make_user, desk, clinic, patient):
    from apps.appointments import services as appts

    appt = appts.book(
        request=desk, patient=patient, doctor=clinic, on_date=timezone.localdate(),
        start=time(23, 0), visit_type="new", source="counter",
    )  # fmt: skip
    client.force_login(make_user("reception", username="r4"))
    client.post(f"/appointments/{appt.pk}/check-in/")
    assert visits.fee_invoice(appt).status == Invoice.Status.UNPAID


# --- Review fixes: doctor's click rule ------------------------------------------------------


def test_waiting_for_the_click_leaves_a_signed_patient_with_the_doctor(
    client, desk, doc_req, clinic, patient, cash
):
    from apps.encounters import services as encounters

    _settings(desk.organization, doctor_must_click_next=True)
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounter = _consult(doc_req, clinic, patient, v.token)
    encounters.complete(encounter, request=doc_req)
    assert _fresh(visit).state == VisitState.WITH_DOCTOR  # pharmacy or lab take it by token


def test_signing_sends_the_patient_on_by_default(desk, doc_req, clinic, patient, cash):
    from apps.encounters import services as encounters

    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounters.complete(_consult(doc_req, clinic, patient, v.token), request=doc_req)
    assert _fresh(visit).state == VisitState.COMPLETED  # no medicine, nothing owed


# --- Review fixes: fees, branches, day end, report ------------------------------------------


def test_only_staff_with_the_fee_permission_add_fees(desk, lab_tech, clinic, patient):
    visit, _ = _visit(desk, patient, clinic)
    svc = _service(desk.organization, "Dressing", CounterLocation.RECEPTION)
    with pytest.raises(services.FlowError, match="not allowed"):
        services.add_charge(request=lab_tech, visit=visit, service=svc)
    assert services.add_charge(request=desk, visit=visit, service=svc).billed_at == "RECEPTION"


def test_doctor_adds_a_fee_from_the_consultation(client, desk, doc_req, clinic, patient, cash):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    encounter = _consult(doc_req, clinic, patient, v.token)
    svc = _service(desk.organization, "Injection", CounterLocation.PHARMACY, "150")
    client.force_login(clinic.user)
    page = client.get(f"/encounters/{encounter.pk}/").content.decode()
    assert "Fees for this visit" in page and "Injection" in page
    client.post(
        f"/flow/{visit.pk}/charge/",
        {"service": svc.pk, "next": f"/encounters/{encounter.pk}/#sec-fees"},
    )
    assert visit.bill_items.filter(service=svc, billed_at="PHARMACY").exists()


def test_a_branch_can_have_its_own_rules(desk, branch2, org):
    services.HospitalFlowSetting.objects.create(
        organization=org, branch=branch2, consultation_fee_billed_at="PHARMACY"
    )
    org._sd_flow_settings = None
    assert services.fee_counter(org, branch2) == "PHARMACY"
    assert services.fee_counter(org, desk.branch) == "RECEPTION"


def test_branch_settings_screen(admin_client, org, branch2):
    url = f"/settings/patient-flow/?branch={branch2.pk}"
    assert admin_client.get(url).status_code == 200
    admin_client.post(url, {"consultation_fee_billed_at": "PHARMACY", "rx_print_at": "NONE",
                            "lab_tests_billed_at": "LAB"})  # fmt: skip
    row = services.HospitalFlowSetting.objects.get(organization=org, branch=branch2)
    assert row.consultation_fee_billed_at == "PHARMACY"
    admin_client.post(url, {"action": "use-hospital"})
    assert not services.HospitalFlowSetting.objects.filter(branch=branch2).exists()


def test_visits_left_open_close_at_day_end(desk, clinic, patient):
    visit, v = _visit(desk, patient, clinic)
    QueueToken.objects.filter(pk=v.token.pk).update(date=timezone.localdate() - timedelta(days=1))
    assert services.close_stale_visits(organization=desk.organization) == 1
    visit = _fresh(visit)
    assert visit.state == VisitState.COMPLETED and visit.closed_at_day_end


def test_flow_times_report(admin_client, desk, clinic, patient, cash):
    _visit(desk, patient, clinic, collect=True, method=cash)
    page = admin_client.get("/reports/patient-flow-times/")
    assert page.status_code == 200 and "Wait for doctor" in page.content.decode()


def test_counter_screens_list_who_is_waiting_there(
    client, make_user, desk, doc_req, clinic, patient, cash
):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    _see_and_finish(doc_req, clinic, v.token)  # paper Rx → pharmacy queue
    client.force_login(make_user("pharmacy", username="ph3"))
    page = client.get("/pharmacy/dispense/").content.decode()
    assert "Waiting here (1)" in page and v.token.label in page
