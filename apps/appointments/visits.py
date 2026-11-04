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


def bill_fee(request, appt) -> Invoice | None:
    """The doctor's fee invoice, when reception takes it (Settings → Patient flow)."""
    from apps.flow.services import fee_counter

    if (
        fee_counter(request.organization, request.branch) != "RECEPTION"
        or COLLECT not in request.sd_perms
        or appt.fee <= ZERO
    ):
        return None
    invoice = fee_invoice(appt)
    if invoice is None:
        invoice = billing.create_invoice(request=request, patient=appt.patient, appointment=appt)
    return billing.issue(invoice, request=request) if invoice.is_draft else invoice


@transaction.atomic
def collect_fee(
    invoice, *, request, method, reference="", key="", cash_paid=None, discount=ZERO, reason=""
) -> Payment:
    """Take the fee. A discount comes off first; cash_paid may differ by the adjustment limit."""
    if COLLECT not in request.sd_perms:
        raise BookingError("You are not allowed to collect payments.")
    if discount:
        invoice = billing.set_invoice_discount(
            invoice, request=request, amount=discount, reason=reason, before_payment=True
        )
    return billing.settle_cash(
        invoice,
        request=request,
        method=method,
        cash_paid=invoice.balance_due if cash_paid is None else cash_paid,
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
    cash_paid=None,
    discount=ZERO,
    discount_reason="",
    emergency=False,
) -> Visit:
    """emergency: seen now whatever the slots or leave say, first in the queue, E- token."""
    if emergency:
        on_date = timezone.localdate()
    suggested = services.suggest_visit_type(doctor, patient, on_date)
    visit_type = None
    if emergency:
        start = timezone.localtime().time().replace(second=0, microsecond=0)
        visit_type = Appointment.VisitType.EMERGENCY
    elif start is None:
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
        visit.token = _join_queue(
            request, appt, priority=QueueToken.Priority.EMERGENCY if emergency else "normal"
        )
        visit.invoice = bill_fee(request, appt)
    elif collect:  # advance payment for a later date
        visit.invoice = bill_fee(request, appt)
    if vitals:
        vitals_services.record(request=request, patient=patient, data=vitals, token=visit.token)
    if collect and visit.invoice is not None and visit.invoice.balance_due > ZERO:
        if method is None:
            raise BookingError("Choose how the fee was paid.")
        visit.payment = collect_fee(
            visit.invoice,
            request=request,
            method=method,
            reference=reference,
            key=key,
            cash_paid=cash_paid,
            discount=discount,
            reason=discount_reason,
        )
    return visit


@transaction.atomic
def arrive(appt, *, request, priority="normal") -> Visit:
    """A booked patient has come in: token and fee invoice, ready for the papers."""
    if appt.date != timezone.localdate():
        raise BookingError("Only today's appointments can be checked in.")
    token = _join_queue(request, appt, priority=priority)
    appt.refresh_from_db()
    return Visit(appointment=appt, token=token, invoice=bill_fee(request, appt))
