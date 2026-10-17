"""Expense and supplier-payment writes (Phase 36). Transactional and double-submit safe."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import ZERO, to_money
from apps.core.db import locked
from apps.core.models import next_number

from . import segments
from .models import Expense, ExpenseCategory


class FinanceError(ValidationError):
    pass


def ensure_categories(organization) -> None:
    if ExpenseCategory.objects.filter(organization=organization).exists():
        return
    ExpenseCategory.objects.bulk_create(
        ExpenseCategory(organization=organization, name=name, segment=seg)
        for name, seg in ExpenseCategory.DEFAULTS
    )


def _check_method(request, method):
    if method is None or method.organization_id != request.organization.pk or not method.is_active:
        raise FinanceError("Choose an active payment method.")


@transaction.atomic
def record_expense(
    *,
    request,
    category,
    amount,
    method,
    day=None,
    segment="",
    payee="",
    reference="",
    note="",
    idempotency_key="",
) -> Expense:
    if idempotency_key:
        done = Expense.objects.filter(
            organization=request.organization, idempotency_key=idempotency_key
        ).first()
        if done:
            return done
    if category.organization_id != request.organization.pk or not category.is_active:
        raise FinanceError("Choose an active expense category.")
    _check_method(request, method)
    amount = to_money(amount)
    if amount <= ZERO:
        raise FinanceError("Enter an amount greater than zero.")
    day = day or timezone.localdate()
    if day > timezone.localdate():
        raise FinanceError("Expenses cannot be dated in the future.")
    segment = segment or category.segment
    if segment not in segments.KEYS:
        raise FinanceError("Choose a valid segment.")
    if method.requires_reference and not reference.strip():
        raise FinanceError(f"{method.name} payments need a reference number.")
    org = request.organization
    try:
        with transaction.atomic():
            return Expense.objects.create(
                organization=org,
                branch=request.branch,
                number=f"EXP-{next_number(org, 'expense'):05d}",
                date=day,
                category=category,
                segment=segment,
                amount=amount,
                method=method,
                payee=payee.strip()[:120],
                reference=reference.strip()[:80],
                note=note.strip()[:200],
                created_by=request.user,
                idempotency_key=idempotency_key[:40],
            )
    except IntegrityError as err:
        raise FinanceError("This expense was already recorded.") from err


@transaction.atomic
def void_expense(expense, *, request, reason) -> Expense:
    if not reason.strip():
        raise FinanceError("A reason is required to void an expense.")
    expense = locked(expense)
    if expense.is_void:
        raise FinanceError(f"{expense.number} is already void.")
    expense.voided_at = timezone.now()
    expense.voided_by = request.user
    expense.void_reason = reason.strip()[:200]
    expense.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_at"])
    return expense


@transaction.atomic
def pay_supplier(*, request, vendor, amount, method, reference="", note="", idempotency_key=""):
    """Money paid to a supplier: a negative entry on the supplier ledger."""
    from apps.pharmacy.models import VendorLedgerEntry

    if idempotency_key:
        done = VendorLedgerEntry.objects.filter(
            organization=request.organization, idempotency_key=idempotency_key
        ).first()
        if done:
            return done
    if vendor.organization_id != request.organization.pk:
        raise FinanceError("Choose a supplier from this hospital.")
    _check_method(request, method)
    amount = to_money(amount)
    if amount <= ZERO:
        raise FinanceError("Enter an amount greater than zero.")
    if method.requires_reference and not reference.strip():
        raise FinanceError(f"{method.name} payments need a reference number.")
    try:
        with transaction.atomic():
            return VendorLedgerEntry.objects.create(
                organization=request.organization,
                branch=request.branch,
                vendor=vendor,
                kind=VendorLedgerEntry.Kind.PAYMENT,
                amount=-amount,
                method=method,
                reference=reference.strip()[:80],
                note=note.strip()[:200] or "Payment to supplier",
                created_by=request.user,
                idempotency_key=idempotency_key[:40],
            )
    except IntegrityError as err:
        raise FinanceError("This payment was already recorded.") from err
