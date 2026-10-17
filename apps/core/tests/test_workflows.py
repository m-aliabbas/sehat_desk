"""Cross-module workflows end to end (Phase 27 integration tests).

One patient goes through the whole hospital, each step done by the role that does it in real
life. Every hand-off between modules is checked: the record one desk creates is the record the
next desk sees, money and stock move exactly once, and beds are taken and released.
"""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.appointments.services import available_slots
from apps.billing import services as billing
from apps.billing.models import Invoice, PaymentMethod
from apps.encounters import services as encounters
from apps.encounters.models import Encounter
from apps.ipd import services as ipd
from apps.ipd.models import Admission, Bed
from apps.ipd.tests.helpers import admit, make_request, make_ward
from apps.lab import services as lab
from apps.lab.models import LabOrder
from apps.lab.tests.test_lab import catalogue  # noqa: F401 (fixture)
from apps.patients.models import Patient
from apps.pharmacy import pos, stock
from apps.pharmacy.models import Product, StockTransaction
from apps.prescriptions import services as rx_services
from apps.prescriptions.models import Medicine, Prescription
from apps.queue import services as queue
from apps.queue.models import QueueToken


@pytest.fixture
def staff(make_user):
    return {
        role: make_user(role, username=f"{role}1")
        for role in ("reception", "cashier", "lab", "pharmacy", "nurse")
    }


@pytest.fixture
def cash(org):
    billing.ensure_payment_methods(org)
    return PaymentMethod.objects.get(organization=org, code="cash")


@pytest.fixture
def pharmacy_stock(rf, org, branch, admin_user):
    medicine = Medicine.objects.create(
        organization=org, brand_name="Panadol", generic_name="Paracetamol", strength="500 mg"
    )
    product = Product.objects.create(
        organization=org, sku="P-1", brand_name="Panadol", strength="500 mg",
        generic_name="Paracetamol", medicine=medicine, pack_size=10, sale_price=D("3"),
    )  # fmt: skip
    stock.receive(
        request=make_request(rf, admin_user, org, branch), product=product, batch_number="B-1",
        expiry_date=timezone.localdate() + timedelta(days=300), quantity=40,
        purchase_cost=D("1"),
    )  # fmt: skip
    return medicine, product


