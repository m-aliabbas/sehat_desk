"""Ordering, completing and cancelling patient procedures. Views call these only."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import InvoiceItem, to_money
from apps.core.db import locked
from apps.core.models import next_number

from .models import PatientProcedure

Status = PatientProcedure.Status
PRICE_OVERRIDE = "procedure.price.override"


class ProcedureError(ValidationError):
    pass


@transaction.atomic
def order(
    *,
    request,
    patient,
    service,
    performed_by=None,
    encounter=None,
    quantity=1,
    unit_price=None,
    price_reason="",
    notes="",
) -> PatientProcedure:
    """Add a procedure to a patient and issue its invoice, all or nothing."""
    org = request.organization
    if patient.organization_id != org.pk:
        raise ProcedureError("That patient is not in this hospital.")
    if service.organization_id != org.pk or not service.is_active or not service.is_procedure:
        raise ProcedureError("Choose an active procedure.")
    if encounter is not None:
        if encounter.patient_id != patient.pk:
            raise ProcedureError("That consultation belongs to another patient.")
        performed_by = performed_by or encounter.doctor
    if performed_by is not None and performed_by.organization_id != org.pk:
        raise ProcedureError("Choose a doctor from this hospital.")
    if quantity < 1:
        raise ProcedureError("Quantity must be at least 1.")

    list_price = to_money(service.price_for(request.branch))
    price = list_price if unit_price in (None, "") else to_money(unit_price)
    reason = price_reason.strip()[:200]
    if price < 0:
        raise ProcedureError("Give a price of zero or more.")
    if price != list_price:
        if not service.price_editable:
            raise ProcedureError(f"The price of {service.name} is fixed at {list_price}.")
        if PRICE_OVERRIDE not in request.sd_perms:
            raise ProcedureError("You are not allowed to change procedure prices.")
        if not reason:
            raise ProcedureError("Give a reason for the changed price.")
    else:
        reason = ""

    proc = PatientProcedure.objects.create(
        organization=org,
        branch=request.branch,
        number=f"PRC-{next_number(org, 'procedure'):05d}",
        patient=patient,
        encounter=encounter,
        service=service,
        name=service.name,
        performed_by=performed_by,
        quantity=quantity,
        list_price=list_price,
        unit_price=price,
        price_change_reason=reason,
        notes=notes.strip()[:300],
        created_by=request.user,
    )
    if price > Decimal("0"):
        invoice = billing.create_invoice(
            request=request, patient=patient, notes=f"Procedure {proc.number}"
        )
        description = proc.name + (f" · {proc.notes}" if proc.notes else "")
        billing.add_item(
            invoice,
            request=request,
            service=service,
            kind=InvoiceItem.Kind.PROCEDURE,
            description=description[:160],
            unit_price=price,
            quantity=quantity,
            performed_by=performed_by.user if performed_by else None,
        )
        proc.invoice = billing.issue(invoice, request=request)
        proc.save(update_fields=["invoice", "updated_at"])
    return proc


@transaction.atomic
def complete(proc, *, request, findings="") -> PatientProcedure:
    proc = locked(proc)
    if proc.status != Status.ORDERED:
        raise ProcedureError(f"{proc.number} is {proc.get_status_display().lower()}.")
    proc.status = Status.COMPLETED
    proc.findings = findings.strip()
    proc.completed_at = timezone.now()
    proc.completed_by = request.user
    proc.save(update_fields=["status", "findings", "completed_at", "completed_by", "updated_at"])
    return proc


@transaction.atomic
def cancel(proc, *, request, reason) -> PatientProcedure:
    """Cancel a procedure that has not been done. Paid invoices are refunded in Billing first."""
    if not reason.strip():
        raise ProcedureError("A reason is required to cancel a procedure.")
    proc = locked(proc)
    if proc.status != Status.ORDERED:
        raise ProcedureError(f"{proc.number} is {proc.get_status_display().lower()}.")
    if proc.invoice_id:
        invoice = proc.invoice
        if invoice.net_paid > 0:
            raise ProcedureError(
                f"{invoice.number} has payments. Refund them in Billing first, then cancel."
            )
        billing.void_unpaid(invoice, user=request.user, reason=f"{proc.number}: {reason}")
    proc.status = Status.CANCELLED
    proc.cancelled_at = timezone.now()
    proc.cancelled_by = request.user
    proc.cancel_reason = reason.strip()[:200]
    proc.save(
        update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason", "updated_at"]
    )
    return proc
