from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.appointments import services
from apps.appointments.models import Appointment
from apps.audit.models import AuditLog
from apps.masterdata.models import DoctorLeave

Status = Appointment.Status


@pytest.fixture
def future_day():
    return timezone.localdate() + timedelta(days=3)


def _book(client, patient, doctor, day, t="09:30", **extra):
    data = {
        "patient": patient.pk,
        "doctor": doctor.pk,
        "date": day.isoformat(),
        "time": t,
        "visit_type": "new",
        "source": "counter",
    }
    data.update(extra)
    return client.post("/appointments/book/", data)


def _appt(org, branch, patient, doctor, user, day, start=time(9), status=Status.SCHEDULED):
    end = (datetime.combine(day, start) + timedelta(minutes=30)).time()
    return Appointment.objects.create(
        organization=org, branch=branch, number=f"T-{Appointment.objects.count()}", patient=patient,
        doctor=doctor, department=doctor.department, date=day, start_time=start, end_time=end,
        fee=Decimal("0"), status=status, created_by=user,
    )  # fmt: skip


# --- Slots ---------------------------------------------------------------------------------


def test_slots_follow_schedule_and_skip_break(doctor, branch, future_day):
    slots = services.available_slots(doctor, branch, future_day)
    starts = [s.start for s in slots]
    assert starts[0] == time(9) and time(11) not in starts and starts[-1] == time(13, 30)
    assert all(s.available for s in slots)


def test_booked_slot_is_unavailable(org, branch, doctor, patient, admin_user, future_day):
    _appt(org, branch, patient, doctor, admin_user, future_day, time(9, 30))
    slots = {s.start: s for s in services.available_slots(doctor, branch, future_day)}
    assert not slots[time(9, 30)].available and slots[time(10)].available


def test_cancelled_booking_frees_slot(org, branch, doctor, patient, admin_user, future_day):
    _appt(org, branch, patient, doctor, admin_user, future_day, time(9, 30), Status.CANCELLED)
    slots = {s.start: s for s in services.available_slots(doctor, branch, future_day)}
    assert slots[time(9, 30)].available


def test_no_slots_on_leave_or_without_schedule(doctor, branch, branch2, future_day):
    assert services.available_slots(doctor, branch2, future_day) == []
    DoctorLeave.objects.create(doctor=doctor, start_date=future_day, end_date=future_day)
    assert services.available_slots(doctor, branch, future_day) == []


def test_slots_endpoint_suggests_follow_up(
    admin_client, org, branch, doctor, patient, admin_user, future_day
):
    _appt(
        org,
        branch,
        patient,
        doctor,
        admin_user,
        timezone.localdate() - timedelta(days=2),
        status=Status.COMPLETED,
    )
    response = admin_client.get(
        "/appointments/slots/",
        {"doctor": doctor.pk, "date": future_day.isoformat(), "patient": patient.pk},
    )
    assert response.context["suggested_visit_type"] == "follow_up"
    assert response.context["suggested_fee"] == Decimal("800")


# --- Booking --------------------------------------------------------------------------------------


def test_book_appointment(admin_client, doctor, patient, future_day):
    response = _book(admin_client, patient, doctor, future_day)
    appt = Appointment.objects.get()
    assert response.url == f"/appointments/{appt.pk}/"
    assert appt.number == "APT-000001" and appt.status == Status.SCHEDULED
    assert appt.end_time == time(10) and appt.fee == Decimal("1500")
    assert AuditLog.objects.filter(action="appointment.create").exists()


def test_double_booking_prevented(admin_client, doctor, patient, future_day, org, admin_user):
    from apps.patients.services import register_patient

    _book(admin_client, patient, doctor, future_day)
    other = register_patient(
        organization=org,
        branch=doctor.schedules.first().branch,
        user=admin_user,
        data={"full_name": "Bilal"},
    )
    response = _book(admin_client, other, doctor, future_day)
    assert response.status_code == 200
    assert b"already booked" in response.content
    assert Appointment.objects.count() == 1


