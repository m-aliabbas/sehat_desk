"""The only code that changes stock. Every change locks the batch row, checks for negative
stock and writes a StockTransaction in the same database transaction.

`select_for_update` is a no-op on SQLite (which serialises writers anyway). On PostgreSQL it
takes a row lock, so two tills selling the last strip cannot both succeed.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from .models import InventoryBatch, Product, StockTransaction

Kind = StockTransaction.Kind


class StockError(ValidationError):
    pass


def _need_manage(request):
    if "pharmacy.inventory.manage" not in request.sd_perms:
        raise StockError("You are not allowed to change stock.")


def _post(batch, *, request, kind, delta: int, reason="", reference="", original=None):
    """Apply a signed quantity change to one batch and log it. Call inside a transaction."""
    if delta == 0:
        raise StockError("The quantity must not be zero.")
    batch = InventoryBatch.objects.select_for_update(of=("self",)).get(pk=batch.pk)
    if batch.branch_id != request.branch.pk:
        raise StockError("That batch belongs to another branch.")
    new_balance = batch.quantity_available + delta
    if new_balance < 0:
        raise StockError(
            f"Only {batch.quantity_available} {batch.product.get_unit_display().lower()}(s) "
            f"left in batch {batch.batch_number}."
        )
    try:
        with transaction.atomic():
            InventoryBatch.objects.filter(pk=batch.pk).update(
                quantity_available=F("quantity_available") + delta, updated_at=timezone.now()
            )
    except IntegrityError as err:  # the database check constraint caught a race
        raise StockError("Not enough stock in this batch.") from err
    batch.refresh_from_db(fields=["quantity_available", "quantity_received", "updated_at"])
    return StockTransaction.objects.create(
        organization=batch.organization,
        branch=batch.branch,
        kind=kind,
        product=batch.product,
        batch=batch,
        quantity=delta,
        balance_after=new_balance,
        unit_cost=batch.purchase_cost,
        reference=reference.strip()[:80],
        reason=reason.strip()[:200],
        original=original,
        created_by=request.user,
    )


@transaction.atomic
def receive(
    *,
    request,
    product,
    batch_number,
    expiry_date,
    quantity: int,
    purchase_cost,
    sale_price=None,
    reference="",
    opening=False,
) -> StockTransaction:
    """Purchase stock-in (or opening stock). Adds to an existing batch with the same number."""
    _need_manage(request)
    if product.organization_id != request.organization.id or not product.is_active:
        raise StockError("That product is not available.")
    if quantity <= 0:
        raise StockError("Enter a quantity greater than zero.")
    batch_number = batch_number.strip().upper()
    if not batch_number:
        raise StockError("Enter the batch number printed on the pack.")
    if expiry_date <= timezone.localdate():
        raise StockError("This stock is already expired. Do not receive it into saleable stock.")
    sale_price = product.sale_price if sale_price in (None, "") else sale_price
    batch, created = InventoryBatch.objects.select_for_update(of=("self",)).get_or_create(
        branch=request.branch,
        product=product,
        batch_number=batch_number,
        defaults={
            "organization": request.organization,
            "expiry_date": expiry_date,
            "purchase_cost": purchase_cost,
            "sale_price": sale_price,
        },
    )
    if not created and batch.expiry_date != expiry_date:
        raise StockError(
            f"Batch {batch_number} is already recorded with expiry {batch.expiry_date:%d %b %Y}."
        )
    InventoryBatch.objects.filter(pk=batch.pk).update(
        quantity_received=F("quantity_received") + quantity
    )
    return _post(
        batch,
        request=request,
        kind=Kind.OPENING if opening else Kind.PURCHASE,
        delta=quantity,
        reference=reference,
    )


@transaction.atomic
def sell(batch, *, request, quantity: int, reference="") -> StockTransaction:
    """Sale stock-out (used by the POS, Phase 13). Expired batches cannot be sold."""
    if not ({"pharmacy.sale", "pharmacy.inventory.manage"} & set(request.sd_perms)):
        raise StockError("You are not allowed to sell stock.")
    if quantity <= 0:
        raise StockError("Enter a quantity greater than zero.")
    if batch.is_expired:
        raise StockError(f"Batch {batch.batch_number} expired on {batch.expiry_date:%d %b %Y}.")
    return _post(batch, request=request, kind=Kind.SALE, delta=-quantity, reference=reference)


@transaction.atomic
def sale_return(sale_txn, *, request, quantity: int, reason: str) -> StockTransaction:
    """Return part or all of a sale to the batch it came from."""
    if "pharmacy.refund" not in request.sd_perms:
        raise StockError("You are not allowed to process returns.")
    if sale_txn.kind != Kind.SALE:
        raise StockError("Only sales can be returned.")
    if not reason.strip():
        raise StockError("Give the reason for the return.")
    returned = -(sale_txn.returns.aggregate(s=Sum("quantity"))["s"] or 0)
    sold = -sale_txn.quantity
    if quantity <= 0 or quantity > sold + returned:
        raise StockError(f"Only {sold + returned} unit(s) of this sale can still be returned.")
    return _post(
        sale_txn.batch,
        request=request,
        kind=Kind.SALE_RETURN,
        delta=quantity,
        reason=reason,
        reference=sale_txn.reference,
        original=sale_txn,
    )


@transaction.atomic
def purchase_return(batch, *, request, quantity: int, reason: str, reference=""):
    _need_manage(request)
    if not reason.strip():
        raise StockError("Give the reason for returning stock to the supplier.")
    if quantity <= 0:
        raise StockError("Enter a quantity greater than zero.")
    return _post(
        batch,
        request=request,
        kind=Kind.PURCHASE_RETURN,
        delta=-quantity,
        reason=reason,
        reference=reference,
    )


@transaction.atomic
def adjust_to_count(batch, *, request, counted: int, reason: str) -> StockTransaction:
    """Physical count: set the batch to what is on the shelf, logging the difference."""
    _need_manage(request)
    if counted < 0:
        raise StockError("The counted quantity cannot be negative.")
    if not reason.strip():
        raise StockError("Give the reason for the adjustment.")
    current = (
        InventoryBatch.objects.select_for_update(of=("self",)).get(pk=batch.pk).quantity_available
    )
    if counted == current:
        raise StockError("The count matches the system stock; nothing to adjust.")
    return _post(
        batch, request=request, kind=Kind.ADJUSTMENT, delta=counted - current, reason=reason
    )


@transaction.atomic
def write_off(batch, *, request, quantity: int, kind: str, reason: str) -> StockTransaction:
    _need_manage(request)
    if kind not in (Kind.EXPIRED, Kind.DAMAGED):
        raise StockError("Choose expired or damaged.")
    if not reason.strip():
        raise StockError("Give the reason for the write-off.")
    if quantity <= 0:
        raise StockError("Enter a quantity greater than zero.")
    return _post(batch, request=request, kind=kind, delta=-quantity, reason=reason)


# --- Queries -----------------------------------------------------------------------------------


def saleable(branch, product=None, *, today=None):
    """Batches that can be sold: in stock and not expired, first-expiry first (FEFO)."""
    today = today or timezone.localdate()
    qs = InventoryBatch.objects.filter(
        branch=branch, quantity_available__gt=0, expiry_date__gte=today
    ).select_related("product")
    if product is not None:
        qs = qs.filter(product=product)
    return qs.order_by("expiry_date", "pk")


def fefo_allocate(
    branch, product, quantity: int, *, today=None
) -> list[tuple[InventoryBatch, int]]:
    """Which batches to take `quantity` units from, first expiry first. Raises if short."""
    if quantity <= 0:
        raise StockError("Enter a quantity greater than zero.")
    plan, remaining = [], quantity
    for batch in saleable(branch, product, today=today):
        take = min(batch.quantity_available, remaining)
        plan.append((batch, take))
        remaining -= take
        if remaining == 0:
            return plan
    have = quantity - remaining
    raise StockError(f"Only {have} unit(s) of {product} in saleable stock.")


def on_hand(branch, product) -> int:
    return saleable(branch, product).aggregate(s=Sum("quantity_available"))["s"] or 0


def products_with_stock(branch, organization):
    """Products annotated with saleable and expired stock in one branch."""
    today = timezone.localdate()
    return Product.objects.filter(organization=organization).annotate(
        stock=Sum(
            "batches__quantity_available",
            filter=Q(batches__branch=branch, batches__expiry_date__gte=today),
            default=0,
        ),
        expired_stock=Sum(
            "batches__quantity_available",
            filter=Q(batches__branch=branch, batches__expiry_date__lt=today),
            default=0,
        ),
    )


def low_stock(branch, organization):
    return (
        products_with_stock(branch, organization)
        .filter(is_active=True, reorder_level__gt=0, stock__lte=F("reorder_level"))
        .order_by("stock", "brand_name")
    )


def near_expiry_days(organization) -> int:
    """Settings → Preferences → expiry alert (default 90 days)."""
    from apps.organizations.models import preferences_for

    return preferences_for(organization).near_expiry_days


def near_expiry(branch, *, days=None):
    today = timezone.localdate()
    if days is None:
        days = near_expiry_days(branch.organization)
    return (
        InventoryBatch.objects.filter(
            branch=branch,
            quantity_available__gt=0,
            expiry_date__gte=today,
            expiry_date__lte=today + timedelta(days=days),
        )
        .select_related("product")
        .order_by("expiry_date")
    )


def expired(branch):
    return (
        InventoryBatch.objects.filter(
            branch=branch, quantity_available__gt=0, expiry_date__lt=timezone.localdate()
        )
        .select_related("product")
        .order_by("expiry_date")
    )


def stock_value(branch) -> dict:
    batches = InventoryBatch.objects.filter(branch=branch, quantity_available__gt=0)
    total = sum((b.purchase_cost * b.quantity_available for b in batches), 0)
    retail = sum((b.sale_price * b.quantity_available for b in batches), 0)
    return {"cost": total, "retail": retail}
