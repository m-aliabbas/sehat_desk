"""Slot calculation and appointment state changes. Views never edit status directly."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import next_number
from apps.masterdata.models import DoctorSchedule

from .models import Appointment

Status = Appointment.Status
VisitType = Appointment.VisitType


class BookingError(ValidationError):
    pass


@dataclass
class Slot:
    start: time
    end: time
    booked: int = 0
    past: bool = False

    @property
    def available(self) -> bool:
        return not self.past and self.booked == 0

    @property
    def value(self) -> str:
        return self.start.strftime("%H:%M")


def _add_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(date.min, t) + timedelta(minutes=minutes)).time()


def schedule_for(doctor, branch, on_date):
    return DoctorSchedule.objects.filter(
        doctor=doctor, branch=branch, weekday=on_date.weekday(), is_active=True
    ).first()


def on_leave(doctor, on_date) -> bool:
    return doctor.leaves.filter(start_date__lte=on_date, end_date__gte=on_date).exists()


def _overlapping(doctor, on_date, start, end, exclude_pk=None):
    # A doctor cannot be in two places at once, so check every branch, not only this one.
    qs = Appointment.objects.active().filter(
        doctor=doctor, date=on_date, start_time__lt=end, end_time__gt=start
    )
    return qs.exclude(pk=exclude_pk) if exclude_pk else qs


def available_slots(doctor, branch, on_date, *, exclude_pk=None) -> list[Slot]:
    """Every slot in the doctor's clinic hours on that date, with how many bookings it has."""
    sched = schedule_for(doctor, branch, on_date)
    if sched is None or not doctor.is_active or on_leave(doctor, on_date):
        return []
    booked = list(
        Appointment.objects.active()
        .filter(doctor=doctor, date=on_date)
        .exclude(pk=exclude_pk)
        .values_list("start_time", "end_time")
    )
    now = timezone.localtime()
    slots = []
    for start in sched.slot_starts(doctor.slot_minutes, on_date):
        end = _add_minutes(start, doctor.slot_minutes)
        slot = Slot(start=start, end=end)
        slot.booked = sum(1 for s, e in booked if s < end and e > start)
        slot.past = on_date < now.date() or (on_date == now.date() and start <= now.time())
        slots.append(slot)
    return slots


def default_fee(doctor, patient, visit_type):
    if visit_type == VisitType.FOLLOW_UP:
        return doctor.follow_up_fee
    return doctor.consultation_fee


def suggest_visit_type(doctor, patient, on_date) -> str:
    """Follow-up when the patient saw this doctor within the doctor's follow-up window."""
    last = (
        Appointment.objects.filter(doctor=doctor, patient=patient, status=Status.COMPLETED)
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
    )
    if last and (on_date - last).days <= doctor.follow_up_days:
        return VisitType.FOLLOW_UP
    return VisitType.NEW


def _check_slot(doctor, branch, on_date, start, visit_type, exclude_pk=None):
    """Validate a requested time. Returns (end_time, is_overbooked)."""
    end = _add_minutes(start, doctor.slot_minutes)
    urgent = visit_type in (VisitType.WALK_IN, VisitType.EMERGENCY)
    if not doctor.is_active:
        raise BookingError(f"{doctor} is not accepting appointments.")
    if on_date < timezone.localdate():
        raise BookingError("Appointments cannot be booked in the past.")
    if on_leave(doctor, on_date) and visit_type != VisitType.EMERGENCY:
        raise BookingError(f"{doctor} is on leave on {on_date:%d %b %Y}.")
    if not urgent:
        valid_starts = {
            s.start for s in available_slots(doctor, branch, on_date, exclude_pk=exclude_pk)
        }
        if start not in valid_starts:
            raise BookingError("That time is not one of the doctor's slots in this branch.")
        now = timezone.localtime()
        if on_date == now.date() and start <= now.time():
            raise BookingError(
                "That slot has already started. Choose a later slot or book a walk-in."
            )
    clash = _overlapping(doctor, on_date, start, end, exclude_pk).exists()
    if clash and not (doctor.allow_overbooking or urgent):
        raise BookingError("That slot is already booked. Choose another slot.")
    return end, clash


def _lock_bookings(organization):
    """Serialize booking writes per organization before checking for clashes.

    The UPDATE inside next_number takes the write lock (SQLite) or a row lock (PostgreSQL), so a
    second desk booking the same slot waits here and then sees the first booking.
    """
    next_number(organization, "booking-lock")


@transaction.atomic
def book(
    *, request, patient, doctor, on_date, start, visit_type, source, reason="", notes="", fee=None
):
    _lock_bookings(request.organization)
    if not patient.is_active:
        raise BookingError("This patient record is inactive.")
    if Appointment.objects.active().filter(patient=patient, doctor=doctor, date=on_date).exists():
        raise BookingError(
            f"{patient.full_name} already has an appointment with {doctor} that day."
        )
    end, overbooked = _check_slot(doctor, request.branch, on_date, start, visit_type)
    number = next_number(request.organization, "appointment")
    return Appointment.objects.create(
        organization=request.organization,
        branch=request.branch,
        number=f"APT-{number:06d}",
        patient=patient,
        doctor=doctor,
        department=doctor.department,
        date=on_date,
        start_time=start,
        end_time=end,
        visit_type=visit_type,
        source=source,
        reason=reason,
        notes=notes,
        fee=default_fee(doctor, patient, visit_type) if fee is None else fee,
        is_overbooked=overbooked,
        created_by=request.user,
    )


@transaction.atomic
def reschedule(appt, *, on_date, start, doctor=None):
    if not appt.can_reschedule:
        raise BookingError(
            f"A {appt.get_status_display().lower()} appointment cannot be rescheduled."
        )
    _lock_bookings(appt.organization)
    doctor = doctor or appt.doctor
    end, overbooked = _check_slot(
        doctor, appt.branch, on_date, start, appt.visit_type, exclude_pk=appt.pk
    )
    appt.doctor = doctor
    appt.department = doctor.department
    appt.date, appt.start_time, appt.end_time = on_date, start, end
    appt.is_overbooked = overbooked
    appt.reschedule_count += 1
    appt.status = Status.SCHEDULED
    appt.save()
    return appt


def _transition(appt, new_status):
    if not appt.can_transition(new_status):
        raise BookingError(
            f"Cannot change a {appt.get_status_display().lower()} appointment to "
            f"{Status(new_status).label.lower()}."
        )
    appt.status = new_status


@transaction.atomic
def cancel(appt, *, user, reason):
    if not reason.strip():
        raise BookingError("A cancellation reason is required.")
    _transition(appt, Status.CANCELLED)
    appt.cancellation_reason = reason.strip()[:200]
    appt.cancelled_at = timezone.now()
    appt.cancelled_by = user
    appt.save()
    return appt


@transaction.atomic
def confirm(appt):
    _transition(appt, Status.CONFIRMED)
    appt.save(update_fields=["status", "updated_at"])
    return appt


@transaction.atomic
def check_in(appt):
    if appt.date != timezone.localdate():
        raise BookingError("Only today's appointments can be checked in.")
    _transition(appt, Status.CHECKED_IN)
    appt.checked_in_at = timezone.now()
    appt.save(update_fields=["status", "checked_in_at", "updated_at"])
    return appt


@transaction.atomic
def mark_no_show(appt):
    if not appt.is_past_start:
        raise BookingError("An appointment can only be marked no-show after its start time.")
    _transition(appt, Status.NO_SHOW)
    appt.save(update_fields=["status", "updated_at"])
    return appt
