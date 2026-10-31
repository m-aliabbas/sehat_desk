"""The front-desk visit: one step from "patient at the counter" to papers in hand.

register_visit books (today or later). A visit for today also gets its token, any vitals, and the
doctor's fee invoice. arrive does the same for a patient who booked earlier. Everything is one
transaction, so a refused payment leaves no half-made visit behind.
"""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import ZERO, Invoice, Payment
from apps.organizations.models import preferences_for
from apps.queue import services as queue_services
from apps.queue.models import QueueToken
from apps.vitals import services as vitals_services

from . import services
from .models import Appointment
from .services import BookingError

COLLECT = "billing.payment.collect"


@dataclass
class Visit:
    appointment: Appointment
    token: QueueToken | None = None
    invoice: Invoice | None = None
    payment: Payment | None = None


def _choose_time(request, doctor, on_date, shift):
    """Next free slot; a patient standing here today is seen as a walk-in if none is left.
    Returns (start, visit_type or None)."""
    slot = services.next_free_slot(doctor, request.branch, on_date, shift=shift)
    if slot:
        return slot.start, None
    if on_date == timezone.localdate():
        now = timezone.localtime().time().replace(second=0, microsecond=0)
        return now, Appointment.VisitType.WALK_IN
    raise BookingError(f"{doctor} has no free slot on {on_date:%d %b %Y}. Choose another date.")


def fee_invoice(appt) -> Invoice | None:
    return appt.invoices.exclude(status=Invoice.Status.CANCELLED).first()


def _bill_fee(request, appt) -> Invoice | None:
    """The doctor's fee invoice, when the hospital collects it up front."""
    if (
        not preferences_for(request.organization).opd_collect_fee_upfront
        or COLLECT not in request.sd_perms
        or appt.fee <= ZERO
    ):
        return None
    invoice = fee_invoice(appt)
    if invoice is None:
        invoice = billing.create_invoice(request=request, patient=appt.patient, appointment=appt)
    return billing.issue(invoice, request=request) if invoice.is_draft else invoice


def collect_fee(invoice, *, request, method, reference="", key="") -> Payment:
    if COLLECT not in request.sd_perms:
        raise BookingError("You are not allowed to collect payments.")
    return billing.receive_payment(
        invoice,
        request=request,
        method=method,
        amount=invoice.balance_due,
        reference=reference,
        idempotency_key=key,
    )


def _join_queue(request, appt, priority="normal") -> QueueToken:
    if "queue.manage" not in request.sd_perms:
        raise BookingError("You are not allowed to issue tokens.")
    return queue_services.issue_token(
        request=request,
        patient=appt.patient,
        department=appt.department,
        doctor=appt.doctor,
        appointment=appt,
        priority=priority,
    )


@transaction.atomic
def register_visit(
    *,
    request,
    patient,
    doctor,
    on_date,
    start=None,
    shift="",
    reason="",
    source=Appointment.Source.COUNTER,
    vitals=None,
    collect=False,
    method=None,
    reference="",
    key="",
) -> Visit:
    suggested = services.suggest_visit_type(doctor, patient, on_date)
    visit_type = None
    if start is None:
        start, visit_type = _choose_time(request, doctor, on_date, shift)
    appt = services.book(
        request=request,
        patient=patient,
        doctor=doctor,
        on_date=on_date,
        start=start,
        visit_type=visit_type or suggested,
        source=source,
        reason=reason,
        fee=services.default_fee(doctor, patient, suggested),
    )
    visit = Visit(appointment=appt)
    if on_date == timezone.localdate():
        visit.token = _join_queue(request, appt)
        visit.invoice = _bill_fee(request, appt)
    elif collect:  # advance payment for a later date
        visit.invoice = _bill_fee(request, appt)
    if vitals:
        vitals_services.record(request=request, patient=patient, data=vitals, token=visit.token)
    if collect and visit.invoice is not None and visit.invoice.balance_due > ZERO:
        if method is None:
            raise BookingError("Choose how the fee was paid.")
        visit.payment = collect_fee(
            visit.invoice, request=request, method=method, reference=reference, key=key
        )
    return visit


@transaction.atomic
def arrive(appt, *, request, priority="normal") -> Visit:
    """A booked patient has come in: token and fee invoice, ready for the papers."""
    if appt.date != timezone.localdate():
        raise BookingError("Only today's appointments can be checked in.")
    token = _join_queue(request, appt, priority=priority)
    appt.refresh_from_db()
    return Visit(appointment=appt, token=token, invoice=_bill_fee(request, appt))
