"""The one-screen front-desk visit: token, fee invoice, vitals, papers, pre-booking and shifts."""

from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.appointments import services, visits
from apps.appointments.models import Appointment
from apps.billing.models import Invoice
from apps.masterdata.models import DoctorSchedule
from apps.organizations.models import preferences_for
from apps.queue.models import QueueToken
from apps.vitals.models import VitalSign

pytestmark = pytest.mark.django_db


@pytest.fixture
def clinic(doctor, branch):
    doctor.consultation_fee = Decimal("1500")
    doctor.save()
    DoctorSchedule.objects.filter(doctor=doctor).delete()
    for day in range(7):
        DoctorSchedule.objects.create(
            doctor=doctor, branch=branch, weekday=day, start_time=time(0), end_time=time(23, 45)
        )
    return doctor


@pytest.fixture
def desk(client, make_user):
    client.force_login(make_user("reception", username="frontdesk"))
    return client


AUTO_PRINT = 'addEventListener("load", () => window.print())'
AUTO_PRINT_SLIPS = 'addEventListener("load", printSlips)'


def _post(desk, patient, doctor, on_date=None, **extra):
    data = {
        "mode": "find",
        "patient": patient.pk,
        "doctor": doctor.pk,
        "date": (on_date or timezone.localdate()).isoformat(),
        **extra,
    }
    return desk.post("/appointments/visit/", data)


def test_screen_renders(desk, clinic):
    page = desk.get("/appointments/visit/")
    assert page.status_code == 200
    assert clinic.display_name in page.content.decode()


def test_today_makes_token_invoice_and_payment(desk, clinic, patient, cash):
    response = _post(desk, patient, clinic, collect="on", method=cash.pk, key="k1")
    appt = Appointment.objects.get(patient=patient)
    assert response.status_code == 302
    assert f"/appointments/{appt.pk}/papers/" in response["Location"]
    assert QueueToken.objects.filter(appointment=appt).exists()
    invoice = appt.invoices.get()
    assert invoice.status == Invoice.Status.PAID
    assert invoice.grand_total == Decimal("1500")


def test_unpaid_visit_leaves_balance_on_the_papers(desk, clinic, patient):
    _post(desk, patient, clinic)
    appt = Appointment.objects.get(patient=patient)
    assert appt.invoices.get().status == Invoice.Status.UNPAID
    page = desk.get(f"/appointments/{appt.pk}/papers/?print=1")
    html = page.content.decode()
    assert "Collect the fee" in html
    assert AUTO_PRINT_SLIPS not in html  # hold printing until the fee is in


def test_papers_show_blank_rx_and_both_copies(desk, clinic, patient, cash):
    _post(desk, patient, clinic, collect="on", method=cash.pk, key="k2")
    appt = Appointment.objects.get(patient=patient)
    html = desk.get(f"/appointments/{appt.pk}/papers/?print=1").content.decode()
    assert "Patient copy" in html and "Office copy" in html and "thermal-sheet" in html
    assert html.count("data-slip-copy>") == 2 and "cut here" not in html
    assert AUTO_PRINT_SLIPS in html and "℞" not in html and "size: A5" not in html
    rx = desk.get(f"/appointments/{appt.pk}/papers/?part=rx&print=1").content.decode()
    assert "℞" in rx and "size: A5" in rx and AUTO_PRINT in rx


def test_admin_can_turn_off_prescription_office_copy_and_fee(desk, clinic, patient, org):
    prefs = preferences_for(org)
    prefs.opd_print_blank_prescription = False
    prefs.opd_collect_fee_upfront = False
    prefs.save()
    _post(desk, patient, clinic)
    appt = Appointment.objects.get(patient=patient)
    assert not appt.invoices.exists()
    assert QueueToken.objects.filter(appointment=appt).exists()
    assert "℞" not in desk.get(f"/appointments/{appt.pk}/papers/").content.decode()


def test_new_patient_on_the_same_screen(desk, clinic):
    desk.post(
        "/appointments/visit/",
        {
            "mode": "new",
            "new-full_name": "Sana Tariq",
            "new-phone": "0300 1112223",
            "new-age_years": "30",
            "doctor": clinic.pk,
            "date": timezone.localdate().isoformat(),
        },
    )
    appt = Appointment.objects.get(patient__full_name="Sana Tariq")
    assert appt.status == Appointment.Status.IN_QUEUE


def test_vitals_are_optional_and_saved_with_the_token(desk, clinic, patient):
    _post(desk, patient, clinic, **{"v-bp_systolic": "120", "v-bp_diastolic": "80",
                                    "v-temperature_unit": "F"})  # fmt: skip
    vitals = VitalSign.objects.get(patient=patient)
    assert vitals.bp_systolic == 120 and vitals.token is not None


def test_bad_vitals_book_nothing(desk, clinic, patient):
    page = _post(desk, patient, clinic, **{"v-bp_systolic": "80", "v-bp_diastolic": "120",
                                           "v-temperature_unit": "F"})  # fmt: skip
    assert page.status_code == 200
    assert not Appointment.objects.filter(patient=patient).exists()


