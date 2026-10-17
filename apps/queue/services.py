"""Token lifecycle. Views call these functions; they never change token status directly.

waiting → called → in_consultation → completed
   ↑        ↓ skip           (cancel from any open state)
   └── requeue ← skipped
The linked appointment follows: in_queue → with_doctor → completed (or cancelled).
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.appointments.services import schedule_for
from apps.core.models import next_number

from .models import QueueToken

Status = QueueToken.Status
ApptStatus = Appointment.Status


class QueueError(ValidationError):
    pass


def _set_appointment(token, status):
    appt = token.appointment
    if appt and appt.status != status and appt.can_transition(status):
        appt.status = status
        appt.save(update_fields=["status", "updated_at"])


@transaction.atomic
def issue_token(*, request, patient, department, doctor=None, appointment=None, priority="normal"):
    """Create the next token for this branch + department today."""
    today = timezone.localdate()
    if priority == QueueToken.Priority.SPECIAL and "priority.special" not in request.sd_perms:
        raise QueueError("You are not allowed to give special priority.")
    if doctor and doctor.department_id != department.id:
        raise QueueError("The doctor is not in that department.")
    if appointment is not None:
        if appointment.date != today:
            raise QueueError("Tokens are only issued for today's appointments.")
        if hasattr(appointment, "token"):
            raise QueueError(f"This appointment already has token {appointment.token.label}.")
        if appointment.status not in (
            ApptStatus.SCHEDULED, ApptStatus.CONFIRMED, ApptStatus.CHECKED_IN
        ):  # fmt: skip
            raise QueueError(
                f"A {appointment.get_status_display().lower()} appointment cannot join the queue."
            )
    open_token = QueueToken.objects.filter(
        branch=request.branch, date=today, patient=patient, department=department,
        status__in=QueueToken.OPEN_STATUSES,
    ).first()  # fmt: skip
    if open_token:
        raise QueueError(f"{patient.full_name} already holds token {open_token.label} here.")

    number = next_number(
        request.organization, f"token:{request.branch.pk}:{department.pk}:{today:%Y%m%d}"
    )
    sched = schedule_for(doctor, request.branch, today) if doctor else None
    token = QueueToken.objects.create(
        organization=request.organization,
        branch=request.branch,
        date=today,
        department=department,
        doctor=doctor,
        patient=patient,
        appointment=appointment,
        number=number,
        label=f"{department.token_letters}-{number:03d}",
        room=sched.room if sched else "",
        priority=priority,
        issued_by=request.user,
    )
    if appointment is not None:
        # Arrival = check-in + joining the queue.
        appointment.checked_in_at = appointment.checked_in_at or timezone.now()
        appointment.status = ApptStatus.IN_QUEUE
        appointment.save(update_fields=["status", "checked_in_at", "updated_at"])
    return token


def queue_for(branch, *, doctor=None, department=None, day=None):
    """Tokens a doctor (or department desk) serves today: their own plus the department queue."""
    day = day or timezone.localdate()
    qs = QueueToken.objects.filter(branch=branch, date=day).select_related(
        "patient", "doctor__user", "department"
    )
    if doctor is not None:
        qs = qs.filter(
            Q(doctor=doctor) | Q(doctor__isnull=True, department_id=doctor.department_id)
        )
    elif department is not None:
        qs = qs.filter(department=department)
    return qs


def waiting(qs):
    return qs.filter(status=Status.WAITING).order_by("priority_rank", "queued_at")


def current_for(branch, doctor):
    """The token a doctor is calling or seeing now."""
    return (
        QueueToken.objects.filter(
            branch=branch,
            date=timezone.localdate(),
            doctor=doctor,
            status__in=(Status.CALLED, Status.IN_CONSULTATION),
        )
        .select_related("patient")
        .order_by("-called_at")
        .first()
    )


def _require(token, *allowed):
    if token.status not in allowed:
        raise QueueError(f"{token.label} is {token.get_status_display().lower()}.")


@transaction.atomic
def call_next(*, request, doctor):
    current = current_for(request.branch, doctor)
    if current:
        raise QueueError(
            f"Finish {current.label} first: complete it, skip it, or start the consultation."
        )
    token = (
        waiting(queue_for(request.branch, doctor=doctor)).select_for_update(of=("self",)).first()
    )
    if token is None:
        raise QueueError("No one is waiting in this queue.")
    return call(token, request=request, doctor=doctor)


@transaction.atomic
def call(token, *, request, doctor):
    _require(token, Status.WAITING)
    current = current_for(request.branch, doctor)
    if current and current.pk != token.pk:
        raise QueueError(f"Finish {current.label} first.")
    token.status = Status.CALLED
    token.doctor = doctor  # a department-queue token is claimed by the doctor who calls it
    if not token.room:
        sched = schedule_for(doctor, request.branch, token.date)
        token.room = sched.room if sched else ""
    token.called_at = timezone.now()
    token.call_count = F("call_count") + 1
    token.called_by = request.user
    token.save()
    token.refresh_from_db()
    return token


@transaction.atomic
def recall(token, *, request):
    _require(token, Status.CALLED)
    token.called_at = timezone.now()
    token.call_count = F("call_count") + 1
    token.save(update_fields=["called_at", "call_count", "updated_at"])
    token.refresh_from_db()
    return token


@transaction.atomic
def skip(token):
    """Patient did not respond to the call. They can be put back in line later."""
    _require(token, Status.CALLED, Status.WAITING)
    token.status = Status.SKIPPED
    token.skipped_at = timezone.now()
    token.save(update_fields=["status", "skipped_at", "updated_at"])
    return token


@transaction.atomic
def requeue(token):
    """A skipped patient has arrived: back to waiting, at the end of their priority group."""
    _require(token, Status.SKIPPED)
    token.status = Status.WAITING
    token.queued_at = timezone.now()
    token.save(update_fields=["status", "queued_at", "updated_at"])
    return token


@transaction.atomic
def start(token):
    _require(token, Status.CALLED)
    token.status = Status.IN_CONSULTATION
    token.started_at = timezone.now()
    token.save(update_fields=["status", "started_at", "updated_at"])
    _set_appointment(token, ApptStatus.WITH_DOCTOR)
    return token


@transaction.atomic
def complete(token):
    _require(token, Status.CALLED, Status.IN_CONSULTATION)
    token.status = Status.COMPLETED
    token.completed_at = timezone.now()
    token.started_at = token.started_at or token.completed_at
    token.save(update_fields=["status", "completed_at", "started_at", "updated_at"])
    if token.appointment and token.appointment.status == ApptStatus.IN_QUEUE:
        _set_appointment(token, ApptStatus.WITH_DOCTOR)
    _set_appointment(token, ApptStatus.COMPLETED)
    return token


@transaction.atomic
def cancel(token, *, reason):
    if not reason.strip():
        raise QueueError("A reason is required to cancel a token.")
    _require(token, *QueueToken.OPEN_STATUSES)
    token.status = Status.CANCELLED
    token.cancelled_at = timezone.now()
    token.cancel_reason = reason.strip()[:200]
    token.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
    appt = token.appointment
    if appt and appt.can_transition(ApptStatus.CANCELLED):
        appt.status = ApptStatus.CANCELLED
        appt.cancelled_at = token.cancelled_at
        appt.cancellation_reason = f"Token cancelled: {token.cancel_reason}"
        appt.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])
    return token


def stats(branch, day=None) -> dict:
    """Headline numbers for the reception desk."""
    qs = QueueToken.objects.filter(branch=branch, date=day or timezone.localdate())
    # Averaged in Python: date arithmetic in SQL differs between SQLite and PostgreSQL.
    waits = [
        (c - i).total_seconds()
        for c, i in qs.exclude(called_at=None).values_list("called_at", "created_at")
    ]
    return {
        "waiting": qs.filter(status=Status.WAITING).count(),
        "called": qs.filter(status=Status.CALLED).count(),
        "in_consultation": qs.filter(status=Status.IN_CONSULTATION).count(),
        "issued": qs.count(),
        "completed": qs.filter(status=Status.COMPLETED).count(),
        "skipped": qs.filter(status=Status.SKIPPED).count(),
        "avg_wait_minutes": int(sum(waits) / len(waits) // 60) if waits else None,
        "active_clinics": qs.filter(status=Status.WAITING).values("department").distinct().count(),
    }
