"""Doctor commission with the clinic (Phase 35).

A line earns commission when its invoice is fully paid and the line names a doctor
(InvoiceItem.performed_by) who has an active pay profile with a rate for that kind of line:
consultation %, procedure % (or a per-procedure rule), lab referral %.

sync_invoice() reconciles: the doctor's total for each line should equal
    share × (net paid / grand total)      for paid or refunded invoices
    0                                     for any other status
and posts the difference as a new row, so it is idempotent and never edits old rows.
The share keeps the rate of the line's first earning row.
"""

from decimal import Decimal

from django.db.models import Sum

from apps.billing.models import CENT, ZERO, Invoice, InvoiceItem, to_money
from apps.finance import segments

from .models import CommissionEarning, CommissionRule, PayProfile

PAID_STATUSES = (Invoice.Status.PAID, Invoice.Status.REFUNDED)
RATE_FIELD = {
    InvoiceItem.Kind.CONSULTATION: "consultation_percent",
    InvoiceItem.Kind.PROCEDURE: "procedure_percent",
    InvoiceItem.Kind.LAB: "lab_referral_percent",
}


def rate_for(profile, item) -> tuple[Decimal, Decimal]:
    """(percent, fixed amount per unit) for this line under this profile."""
    if item.service_id:
        rule = CommissionRule.objects.filter(profile=profile, service_id=item.service_id).first()
        if rule:
            return rule.percent, rule.fixed_amount
    field = RATE_FIELD.get(item.kind)
    return (getattr(profile, field) if field else ZERO), ZERO


def line_bases(invoice) -> dict:
    """Net amount (before tax, after line and allocated invoice discount) per item id."""
    items = list(invoice.items.all())
    lines = sum((i.line_total for i in items), ZERO)
    out = {}
    for i in items:
        net = i.line_total - i.tax_amount
        share = (invoice.invoice_discount * i.line_total / lines) if lines else ZERO
        out[i.pk] = to_money(net - share)
    return out


def _paid_factor(invoice) -> Decimal:
    if invoice.status not in PAID_STATUSES or invoice.grand_total <= ZERO:
        return ZERO
    return min(Decimal("1"), max(ZERO, invoice.net_paid / invoice.grand_total))


def sync_invoice(invoice) -> list[CommissionEarning]:
    """Bring commission rows for this invoice in line with its payments. Call inside the
    transaction that changed the invoice (the billing post_save signal does)."""
    invoice = Invoice.objects.get(pk=invoice.pk)  # never trust a stale copy with money
    items = [i for i in invoice.items.all() if i.performed_by_id]
    if not items:
        return []
    factor = _paid_factor(invoice)
    existing = {}
    for e in CommissionEarning.objects.filter(invoice=invoice).order_by("pk"):
        existing.setdefault(e.invoice_item_id, []).append(e)
    if factor == ZERO and not existing:
        return []
    profiles = {
        p.user_id: p
        for p in PayProfile.objects.filter(
            user_id__in={i.performed_by_id for i in items}, is_active=True
        )
    }
    bases = line_bases(invoice)
    created = []
    for item in items:
        rows = existing.get(item.pk, [])
        if rows:
            first = rows[0]
            pct, fixed, basis = first.percent, first.fixed_amount, first.basis
        else:
            profile = profiles.get(item.performed_by_id)
            if profile is None:
                continue
            pct, fixed = rate_for(profile, item)
            basis = bases[item.pk]
        full = to_money(basis * pct / 100 + fixed * item.quantity)
        if full <= ZERO and not rows:
            continue
        target = to_money(full * factor)
        have = sum((r.amount for r in rows), ZERO)
        delta = target - have
        if abs(delta) < CENT:
            continue
        created.append(
            CommissionEarning.objects.create(
                organization_id=invoice.organization_id,
                branch_id=invoice.branch_id,
                user_id=item.performed_by_id,
                invoice=invoice,
                invoice_item=item,
                kind=CommissionEarning.Kind.ADJUSTMENT if rows else CommissionEarning.Kind.EARNING,
                segment=segments.for_item_kind(item.kind),
                basis=basis if not rows else ZERO,
                percent=pct,
                fixed_amount=fixed,
                amount=delta,
            )
        )
    return created


def unsettled(user, *, before=None):
    qs = CommissionEarning.objects.filter(user=user, payslip__isnull=True)
    if before is not None:
        qs = qs.filter(created_at__lt=before)
    return qs


def unsettled_total(user, *, before=None) -> Decimal:
    return unsettled(user, before=before).aggregate(s=Sum("amount"))["s"] or ZERO
