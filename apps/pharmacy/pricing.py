"""Changing what a product costs or sells for.

A price change is not a new batch. A batch is what is printed on the pack, and inventing one would
break recalls and expiry tracking. So:
- New stock takes the product's price. Stock on the shelf keeps its own until someone reprices it.
- Repricing the shelf changes the batches' sale price, never their cost: what was paid is a fact.
- Stock arriving under an old batch number at a new price opens a new lot (stock.receive).
- Every change is a ProductPriceChange row. No quantity moves, so the stock ledger is untouched.
"""

from decimal import Decimal

from django.db import transaction

from .models import InventoryBatch, ProductPriceChange
from .stock import StockError, _need_manage, _per_unit

Scope = ProductPriceChange.Scope


def unit_value(amount, units) -> Decimal:
    """The price of one base unit, from the price of a pack holding `units` of them."""
    return _per_unit(Decimal(amount) / max(int(units), 1))


def _log(product, *, request, old_cost, old_price, scope, batch=None, repriced=0, reason="",
         reference="") -> ProductPriceChange:  # fmt: skip
    return ProductPriceChange.objects.create(
        organization=product.organization,
        product=product,
        branch=request.branch if repriced or batch else None,
        batch=batch,
        scope=scope,
        old_cost=old_cost,
        new_cost=product.purchase_price,
        old_price=old_price,
        new_price=product.sale_price,
        units_repriced=repriced,
        reason=reason.strip()[:200],
        reference=reference.strip()[:80],
        created_by=request.user,
    )


def record_edit(product, *, request, old_cost, old_price, apply_to_shelf=False, reason="",
                reference=""):  # fmt: skip
    """After the product form saved new prices: log them, and reprice the shelf if asked."""
    if (old_cost, old_price) == (product.purchase_price, product.sale_price):
        return None
    repriced = 0
    if apply_to_shelf and old_price != product.sale_price:
        repriced = _reprice_shelf(product, request)
    return _log(
        product,
        request=request,
        old_cost=old_cost,
        old_price=old_price,
        scope=Scope.SHELF if repriced else Scope.NEW_STOCK,
        repriced=repriced,
        reason=reason,
        reference=reference,
    )


def _reprice_shelf(product, request) -> int:
    batches = InventoryBatch.objects.select_for_update(of=("self",)).filter(
        branch=request.branch, product=product, quantity_available__gt=0
    )
    units = 0
    for batch in batches:
        if batch.sale_price != product.sale_price:
            units += batch.quantity_available
            batch.sale_price = product.sale_price
            batch.save(update_fields=["sale_price", "updated_at"])
    return units


@transaction.atomic
def change_price(product, *, request, sale_price, scope=Scope.NEW_STOCK, reason="", reference=""):
    """Set the price per base unit for new stock and, with scope SHELF, this branch's shelf."""
    _need_manage(request)
    product = type(product).objects.select_for_update(of=("self",)).get(pk=product.pk)
    if product.organization_id != request.organization.id:
        raise StockError("That product is not available.")
    new_price = _per_unit(sale_price)
    if new_price < 0:
        raise StockError("The price cannot be negative.")
    old_price = product.sale_price
    if new_price == old_price and scope != Scope.SHELF:
        raise StockError("That is already the price.")
    product.sale_price = new_price
    product.save(update_fields=["sale_price", "updated_at"])
    repriced = _reprice_shelf(product, request) if scope == Scope.SHELF else 0
    return _log(
        product,
        request=request,
        old_cost=product.purchase_price,
        old_price=old_price,
        scope=scope,
        repriced=repriced,
        reason=reason,
        reference=reference,
    )


@transaction.atomic
def reprice_batch(batch, *, request, sale_price, reason) -> ProductPriceChange:
    """One batch only, like a near-expiry clearance. The product's own price stays."""
    _need_manage(request)
    if not reason.strip():
        raise StockError("Give the reason for changing this batch's price.")
    batch = InventoryBatch.objects.select_for_update(of=("self",)).get(pk=batch.pk)
    if batch.branch_id != request.branch.pk:
        raise StockError("That batch belongs to another branch.")
    new_price = _per_unit(sale_price)
    if new_price < 0:
        raise StockError("The price cannot be negative.")
    old_price = batch.sale_price
    if new_price == old_price:
        raise StockError("That is already this batch's price.")
    batch.sale_price = new_price
    batch.save(update_fields=["sale_price", "updated_at"])
    return ProductPriceChange.objects.create(
        organization=batch.organization,
        product=batch.product,
        branch=batch.branch,
        batch=batch,
        scope=Scope.BATCH,
        old_cost=batch.purchase_cost,
        new_cost=batch.purchase_cost,
        old_price=old_price,
        new_price=new_price,
        units_repriced=batch.quantity_available,
        reason=reason.strip()[:200],
        created_by=request.user,
    )
