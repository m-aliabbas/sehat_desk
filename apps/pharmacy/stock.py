"""The only code that changes stock. Every change locks the batch row, checks for negative
stock and writes a StockTransaction in the same database transaction.

`select_for_update` is a no-op on SQLite (which serialises writers anyway). On PostgreSQL it
takes a row lock, so two tills selling the last strip cannot both succeed.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.core.dates import in_days, start_of_day

from .models import InventoryBatch, Product, ProductPriceChange, StockTransaction

Kind = StockTransaction.Kind


class StockError(ValidationError):
    pass


def _per_unit(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.000001"))


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
        unit_price=batch.sale_price,
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
    """Purchase stock-in (or opening stock). Stock at the cost and price of a lot already on record
    joins it. A new cost or price under the same batch number opens a new lot, so what was bought
    earlier keeps its own cost. A new sale price also becomes the product's price for new stock."""
    _need_manage(request)
    if product.organization_id != request.organization.id or not product.is_active:
        raise StockError("That product is not available.")
    if quantity <= 0:
        raise StockError("Enter a quantity greater than zero.")
    product.refresh_from_db(fields=["sale_price", "purchase_price"])
    batch_number = batch_number.strip().upper()
    if not batch_number:
        raise StockError("Enter the batch number printed on the pack.")
    if expiry_date <= timezone.localdate():
        raise StockError("This stock is already expired. Do not receive it into saleable stock.")
    purchase_cost = _per_unit(purchase_cost)
    new_price = None if sale_price in (None, "") else _per_unit(sale_price)
    sale_price = product.sale_price if new_price is None else new_price
    lots = list(
        InventoryBatch.objects.select_for_update(of=("self",)).filter(
            branch=request.branch, product=product, batch_number=batch_number
        )
    )
    if lots and lots[0].expiry_date != expiry_date:
        raise StockError(
            f"Batch {batch_number} is already recorded with expiry {lots[0].expiry_date:%d %b %Y}."
        )
    batch = next(
        (b for b in lots if (b.purchase_cost, b.sale_price) == (purchase_cost, sale_price)), None
    )
    if batch is None:
        batch = InventoryBatch.objects.create(
            organization=request.organization,
            branch=request.branch,
            product=product,
            batch_number=batch_number,
            lot=max((b.lot for b in lots), default=0) + 1,
            expiry_date=expiry_date,
            purchase_cost=purchase_cost,
            sale_price=sale_price,
        )
    if new_price is not None and new_price != product.sale_price:
        from . import pricing

        pricing.change_price(
            product,
            request=request,
            sale_price=new_price,
            scope=ProductPriceChange.Scope.RECEIPT,
            reference=reference,
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


def unit_correction_blocker(product, factor: int) -> str | None:
    """Why stock cannot be converted to a new unit yet, or None. Only a conversion (factor > 1)
    is blocked: open sales and purchases count in the old unit."""
    from .models import PharmacySale, PharmacySaleItem, PurchaseItem

    if factor <= 1:
        return None
    if PharmacySaleItem.objects.filter(
        product=product, sale__status__in=(PharmacySale.Status.OPEN, PharmacySale.Status.HELD)
    ).exists():
        return "It is on a sale in progress or held at the till. Finish or clear that sale first."
    if PurchaseItem.objects.filter(
        product=product, purchase__status__in=("draft", "ordered")
    ).exists():
        return "It is on an open purchase. Receive or cancel that purchase first."
    for batch in InventoryBatch.objects.filter(product=product, quantity_available__gt=0):
        if batch.quantity_available % factor:
            return (
                f"Batch {batch.label} has {batch.quantity_available}, which is not a whole number "
                f"of the new unit ({factor} each). Count it first."
            )
    return None


@transaction.atomic
def correct_unit(
    product, *, request, unit: str, pack_unit: str = "", pack_size: int = 1, factor: int = 1,
    reason: str,
):  # fmt: skip
    """Fix a product saved in the wrong unit, e.g. tablet that is really a bottle.

    factor = how many of the old unit make one of the new unit (1 = only the name was wrong).
    With a factor, stock in every branch is divided by it and prices per unit multiplied by it,
    so stock value stays the same; each batch that changes gets an adjustment row in the ledger.
    Runs only from an approved request (apps.approvals).
    """
    _need_manage(request)
    unit = " ".join((unit or "").split()).lower()[:30]
    pack_unit = " ".join((pack_unit or "").split()).lower()[:30]
    if not unit:
        raise StockError("Enter the correct unit.")
    if not reason.strip():
        raise StockError("Give the reason for the correction.")
    if factor < 1 or pack_size < 1:
        raise StockError("Sizes must be 1 or more.")
    product = Product.objects.select_for_update(of=("self",)).get(pk=product.pk)
    batches = list(
        InventoryBatch.objects.select_for_update(of=("self",))
        .filter(product=product)
        .select_related("branch")
    )
    if why := unit_correction_blocker(product, factor):
        raise StockError(why)
    before = {
        "unit": product.unit, "pack_unit": product.pack_unit, "pack_size": product.pack_size,
        "sale_price": str(product.sale_price), "purchase_price": str(product.purchase_price),
    }  # fmt: skip
    note = f"Unit corrected: {factor} {product.unit} = 1 {unit}. {reason.strip()}"[:200]
    for batch in batches:
        if factor == 1:
            break
        new_qty = batch.quantity_available // factor
        delta = new_qty - batch.quantity_available
        cost, price = batch.purchase_cost * factor, batch.sale_price * factor
        InventoryBatch.objects.filter(pk=batch.pk).update(
            quantity_available=new_qty,
            quantity_received=batch.quantity_received // factor,
            purchase_cost=cost,
            sale_price=price,
            updated_at=timezone.now(),
        )
        if delta:
            StockTransaction.objects.create(
                organization=batch.organization, branch=batch.branch,
                kind=Kind.ADJUSTMENT, product=product, batch=batch, quantity=delta,
                balance_after=new_qty, unit_cost=cost, unit_price=price, reason=note,
                created_by=request.user,
            )  # fmt: skip
    product.unit = unit
    product.pack_size = pack_size
    product.pack_unit = pack_unit if pack_size > 1 and pack_unit else unit
    if pack_size == 1:
        product.packs_per_box = 1
    product.purchase_price *= factor
    product.sale_price *= factor
    product.reorder_level //= factor
    product.save()
    audit.record(
        request,
        "product.unit_correct",
        product,
        changes={k: [v, str(getattr(product, k))] for k, v in before.items()},
        summary=note[:100],
    )
    return product


@transaction.atomic
def correct_batch(batch, *, request, batch_number: str = "", expiry_date=None, reason: str):
    """Fix a batch number or expiry typed wrong. Moves no stock; the change is kept in the audit
    log. Runs only from an approved request (apps.approvals)."""
    _need_manage(request)
    if not reason.strip():
        raise StockError("Give the reason for the correction.")
    batch = InventoryBatch.objects.select_for_update(of=("self",)).get(pk=batch.pk)
    if batch.branch_id != request.branch.pk:
        raise StockError("That batch belongs to another branch.")
    number = " ".join((batch_number or batch.batch_number).split()).upper()[:40]
    expiry = expiry_date or batch.expiry_date
    before = {"batch_number": batch.batch_number, "expiry_date": str(batch.expiry_date)}
    same = InventoryBatch.objects.filter(
        branch=batch.branch, product=batch.product, batch_number=number
    ).exclude(pk=batch.pk)
    if same.exclude(expiry_date=expiry).exists():
        raise StockError(f"Batch {number} is already on file with another expiry date.")
    if number != batch.batch_number:
        batch.lot = max(same.values_list("lot", flat=True), default=0) + 1
        batch.batch_number = number
    batch.expiry_date = expiry
    batch.save(update_fields=["batch_number", "lot", "expiry_date", "updated_at"])
    audit.record(
        request,
        "stock.batch_correct",
        batch,
        changes={k: [v, str(getattr(batch, k))] for k, v in before.items()},
        summary=reason[:100],
    )
    return batch


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
    """Batches inside their alert window: the product's own days, else the hospital default."""
    today = timezone.localdate()
    if days is None:
        window = Q(
            product__expiry_alert_days__isnull=True,
            expiry_date__lte=today + timedelta(days=near_expiry_days(branch.organization)),
        )
        overrides = (
            Product.objects.filter(organization=branch.organization)
            .exclude(expiry_alert_days=None)
            .values_list("expiry_alert_days", flat=True)
            .distinct()
        )
        for d in overrides:
            window |= Q(product__expiry_alert_days=d, expiry_date__lte=today + timedelta(days=d))
    else:
        window = Q(expiry_date__lte=today + timedelta(days=days))
    return (
        InventoryBatch.objects.filter(
            window, branch=branch, quantity_available__gt=0, expiry_date__gte=today
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


def ledger_mismatches(branch) -> list[InventoryBatch]:
    """Batches whose balance is not the sum of their ledger entries. Should always be empty."""
    return list(
        InventoryBatch.objects.filter(branch=branch)
        .annotate(ledger_total=Sum("transactions__quantity", default=0))
        .exclude(quantity_available=F("ledger_total"))
        .select_related("product")
    )


def product_statement(branch, product, *, start=None, end=None, batch=None) -> dict:
    """A product's stock account for a period, oldest first: opening balance, every movement with
    the running balance across all its batches, closing balance. Dates are inclusive."""
    txns = StockTransaction.objects.filter(branch=branch, product=product)
    if batch is not None:
        txns = txns.filter(batch=batch)
    before = txns.filter(created_at__lt=start_of_day(start)) if start else txns.none()
    opening = before.aggregate(q=Sum("quantity", default=0))["q"]
    opening_value = sum((t.unit_cost * t.quantity for t in before.only("unit_cost", "quantity")), 0)
    period = txns.order_by("created_at", "pk").select_related(
        "batch",
        "created_by",
        "sale_item__sale",
        "sale_return_item__sale_return",
        "purchase_item__purchase",
        "purchase_return_item__purchase_return",
    )
    if start:
        period = period.filter(**in_days("created_at", start))
    if end:
        period = period.filter(**in_days("created_at", end=end))
    rows, balance = [], opening
    total_in = total_out = 0
    value = Decimal(opening_value)
    for txn in period:
        balance += txn.quantity
        value += txn.unit_cost * txn.quantity
        total_in += max(txn.quantity, 0)
        total_out += max(-txn.quantity, 0)
        rows.append({"txn": txn, "balance": balance})
    return {
        "rows": rows,
        "opening": opening,
        "opening_value": Decimal(opening_value).quantize(Decimal("0.01")),
        "total_in": total_in,
        "total_out": total_out,
        "closing": balance,
        "closing_value": value.quantize(Decimal("0.01")),
    }


def stock_value(branch) -> dict:
    batches = InventoryBatch.objects.filter(branch=branch, quantity_available__gt=0)
    total = sum((b.purchase_cost * b.quantity_available for b in batches), 0)
    retail = sum((b.sale_price * b.quantity_available for b in batches), 0)
    return {"cost": total, "retail": retail}