def test_overbooking_allowed_when_configured(
    admin_client, doctor, patient, future_day, org, admin_user
):
    from apps.patients.services import register_patient

    doctor.allow_overbooking = True
    doctor.save()
    _book(admin_client, patient, doctor, future_day)
    other = register_patient(
        organization=org,
        branch=doctor.schedules.first().branch,
        user=admin_user,
        data={"full_name": "Bilal"},
    )
    _book(admin_client, other, doctor, future_day)
    assert Appointment.objects.filter(is_overbooked=True).count() == 1


def test_same_patient_same_doctor_same_day_blocked(admin_client, doctor, patient, future_day):
    _book(admin_client, patient, doctor, future_day, "09:00")
    response = _book(admin_client, patient, doctor, future_day, "10:00")
    assert b"already has an appointment" in response.content


@pytest.mark.parametrize("t", ["11:00", "15:00", "09:10"])
def test_time_outside_slots_rejected(admin_client, doctor, patient, future_day, t):
    response = _book(admin_client, patient, doctor, future_day, t)
    assert response.status_code == 200 and Appointment.objects.count() == 0


def test_past_date_rejected(admin_client, doctor, patient):
    response = _book(admin_client, patient, doctor, timezone.localdate() - timedelta(days=1))
    assert b"in the past" in response.content


def test_walk_in_can_book_outside_slots(admin_client, doctor, patient, future_day):
    _book(admin_client, patient, doctor, future_day, "15:10", visit_type="walk_in")
    assert Appointment.objects.get().start_time == time(15, 10)


def test_booking_on_leave_rejected(admin_client, doctor, patient, future_day):
    DoctorLeave.objects.create(doctor=doctor, start_date=future_day, end_date=future_day)
    assert b"on leave" in _book(admin_client, patient, doctor, future_day).content


def test_reception_can_book_but_doctor_cannot(client, make_user, doctor, patient, future_day):
    client.force_login(make_user("doctor"))
    assert client.get("/appointments/").status_code == 200
    assert _book(client, patient, doctor, future_day).status_code == 403
    client.force_login(make_user("reception"))
    assert _book(client, patient, doctor, future_day).status_code == 302


def test_cannot_book_other_org_patient(admin_client, doctor, future_day, admin_user):
    from apps.organizations.models import Branch, Organization
    from apps.patients.services import register_patient

    other = Organization.objects.create(name="Other")
    ob = Branch.objects.create(organization=other, name="M", code="M")
    stranger = register_patient(
        organization=other, branch=ob, user=admin_user, data={"full_name": "X"}
    )
    response = _book(admin_client, stranger, doctor, future_day)
    assert response.status_code == 200 and not Appointment.objects.exists()


def test_appointment_in_other_branch_is_404(
    admin_client, org, branch2, doctor, patient, admin_user, future_day
):
    a = _appt(org, branch2, patient, doctor, admin_user, future_day)
    assert admin_client.get(f"/appointments/{a.pk}/").status_code == 404


# --- Status changes ---------------------------------------------------------------


def test_cancel_requires_reason(admin_client, org, branch, doctor, patient, admin_user, future_day):
    a = _appt(org, branch, patient, doctor, admin_user, future_day)
    admin_client.post(f"/appointments/{a.pk}/cancel/", {"reason": ""})
    a.refresh_from_db()
    assert a.status == Status.SCHEDULED
    admin_client.post(f"/appointments/{a.pk}/cancel/", {"reason": "Patient requested"})
    a.refresh_from_db()
    assert a.status == Status.CANCELLED and a.cancellation_reason == "Patient requested"
    assert AuditLog.objects.get(action="appointment.cancel").changes == {
        "status": ["scheduled", "cancelled"]
    }


def test_check_in_only_today(admin_client, org, branch, doctor, patient, admin_user, future_day):
    future = _appt(org, branch, patient, doctor, admin_user, future_day)
    admin_client.post(f"/appointments/{future.pk}/check-in/")
    future.refresh_from_db()
    assert future.status == Status.SCHEDULED
    today = _appt(org, branch, patient, doctor, admin_user, timezone.localdate(), time(13, 30))
    admin_client.post(f"/appointments/{today.pk}/check-in/")
    today.refresh_from_db()
    # Check-in issues a queue token, so the appointment moves straight into the queue.
    assert today.status == Status.IN_QUEUE and today.checked_in_at
    assert today.token.label == "G-001"


