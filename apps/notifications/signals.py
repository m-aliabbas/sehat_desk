"""Event notifications raised when records change, without the source apps knowing about them.

Each handler is idempotent (dedupe_key), so a record saved several times notifies once.
"""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse

from apps.appointments.models import Appointment
from apps.lab.models import LabOrder, LabResult

from .models import Event, Level
from .services import notify


def _later(fn):
    # Notify only once the write is committed; a rolled-back change raises nothing.
    transaction.on_commit(fn)


def _inpatient_nurses(patient, organization):
    """Nurses assigned to the patient's open admission, if any."""
    from apps.ipd.services import current_admission

    adm = current_admission(patient, organization)
    if adm is None:
        return adm, []
    return adm, [a.nurse for a in adm.nurse_assignments.filter(ended_at__isnull=True)]


@receiver(post_save, sender=Appointment, dispatch_uid="notify_appointment_cancelled")
def appointment_cancelled(sender, instance, **kwargs):
    if instance.status != Appointment.Status.CANCELLED:
        return
    appt = instance

    def send():
        notify(
            appt.organization,
            Event.APPOINTMENT_CANCELLED,
            f"Appointment cancelled · {appt.start_time:%H:%M} {appt.date:%d %b}",
            body=f"{appt.number} · MRN {appt.patient.mrn}",
            url=reverse("appointments:detail", args=[appt.pk]),
            branch=appt.branch,
            users=[appt.doctor.user],
            exclude=appt.cancelled_by,
            dedupe_key=f"appt-cancel-{appt.pk}",
        )

    _later(send)


@receiver(post_save, sender=LabOrder, dispatch_uid="notify_lab_ready")
def lab_ready(sender, instance, **kwargs):
    order = instance
    if order.status != LabOrder.Status.RELEASED or order.released_at is None:
        return

    def send():
        adm, nurses = _inpatient_nurses(order.patient, order.organization)
        users = [order.doctor.user] if order.doctor_id else []
        notify(
            order.organization,
            Event.LAB_READY,
            f"Lab report ready · {order.number}",
            body=f"MRN {order.patient.mrn}"
            + (" · amended" if order.amendment_count else "")
            + (f" · inpatient {adm.number}" if adm else ""),
            url=reverse("lab:order-detail", args=[order.pk]),
            branch=order.branch,
            users=users + nurses,
            exclude=order.released_by,
            dedupe_key=f"lab-ready-{order.pk}-{order.amendment_count}",
        )

    _later(send)


@receiver(post_save, sender=LabResult, dispatch_uid="notify_lab_critical")
def lab_critical(sender, instance, **kwargs):
    # Raised once the critical value is verified, so a typing mistake never alerts the ward.
    result = instance
    if not (result.is_current and result.is_critical and result.verified_at):
        return
    if result.critical_acknowledged_at:
        return

    def send():
        order = result.item.order
        adm, nurses = _inpatient_nurses(order.patient, order.organization)
        users = [order.doctor.user] if order.doctor_id else []
        notify(
            order.organization,
            Event.LAB_CRITICAL,
            f"Critical result · {result.item.test.name} · {order.number}",
            body=f"MRN {order.patient.mrn} · {result.get_flag_display()}. Review and act now.",
            url=reverse("lab:order-detail", args=[order.pk]),
            branch=order.branch,
            level=Level.CRITICAL,
            users=users + nurses,
            # No ordering doctor (walk-in): alert the doctors of the branch.
            roles=() if users else ("doctor",),
            dedupe_key=f"lab-critical-{result.pk}",
        )

    _later(send)