def test_prebooking_has_no_token_then_arrival_prints(desk, clinic, patient):
    later = timezone.localdate() + timedelta(days=2)
    _post(desk, patient, clinic, on_date=later)
    appt = Appointment.objects.get(patient=patient)
    assert not QueueToken.objects.filter(appointment=appt).exists()
    assert not appt.invoices.exists()
    # Refused on the wrong day; nothing changes.
    desk.post(f"/appointments/{appt.pk}/arrive/")
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.SCHEDULED
    # On the day.
    Appointment.objects.filter(pk=appt.pk).update(date=timezone.localdate())
    response = desk.post(f"/appointments/{appt.pk}/arrive/")
    assert f"/appointments/{appt.pk}/papers/" in response["Location"]
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.IN_QUEUE
    assert appt.invoices.get().status == Invoice.Status.UNPAID


def test_prebooking_with_advance_fee(desk, clinic, patient, cash):
    later = timezone.localdate() + timedelta(days=2)
    _post(desk, patient, clinic, on_date=later, collect="on", method=cash.pk, key="k3")
    appt = Appointment.objects.get(patient=patient)
    assert appt.invoices.get().status == Invoice.Status.PAID
    Appointment.objects.filter(pk=appt.pk).update(date=timezone.localdate())
    desk.post(f"/appointments/{appt.pk}/arrive/")
    assert appt.invoices.count() == 1  # the advance is not billed twice


def test_collect_on_papers(desk, clinic, patient, cash):
    _post(desk, patient, clinic)
    appt = Appointment.objects.get(patient=patient)
    desk.post(f"/appointments/{appt.pk}/collect/", {"method": cash.pk, "key": "k4"})
    assert appt.invoices.get().status == Invoice.Status.PAID


def test_double_submit_does_not_make_two_visits(desk, clinic, patient, cash):
    _post(desk, patient, clinic, collect="on", method=cash.pk, key="k5")
    page = _post(desk, patient, clinic, collect="on", method=cash.pk, key="k5")
    assert page.status_code == 200
    assert Appointment.objects.filter(patient=patient).count() == 1
    assert Invoice.objects.filter(patient=patient).count() == 1


def test_other_organization_papers_404(client, make_user, clinic, patient, desk):
    from apps.organizations.models import Organization

    _post(desk, patient, clinic)
    appt = Appointment.objects.get(patient=patient)
    other = Organization.objects.create(name="Other", patient_id_prefix="OT")
    Appointment.objects.filter(pk=appt.pk).update(organization=other)
    assert desk.get(f"/appointments/{appt.pk}/papers/").status_code == 404


def test_doctor_cannot_use_front_desk(client, make_user, clinic):
    client.force_login(make_user("doctor", username="drx"))
    assert client.get("/appointments/visit/").status_code == 403


def test_shifts_give_slots_in_both_clinics(doctor, branch):
    day = timezone.localdate() + timedelta(days=1)
    DoctorSchedule.objects.filter(doctor=doctor).delete()
    for shift, start, end in (("morning", 9, 10), ("evening", 18, 19)):
        DoctorSchedule.objects.create(
            doctor=doctor, branch=branch, weekday=day.weekday(), shift=shift,
            start_time=time(start), end_time=time(end),
        )  # fmt: skip
    slots = services.available_slots(doctor, branch, day)
    assert {s.shift for s in slots} == {"morning", "evening"}
    assert services.next_free_slot(doctor, branch, day, shift="evening").start == time(18)


def test_no_free_slot_today_falls_back_to_walk_in(doctor, make_request, make_user):
    DoctorSchedule.objects.filter(doctor=doctor).delete()
    request = make_request(make_user("reception", username="rx2"))
    assert visits._choose_time(request, doctor, timezone.localdate(), "")[1] == (
        Appointment.VisitType.WALK_IN
    )


def test_overlapping_shift_is_refused(admin_client, org, doctor, branch):
    prefs = preferences_for(org)
    prefs.use_doctor_shifts = True
    prefs.save()
    DoctorSchedule.objects.filter(doctor=doctor).delete()
    DoctorSchedule.objects.create(
        doctor=doctor, branch=branch, weekday=0, shift="morning",
        start_time=time(9), end_time=time(13),
    )  # fmt: skip
    data = {"save_schedule": "1", "days-TOTAL_FORMS": "7", "days-INITIAL_FORMS": "7"}
    for i in range(7):
        data[f"days-{i}-weekday"] = str(i)
    data.update({"days-0-enabled": "on", "days-0-start_time": "12:00", "days-0-end_time": "15:00"})
    url = f"/settings/doctors/{doctor.pk}/?branch={branch.pk}&shift=evening"
    page = admin_client.post(url, data)
    assert "Overlaps the Morning shift" in page.content.decode()
    assert not DoctorSchedule.objects.filter(doctor=doctor, shift="evening").exists()
    data.update({"days-0-start_time": "17:00", "days-0-end_time": "21:00"})
    admin_client.post(url, data)
    assert DoctorSchedule.objects.filter(doctor=doctor, shift="evening").count() == 1


def test_reception_desk_is_the_booking_screen(desk, clinic, patient, cash):
    page = desk.get("/reception/")
    html = page.content.decode()
    assert 'action="/appointments/visit/"' in html and 'name="v-bp_systolic"' in html
    assert clinic.display_name in html
    response = _post(desk, patient, clinic, collect="on", method=cash.pk, key="k9")
    appt = Appointment.objects.get(patient=patient)
    assert f"/appointments/{appt.pk}/papers/?print=1" in response["Location"]