def test_outpatient_visit_from_registration_to_pharmacy(
    client, rf, org, branch, admin_user, doctor, department, staff, cash, catalogue,  # noqa: F811
    pharmacy_stock,
):  # fmt: skip
    medicine, product = pharmacy_stock

    # 1. Reception registers the patient (screen) ...
    client.force_login(staff["reception"])
    response = client.post(
        reverse("patients:register"),
        {"full_name": "Hina Rauf", "gender": "female", "phone": "0301-2223334",
         "age_years": "34", "allergy_status": "none"},
    )  # fmt: skip
    patient = Patient.objects.get(full_name="Hina Rauf")
    assert response.status_code == 302 and patient.mrn.startswith("SD-")

    # 2. ... books an appointment for today ...
    today = timezone.localdate()
    slot = available_slots(doctor, branch, today + timedelta(days=1))[0]
    response = client.post(
        reverse("appointments:book"),
        {"patient": patient.pk, "doctor": doctor.pk,
         "date": (today + timedelta(days=1)).isoformat(),
         "time": slot.start.strftime("%H:%M"), "visit_type": "new", "source": "counter"},
    )  # fmt: skip
    appt = Appointment.objects.get(patient=patient)
    assert appt.status == Appointment.Status.SCHEDULED
    # (Check-in only works on the day: move the booking to today for the rest of the visit.)
    Appointment.objects.filter(pk=appt.pk).update(date=today)

    # 3. ... and checks the patient in, which issues the token.
    assert client.post(reverse("appointments:check-in", args=[appt.pk])).status_code == 302
    token = QueueToken.objects.get(appointment=appt)
    appt.refresh_from_db()
    assert token.status == "waiting" and appt.status == Appointment.Status.IN_QUEUE

    # 4. The doctor calls the token and opens the consultation from it.
    doc = make_request(rf, doctor.user, org, branch)
    queue.call(token, request=doc, doctor=doctor)
    token.refresh_from_db()
    encounter, created = encounters.start(request=doc, patient=patient, token=token)
    token.refresh_from_db()
    appt.refresh_from_db()
    assert created and token.status == "in_consultation"
    assert appt.status == Appointment.Status.WITH_DOCTOR

    # 5. Notes, a prescription and a lab order, then the doctor signs.
    encounters.save_note(encounter, request=doc, data={"chief_complaint": "Fever, body aches"})
    encounters.save_note(encounter, request=doc, data={"provisional_diagnosis": "Viral fever"})
    rx_services.add_item(
        encounter, request=doc, medicine=medicine,
        data={"dose": "1 tablet", "frequency": "TDS", "duration_value": 5,
              "duration_unit": "days"},
    )  # fmt: skip
    order = lab.create_order(
        request=doc, patient=patient, tests=[catalogue["hb"]], encounter=encounter,
        doctor=doctor, source=LabOrder.Source.DOCTOR,
    )  # fmt: skip
    encounters.complete(encounter, request=doc)
    encounter.refresh_from_db()
    rx = encounter.prescription
    assert encounter.status == Encounter.Status.COMPLETED and not rx.is_draft
    rx_services.send_to_pharmacy(rx, request=doc)

    # 6. The cashier takes the lab payment; the lab can then work the sample.
    assert order.status == LabOrder.Status.PAYMENT_PENDING
    cashier = make_request(rf, staff["cashier"], org, branch)
    invoice = Invoice.objects.get(pk=order.invoice_id)
    billing.receive_payment(invoice, request=cashier, method=cash, amount=invoice.balance_due)
    order.refresh_from_db()
    assert order.status == LabOrder.Status.SAMPLE_PENDING

    tech = make_request(rf, staff["lab"], org, branch)
    lab.collect_sample(order.samples.get(), request=tech)
    lab.start_processing(order, request=tech)
    item = order.items.get()
    result = lab.enter_result(item, request=tech, value="11.2")
    assert result.flag == "low"  # female range 12-15.5
    pathologist = make_request(rf, admin_user, org, branch)
    lab.verify([item], request=pathologist)
    lab.release(order, request=pathologist)
    order.refresh_from_db()
    assert order.status == LabOrder.Status.RELEASED

    # The released report is visible to the doctor.
    client.force_login(doctor.user)
    report = client.get(reverse("lab:report", args=[order.pk]))
    assert report.status_code == 200 and b"11.2" in report.content

    # 7. Pharmacy dispenses the prescription: stock goes down once, a paid invoice is created.
    pharm = make_request(rf, staff["pharmacy"], org, branch)
    before = stock.on_hand(branch, product)
    cart = pos.open_cart(pharm)
    pos.fill_from_prescription(
        cart, request=pharm, prescription=rx,
        lines=[{"rx_item": rx.items.get(), "product": product, "quantity": 15}],
    )  # fmt: skip
    sale = pos.checkout(cart, request=pharm, payments=[{"method": cash, "amount": "45"}])
    rx.refresh_from_db()
    assert rx.dispense_status == Prescription.Dispense.DISPENSED
    assert stock.on_hand(branch, product) == before - 15
    assert StockTransaction.objects.filter(kind="sale", reference=sale.number).count() == 1
    assert sale.invoice.status == Invoice.Status.PAID and sale.invoice.patient == patient

    # The consultation can no longer be reopened once medicines left the pharmacy.
    with pytest.raises(encounters.EncounterError):
        encounters.reopen(encounter, request=doc, reason="Change dose")

    # 8. The patient's record shows the whole visit.
    client.force_login(admin_user)
    timeline = client.get(reverse("patients:detail", args=[patient.pk]), {"tab": "timeline"})
    assert timeline.status_code == 200
    assert appt.number.encode() in timeline.content or encounter.number.encode() in (
        timeline.content
    )


def test_admission_takes_a_bed_and_discharge_releases_it(
    rf, org, branch, admin_user, doctor, patient, staff, cash
):
    _, _, beds = make_ward(org, branch, beds=2)
    req = make_request(rf, admin_user, org, branch)
    free_before = ipd.free_beds(branch).count()

    admission = admit(req, patient, doctor, beds[0])
    beds[0].refresh_from_db()
    assert beds[0].status == Bed.Status.OCCUPIED
    assert ipd.free_beds(branch).count() == free_before - 1
    assert ipd.current_admission(patient, org) == admission

    # A deposit, then discharge with the bill settled.
    ipd.record_deposit(admission, request=req, method=cash, amount=D("5000"))
    ipd.save_discharge_draft(admission, request=req, data={"discharge_diagnosis": "Recovered"})
    ipd.plan_discharge(admission, request=req)
    ipd.finalize_bill(admission, request=req)
    for invoice in ipd.stay_invoices(admission):
        invoice.refresh_from_db()
        if invoice.balance_due > 0:
            billing.receive_payment(invoice, request=req, method=cash, amount=invoice.balance_due)
    ipd.release(admission, request=make_request(rf, staff["nurse"], org, branch))

    admission.refresh_from_db()
    beds[0].refresh_from_db()
    assert admission.status == Admission.Status.DISCHARGED
    assert beds[0].status == Bed.Status.CLEANING  # back in service after housekeeping
    assert ipd.current_admission(patient, org) is None
    assert admission.summary_versions.count() == 1
