"""Billing operations. Views call these; they never change invoice money fields directly.

Every write runs in a transaction and re-reads the invoice row with select_for_update, so two
desks paying the same invoice cannot both take the last balance (a no-op lock on SQLite, which
serializes writers anyway; a real row lock on PostgreSQL).

draft ──issue──▶ unpaid ──pay──▶ partially_paid ──pay──▶ paid ──refund all──▶ refunded
  └─cancel (no money held)──▶ cancelled ◀──cancel── unpaid
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.core.db import locked
from apps.core.models import next_number

from .models import (
    CENT,
    ZERO,
    BillingSettings,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentMethod,
    Refund,
    to_money,
)

Status = Invoice.Status


class BillingError(ValidationError):
    pass


# --- Configuration -----------------------------------------------------------------------------


def settings_for(organization) -> BillingSettings:
    obj, _ = BillingSettings.objects.get_or_create(organization=organization)
    return obj


def ensure_payment_methods(organization) -> None:
    """Create the default payment methods the first time an organization needs them."""
    if PaymentMethod.objects.filter(organization=organization).exists():
        return
    PaymentMethod.objects.bulk_create(
        PaymentMethod(
            organization=organization, code=code, name=name, requires_reference=ref, sort_order=i
        )
        for i, (code, name, ref) in enumerate(PaymentMethod.DEFAULTS)
    )


def active_methods(organization):
    ensure_payment_methods(organization)
    return PaymentMethod.objects.filter(organization=organization, is_active=True)


# --- Invoices ----------------------------------------------------------------------------------


def _lock(invoice) -> Invoice:
    return locked(invoice)


def _status_from_money(invoice) -> str:
    if invoice.amount_paid > ZERO and invoice.amount_refunded >= invoice.amount_paid:
        return Status.REFUNDED
    if invoice.amount_paid >= invoice.grand_total:
        return Status.PAID
    if invoice.amount_paid > ZERO:
        return Status.PARTIALLY_PAID
    return Status.UNPAID


def recalculate(invoice) -> Invoice:
    """Recompute stored totals from the items. Call inside a transaction after any change."""
    items = list(invoice.items.all())
    invoice.subtotal = sum((i.gross for i in items), ZERO)
    invoice.line_discount_total = sum((to_money(i.discount) for i in items), ZERO)
    invoice.tax_total = sum((i.tax_amount for i in items), ZERO)
    lines = sum((i.line_total for i in items), ZERO)
    invoice.invoice_discount = min(to_money(invoice.invoice_discount), lines)
    invoice.grand_total = lines - invoice.invoice_discount
    invoice.save(
        update_fields=[
            "subtotal",
            "line_discount_total",
            "tax_total",
            "invoice_discount",
            "grand_total",
            "updated_at",
        ]
    )
    return invoice


@transaction.atomic
def create_invoice(*, request, patient, appointment=None, notes="", customer_name="") -> Invoice:
    """Start a draft invoice. With an appointment, its consultation fee becomes the first line.
    patient may be None only for a walk-in pharmacy sale, which names the customer instead."""
    if patient is None and appointment is not None:
        raise BillingError("A visit invoice needs the patient.")
    if appointment is not None:
        if appointment.patient_id != patient.pk:
            raise BillingError("The appointment belongs to another patient.")
        open_invoice = appointment.invoices.exclude(status=Status.CANCELLED).first()
        if open_invoice:
            raise BillingError(f"This visit is already billed on {open_invoice.number}.")
    org = request.organization
    number = next_number(org, "invoice")
    invoice = Invoice.objects.create(
        organization=org,
        branch=request.branch,
        number=f"{org.receipt_prefix}-{number:05d}",
        patient=patient,
        customer_name="" if patient else customer_name.strip()[:120],
        appointment=appointment,
        notes=notes[:300],
        created_by=request.user,
    )
    if appointment is not None and appointment.fee > ZERO:
        add_item(
            invoice,
            request=request,
            kind=InvoiceItem.Kind.CONSULTATION,
            description=f"Consultation · {appointment.doctor}",
            unit_price=appointment.fee,
            performed_by=appointment.doctor.user,
        )
        invoice.refresh_from_db()
    return invoice


def _require_draft(invoice):
    if invoice.status != Status.DRAFT:
        raise BillingError(
            f"{invoice.number} is {invoice.get_status_display().lower()}; its items are locked."
        )


@transaction.atomic
def add_item(
    invoice,
    *,
    request,
    service=None,
    description="",
    unit_price=None,
    quantity=1,
    kind=None,
    tax_percent=None,
    performed_by=None,
) -> InvoiceItem:
    invoice = _lock(invoice)
    _require_draft(invoice)
    if quantity < 1:
        raise BillingError("Quantity must be at least 1.")
    if service is not None:
        if service.organization_id != invoice.organization_id or not service.is_active:
            raise BillingError("That service is not available.")
        description = description or service.name
        unit_price = service.price_for(invoice.branch) if unit_price is None else unit_price
        kind = kind or (
            InvoiceItem.Kind.PROCEDURE if service.is_procedure else InvoiceItem.Kind.SERVICE
        )
    if performed_by is not None and performed_by.organization_id != invoice.organization_id:
        raise BillingError("That doctor is not in this hospital.")
    if not description.strip():
        raise BillingError("Describe the item.")
    if unit_price is None or Decimal(unit_price) < 0:
        raise BillingError("Give a price of zero or more.")
    if tax_percent is None:
        tax_percent = settings_for(invoice.organization).default_tax_percent
    item = InvoiceItem(
        invoice=invoice,
        kind=kind or InvoiceItem.Kind.MISC,
        service=service,
        description=description.strip()[:160],
        quantity=quantity,
        unit_price=to_money(unit_price),
        tax_percent=tax_percent,
        performed_by=performed_by,
        added_by=request.user,
    )
    item.compute()
    item.save()
    recalculate(invoice)
    return item


@transaction.atomic
def remove_item(item) -> Invoice:
    invoice = _lock(item.invoice)
    _require_draft(invoice)
    item.delete()  # drafts hold no money yet; removing a line is not a financial correction
    return recalculate(invoice)


def check_discount_cap(organization, amount, base, error=None) -> None:
    """Refuse a discount above Settings → Billing → largest discount (% of base)."""
    cap = settings_for(organization).max_discount_percent
    if base > 0 and amount > (to_money(base) * cap / 100).quantize(CENT):
        raise (error or BillingError)(
            f"Discounts above {cap.normalize():f}% are not allowed (Settings → Billing)."
        )


def _require_discount_permission(request):
    if "billing.discount" not in request.sd_perms:
        raise BillingError("You are not allowed to give discounts.")


@transaction.atomic
def set_line_discount(item, *, request, amount, reason) -> InvoiceItem:
    _require_discount_permission(request)
    invoice = _lock(item.invoice)
    _require_draft(invoice)
    amount = to_money(amount)
    if amount < 0 or amount > item.gross:
        raise BillingError(f"The discount must be between 0 and {item.gross}.")
    check_discount_cap(invoice.organization, amount, item.gross)
    if amount > 0 and not reason.strip():
        raise BillingError("Give a reason for the discount.")
    item.discount = amount
    item.discount_reason = reason.strip()[:200] if amount else ""
    item.compute()
    item.save(update_fields=["discount", "discount_reason", "tax_amount", "line_total"])
    invoice.discount_by = request.user if amount else invoice.discount_by
    invoice.save(update_fields=["discount_by", "updated_at"])
    recalculate(invoice)
    return item


@transaction.atomic
def set_invoice_discount(invoice, *, request, amount, reason, authorized_by=None) -> Invoice:
    """authorized_by: a discount already approved elsewhere by a user holding the right
    permission (a pharmacy cart discount needs pharmacy.discount, checked by pharmacy/pos.py).
    Without it the signed-in user needs billing.discount."""
    if authorized_by is None:
        _require_discount_permission(request)
    invoice = _lock(invoice)
    _require_draft(invoice)
    amount = to_money(amount)
    lines = invoice.grand_total + invoice.invoice_discount
    if amount < 0 or amount > lines:
        raise BillingError(f"The discount must be between 0 and {lines}.")
    check_discount_cap(invoice.organization, amount, lines)
    if amount > 0 and not reason.strip():
        raise BillingError("Give a reason for the discount.")
    invoice.invoice_discount = amount
    invoice.discount_reason = reason.strip()[:200] if amount else ""
    invoice.discount_by = (authorized_by or request.user) if amount else None
    invoice.save(update_fields=["invoice_discount", "discount_reason", "discount_by", "updated_at"])
    return recalculate(invoice)


@transaction.atomic
def issue(invoice, *, request) -> Invoice:
    """Lock the items. From now on the invoice is a financial record."""
    invoice = _lock(invoice)
    _require_draft(invoice)
    if not invoice.items.exists():
        raise BillingError("Add at least one item before issuing the invoice.")
    recalculate(invoice)
    invoice.issued_at = timezone.now()
    invoice.issued_by = request.user
    invoice.status = _status_from_money(invoice)
    invoice.save(update_fields=["issued_at", "issued_by", "status", "updated_at"])
    return invoice


@transaction.atomic
def cancel(invoice, *, request, reason) -> Invoice:
    """Void an invoice that holds no money. Paid invoices are corrected by refund first."""
    if not reason.strip():
        raise BillingError("A reason is required to cancel an invoice.")
    invoice = _lock(invoice)
    if invoice.status == Status.CANCELLED:
        raise BillingError(f"{invoice.number} is already cancelled.")
    if invoice.status != Status.DRAFT and "billing.refund" not in request.sd_perms:
        raise BillingError("Only staff who can issue refunds may cancel an issued invoice.")
    return _void(invoice, user=request.user, reason=reason)


@transaction.atomic
def void_unpaid(invoice, *, user, reason) -> Invoice:
    """Cancel an invoice that holds no money because the work it bills was cancelled (e.g. a
    lab order). No billing permission is needed: no money moves, and _void re-checks that."""
    invoice = _lock(invoice)
    if invoice.status == Status.CANCELLED:
        return invoice
    return _void(invoice, user=user, reason=reason)


def _void(invoice, *, user, reason) -> Invoice:
    if invoice.net_paid > ZERO:
        raise BillingError("Refund the payments on this invoice before cancelling it.")
    invoice.status = Status.CANCELLED
    invoice.cancelled_at = timezone.now()
    invoice.cancelled_by = user
    invoice.cancel_reason = reason.strip()[:200]
    invoice.save(
        update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason", "updated_at"]
    )
    return invoice


# --- Payments ----------------------------------------------------------------------------------


@transaction.atomic
def receive_payment(
    invoice, *, request, method, amount, reference="", note="", idempotency_key=""
) -> Payment:
    """Record money received. A draft is issued first. Partial payments are allowed."""
    if idempotency_key:
        done = Payment.objects.filter(
            organization=request.organization, idempotency_key=idempotency_key
        ).first()
        if done:
            return done  # the same form was submitted twice; never charge twice
    invoice = _lock(invoice)
    if invoice.status == Status.DRAFT:
        invoice = issue(invoice, request=request)
    if invoice.status not in Invoice.OUTSTANDING_STATUSES:
        raise BillingError(f"{invoice.number} is {invoice.get_status_display().lower()}.")
    if method.organization_id != invoice.organization_id or not method.is_active:
        raise BillingError("Choose an active payment method.")
    amount = to_money(amount)
    if amount <= ZERO:
        raise BillingError("Enter an amount greater than zero.")
    if amount > invoice.balance_due:
        raise BillingError(f"The balance due is only {invoice.balance_due}.")
    if method.requires_reference and not reference.strip():
        raise BillingError(f"{method.name} payments need a reference number.")
    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                organization=invoice.organization,
                branch=request.branch,
                number=f"RCT-{next_number(invoice.organization, 'payment'):05d}",
                invoice=invoice,
                method=method,
                amount=amount,
                reference=reference.strip()[:80],
                note=note.strip()[:200],
                received_by=request.user,
                idempotency_key=idempotency_key[:40],
            )
    except IntegrityError as err:  # a parallel submit of the same form won the race
        raise BillingError("This payment was already recorded.") from err
    invoice.amount_paid += amount
    invoice.status = _status_from_money(invoice)
    invoice.save(update_fields=["amount_paid", "status", "updated_at"])
    return payment


# --- Refunds -----------------------------------------------------------------------------------


@transaction.atomic
def refund(
    payment,
    *,
    request,
    amount,
    reason,
    method=None,
    reference="",
    idempotency_key="",
    permission="billing.refund",
):
    """Return money from an original payment. Needs billing.refund and a reason.
    permission: pharmacy returns pass "pharmacy.refund" (the POS return screen)."""
    if permission not in request.sd_perms:
        raise BillingError("You are not allowed to issue refunds.")
    if idempotency_key:
        done = Refund.objects.filter(
            organization=request.organization, idempotency_key=idempotency_key
        ).first()
        if done:
            return done
    if not reason.strip():
        raise BillingError("A reason is required for a refund.")
    invoice = _lock(payment.invoice)
    payment = Payment.objects.select_for_update(of=("self",)).get(pk=payment.pk)
    amount = to_money(amount)
    if amount <= ZERO:
        raise BillingError("Enter an amount greater than zero.")
    if amount > payment.refundable:
        raise BillingError(f"Only {payment.refundable} of {payment.number} can still be refunded.")
    method = method or payment.method
    if method.organization_id != invoice.organization_id:
        raise BillingError("Choose a valid refund method.")
    try:
        with transaction.atomic():
            refund_row = Refund.objects.create(
                organization=invoice.organization,
                branch=request.branch,
                number=f"RF-{next_number(invoice.organization, 'refund'):05d}",
                invoice=invoice,
                payment=payment,
                method=method,
                amount=amount,
                reason=reason.strip()[:200],
                reference=reference.strip()[:80],
                refunded_by=request.user,
                idempotency_key=idempotency_key[:40],
            )
    except IntegrityError as err:
        raise BillingError("This refund was already recorded.") from err
    invoice.amount_refunded += amount
    invoice.status = _status_from_money(invoice)
    invoice.save(update_fields=["amount_refunded", "status", "updated_at"])
    return refund_row


# --- Day totals --------------------------------------------------------------------------------


# Inpatient deposits (apps.ipd AdmissionDeposit) are cash on the day they are taken. At discharge
# each one is applied to the final bill as a Payment whose idempotency key starts with this
# prefix; that Payment only moves money already in the till, so day totals skip it.
DEPOSIT_PAYMENT_KEY_PREFIX = "deposit-"


def applied_deposit_payments(payments):
    return payments.filter(idempotency_key__startswith=DEPOSIT_PAYMENT_KEY_PREFIX)


def day_totals(branch, day: date | None = None) -> dict:
    """Money in and out of one branch on one day, by payment method.

    In: payments (except applied deposits) plus inpatient deposits received that day.
    Out: refunds plus unused deposit balances handed back that day.
    """
    from apps.ipd.models import AdmissionDeposit

    day = day or timezone.localdate()
    payments = Payment.objects.filter(branch=branch, created_at__date=day).exclude(
        idempotency_key__startswith=DEPOSIT_PAYMENT_KEY_PREFIX
    )
    refunds = Refund.objects.filter(branch=branch, created_at__date=day)
    deposits = AdmissionDeposit.objects.filter(branch=branch, created_at__date=day)
    returns = AdmissionDeposit.objects.filter(
        branch=branch, returned_at__date=day, returned_amount__gt=ZERO
    )

    by_method: dict = {}

    def row(name):
        return by_method.setdefault(
            name,
            {"collected": ZERO, "count": 0, "deposits": 0, "refunded": ZERO, "returned": ZERO},
        )

    methods = (
        list(payments.values_list("method__name", "method__sort_order"))
        + list(deposits.values_list("method__name", "method__sort_order"))
        + list(returns.values_list("method__name", "method__sort_order"))
        + list(refunds.values_list("method__name", "method__sort_order"))
    )
    for name, _ in sorted(set(methods), key=lambda m: (m[1], m[0])):
        row(name)
    for r in payments.values("method__name").annotate(s=Sum("amount"), n=Count("id")):
        row(r["method__name"])["collected"] += r["s"]
        row(r["method__name"])["count"] += r["n"]
    for r in deposits.values("method__name").annotate(s=Sum("amount"), n=Count("id")):
        row(r["method__name"])["collected"] += r["s"]
        row(r["method__name"])["deposits"] += r["n"]
    for r in refunds.values("method__name").annotate(s=Sum("amount")):
        row(r["method__name"])["refunded"] += r["s"]
    for r in returns.values("method__name").annotate(s=Sum("returned_amount")):
        row(r["method__name"])["refunded"] += r["s"]
        row(r["method__name"])["returned"] += r["s"]
    for v in by_method.values():
        v["net"] = v["collected"] - v["refunded"]

    deposit_in = deposits.aggregate(s=Sum("amount"))["s"] or ZERO
    deposit_out = returns.aggregate(s=Sum("returned_amount"))["s"] or ZERO
    collected = (payments.aggregate(s=Sum("amount"))["s"] or ZERO) + deposit_in
    refunded = (refunds.aggregate(s=Sum("amount"))["s"] or ZERO) + deposit_out
    return {
        "collected": collected,
        "refunded": refunded,
        "net": collected - refunded,
        "count": payments.count(),
        "deposits": {
            "received": deposit_in,
            "received_count": deposits.count(),
            "returned": deposit_out,
            "returned_count": returns.count(),
        },
        "by_method": by_method,
    }


def net_by_day(branch, first: date, last: date) -> dict:
    """Net collection per local day, first..last inclusive, in four queries.

    Same rules as day_totals()["net"] (which runs ~16 queries for one day); used by trends.
    Rows are bucketed by local date in Python, so it is portable between SQLite and PostgreSQL.
    """
    from datetime import datetime, time, timedelta

    from apps.ipd.models import AdmissionDeposit

    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz)
    totals = {first + timedelta(days=i): ZERO for i in range((last - first).days + 1)}

    def add(rows, sign):
        for when, amount in rows:
            day = timezone.localtime(when, tz).date()
            if day in totals:
                totals[day] += sign * (amount or ZERO)

    window = {"branch": branch, "created_at__gte": start, "created_at__lt": end}
    add(
        Payment.objects.filter(**window)
        .exclude(idempotency_key__startswith=DEPOSIT_PAYMENT_KEY_PREFIX)
        .values_list("created_at", "amount"),
        1,
    )
    add(AdmissionDeposit.objects.filter(**window).values_list("created_at", "amount"), 1)
    add(Refund.objects.filter(**window).values_list("created_at", "amount"), -1)
    add(
        AdmissionDeposit.objects.filter(
            branch=branch, returned_at__gte=start, returned_at__lt=end, returned_amount__gt=ZERO
        ).values_list("returned_at", "returned_amount"),
        -1,
    )
    return totals