def test_no_show_only_after_start(
    admin_client, org, branch, doctor, patient, admin_user, future_day
):
    a = _appt(org, branch, patient, doctor, admin_user, future_day)
    admin_client.post(f"/appointments/{a.pk}/no-show/")
    a.refresh_from_db()
    assert a.status == Status.SCHEDULED
    past = _appt(org, branch, patient, doctor, admin_user, timezone.localdate() - timedelta(days=1))
    admin_client.post(f"/appointments/{past.pk}/no-show/")
    past.refresh_from_db()
    assert past.status == Status.NO_SHOW


def test_closed_appointment_cannot_change(
    admin_client, org, branch, doctor, patient, admin_user, future_day
):
    a = _appt(org, branch, patient, doctor, admin_user, future_day, status=Status.CANCELLED)
    admin_client.post(f"/appointments/{a.pk}/confirm/")
    a.refresh_from_db()
    assert a.status == Status.CANCELLED


def test_reschedule(admin_client, org, branch, doctor, patient, admin_user, future_day):
    a = _appt(org, branch, patient, doctor, admin_user, future_day)
    new_day = future_day + timedelta(days=1)
    response = admin_client.post(
        f"/appointments/{a.pk}/reschedule/",
        {"date": new_day.isoformat(), "time": "12:00", "doctor": doctor.pk},
    )
    assert response.status_code == 302
    a.refresh_from_db()
    assert (a.date, a.start_time, a.reschedule_count) == (new_day, time(12), 1)
    assert AuditLog.objects.filter(action="appointment.reschedule").exists()


def test_reschedule_into_own_slot_is_allowed(
    admin_client, org, branch, doctor, patient, admin_user, future_day
):
    a = _appt(org, branch, patient, doctor, admin_user, future_day, time(9))
    admin_client.post(
        f"/appointments/{a.pk}/reschedule/",
        {"date": future_day.isoformat(), "time": "09:00", "doctor": doctor.pk},
    )
    a.refresh_from_db()
    assert a.reschedule_count == 1


# --- Screens --------------------------------------------------------------------


@pytest.mark.parametrize("view", ["day", "week"])
def test_calendar_views(admin_client, org, branch, doctor, patient, admin_user, future_day, view):
    _appt(org, branch, patient, doctor, admin_user, future_day)
    response = admin_client.get("/appointments/", {"view": view, "date": future_day.isoformat()})
    assert response.status_code == 200
    if view == "day":
        assert b"Ayesha Khan" in response.content
        assert response.context["columns"] == [doctor]


def test_list_and_patient_history(admin_client, org, branch, doctor, patient, admin_user):
    _appt(org, branch, patient, doctor, admin_user, timezone.localdate(), time(13, 30))
    assert b"Ayesha Khan" in admin_client.get("/appointments/list/").content
    response = admin_client.get(f"/patients/{patient.pk}/", {"tab": "visits"})
    assert len(response.context["appointments"]) == 1


def test_dashboard_counts(admin_client, org, branch, doctor, patient, admin_user):
    _appt(org, branch, patient, doctor, admin_user, timezone.localdate(), time(13, 30))
    ctx = admin_client.get("/").context
    assert ctx["patient_total"] == 1 and ctx["appt_total"] == 1 and ctx["doctors_today"] == 1


def test_sequence_numbers_do_not_repeat(org):
    from apps.core.models import next_number

    assert [next_number(org, "x") for _ in range(3)] == [1, 2, 3]


def test_calendar_rows_are_bookable_slot_times(admin_client, doctor, branch, future_day):
    """Every open cell's "+ Book" time must be a slot the booking service accepts."""
    sched = doctor.schedules.get(weekday=future_day.weekday())
    sched.break_end = time(11, 15)
    sched.save()
    rows = admin_client.get("/appointments/", {"date": future_day.isoformat()}).context["rows"]
    offered = {r["time"] for r in rows if r["cells"][0]["open"]}
    valid = {s.start for s in services.available_slots(doctor, branch, future_day)}
    assert offered == valid and time(11, 15) in offered and time(11, 30) not in offered
