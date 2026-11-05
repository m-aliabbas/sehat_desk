"""Pharmacy purchasing (Phase 14): suppliers, purchases, goods receipt and purchase returns.

draft ──order──▶ ordered ──receive──▶ received ──return (part)──▶ received
  └──────cancel (nothing received)──────▶ cancelled

Receiving posts every line to stock (stock.receive: batch, expiry, cost) and the purchase total
to the supplier ledger in one transaction, so stock and the supplier balance never disagree.
Quantities on an unreceived purchase are not stock and cannot be sold.
All writes need pharmacy.inventory.manage (stock.py checks it again).
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.billing.models import to_money
from apps.core.dates import in_days
from apps.core.models import next_number
from apps.organizations import field_rules

from . import pricing, stock
from .models import (
    InventoryBatch,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Vendor,
    VendorLedgerEntry,
)

Status = Purchase.Status
ZERO = Decimal("0.00")
MANAGE = "pharmacy.inventory.manage"


class PurchaseError(ValidationError):
    pass


def _need_manage(request):
    if MANAGE not in request.sd_perms:
        raise PurchaseError("You are not allowed to manage purchases.")


def _ledger(request, vendor, kind, amount, *, purchase=None, purchase_return=None, note=""):
    return VendorLedgerEntry.objects.create(
        organization=vendor.organization,
        branch=request.branch,
        vendor=vendor,
        kind=kind,
        amount=amount,
        purchase=purchase,
        purchase_return=purchase_return,
        note=note.strip()[:200],
        created_by=request.user,
    )


# --- Vendors -----------------------------------------------------------------------------------


@transaction.atomic
def post_opening_balance(vendor, *, request, amount) -> VendorLedgerEntry | None:
    """What the hospital already owed this supplier before SehatDesk (once per vendor)."""
    _need_manage(request)
    amount = to_money(amount)
    if not amount:
        return None
    if vendor.ledger.filter(kind=VendorLedgerEntry.Kind.OPENING).exists():
        raise PurchaseError(f"{vendor} already has an opening balance.")
    return _ledger(request, vendor, VendorLedgerEntry.Kind.OPENING, amount, note="Opening balance")


# --- Purchases ---------------------------------------------------------------------------------


@transaction.atomic
def create_purchase(*, request, vendor, supplier_invoice_number="", notes="") -> Purchase:
    _need_manage(request)
    if vendor.organization_id != request.organization.id or not vendor.is_active:
        raise PurchaseError("Choose an active supplier.")
    org = request.organization
    return Purchase.objects.create(
        organization=org,
        branch=request.branch,
        number=f"PO-{next_number(org, 'purchase'):05d}",
        vendor=vendor,
        supplier_invoice_number=supplier_invoice_number.strip()[:60],
        notes=notes.strip()[:300],
        created_by=request.user,
    )


def _lock(purchase, request) -> Purchase:
    _need_manage(request)
    purchase = Purchase.objects.select_for_update(of=("self",)).get(pk=purchase.pk)
    if purchase.branch_id != request.branch.pk:
        raise PurchaseError("That purchase belongs to another branch.")
    if not purchase.is_editable:
        raise PurchaseError(
            f"{purchase.number} is {purchase.get_status_display().lower()}; it cannot change."
        )
    return purchase


def recalculate(purchase) -> Purchase:
    purchase.subtotal = sum((i.line_cost for i in purchase.items.all()), ZERO)
    purchase.discount = min(to_money(purchase.discount), purchase.subtotal)
    purchase.line_tax = sum((i.tax_amount for i in purchase.items.all()), ZERO)
    purchase.grand_total = (
        purchase.subtotal
        - purchase.discount
        + purchase.line_tax
        + to_money(purchase.tax_amount)
        + to_money(purchase.further_tax)
        + to_money(purchase.advance_tax)
    )
    purchase.save(update_fields=["subtotal", "discount", "line_tax", "grand_total", "updated_at"])
    return purchase


def _clean_receipt_fields(product, batch_number, expiry_date, sale_price):
    return {
        "batch_number": (batch_number or "").strip().upper()[:40],
        "expiry_date": expiry_date,
        "sale_price": None
        if sale_price in (None, "")
        else Decimal(sale_price).quantize(Decimal("0.000001")),
    }


@transaction.atomic
def add_item(
    purchase,
    *,
    request,
    product,
    quantity: int,
    unit_cost,
    free_quantity: int = 0,
    batch_number="",
    expiry_date=None,
    sale_price=None,
    tax_percent=0,
    discount_percent=0,
) -> PurchaseItem:
    purchase = _lock(purchase, request)
    if product.organization_id != purchase.organization_id or not product.is_active:
        raise PurchaseError("That product is not available.")
    if quantity <= 0:
        raise PurchaseError("Enter a quantity greater than zero.")
    if free_quantity < 0 or to_money(unit_cost) < 0:
        raise PurchaseError("Cost and free quantity cannot be negative.")
    if not 0 <= Decimal(tax_percent) <= 100:
        raise PurchaseError("GST must be between 0 and 100 percent.")
    if not 0 <= Decimal(discount_percent) <= 100:
        raise PurchaseError("Discount must be between 0 and 100 percent.")
    item = PurchaseItem.objects.create(
        purchase=purchase,
        product=product,
        quantity=quantity,
        free_quantity=free_quantity,
        unit_cost=Decimal(unit_cost).quantize(Decimal("0.0001")),
        tax_percent=tax_percent,
        discount_percent=discount_percent,
        **_clean_receipt_fields(product, batch_number, expiry_date, sale_price),
    )
    recalculate(purchase)
    return item


@transaction.atomic
def update_receipt(item, *, request, batch_number, expiry_date, sale_price=None,
                   quantity=None, free_quantity=None) -> PurchaseItem:  # fmt: skip
    """Goods receipt details for one line: batch, expiry and (if they differ from the order)
    the quantities actually delivered."""
    purchase = _lock(item.purchase, request)
    item = PurchaseItem.objects.select_related("product").get(pk=item.pk, purchase=purchase)
    for k, v in _clean_receipt_fields(item.product, batch_number, expiry_date, sale_price).items():
        setattr(item, k, v)
    if quantity is not None:
        if quantity <= 0:
            raise PurchaseError("Enter a quantity greater than zero, or remove the line.")
        item.quantity = quantity
    if free_quantity is not None:
        item.free_quantity = max(free_quantity, 0)
    item.save()
    recalculate(purchase)
    return item


@transaction.atomic
def remove_item(item, *, request) -> Purchase:
    purchase = _lock(item.purchase, request)
    item.delete()  # an unreceived line: no stock or money was posted
    return recalculate(purchase)


@transaction.atomic
def set_invoice_details(
    purchase, *, request, supplier_invoice_number, supplier_invoice_date, discount, tax_amount,
    notes="", further_tax=0, advance_tax=0,
) -> Purchase:  # fmt: skip
    purchase = _lock(purchase, request)
    discount, tax_amount = to_money(discount), to_money(tax_amount)
    further_tax, advance_tax = to_money(further_tax), to_money(advance_tax)
    if min(discount, tax_amount, further_tax, advance_tax) < 0:
        raise PurchaseError("Discount and tax cannot be negative.")
    if discount > purchase.subtotal:
        raise PurchaseError(f"The discount cannot be more than {purchase.subtotal}.")
    purchase.supplier_invoice_number = supplier_invoice_number.strip()[:60]
    purchase.supplier_invoice_date = supplier_invoice_date
    purchase.discount = discount
    purchase.tax_amount = tax_amount
    purchase.further_tax, purchase.advance_tax = further_tax, advance_tax
    purchase.notes = notes.strip()[:300]
    purchase.save()
    return recalculate(purchase)


@transaction.atomic
def mark_ordered(purchase, *, request) -> Purchase:
    purchase = _lock(purchase, request)
    if purchase.status != Status.DRAFT:
        raise PurchaseError(f"{purchase.number} is already ordered.")
    if not purchase.items.exists():
        raise PurchaseError("Add at least one item before ordering.")
    purchase.status = Status.ORDERED
    purchase.ordered_at = timezone.now()
    purchase.save(update_fields=["status", "ordered_at", "updated_at"])
    return purchase


def receipt_problems(purchase) -> list[str]:
    """What is missing before the goods can be posted to stock."""
    problems = []
    items = list(purchase.items.select_related("product"))
    if not items:
        problems.append("Add at least one item.")
    org = purchase.organization
    if field_rules.is_required(org, "purchase.supplier_invoice"):
        if not purchase.supplier_invoice_number:
            problems.append("Enter the supplier's invoice number.")
    if field_rules.is_required(org, "purchase.further_tax") and not purchase.further_tax:
        problems.append("Enter the further tax on this bill.")
    if field_rules.is_required(org, "purchase.advance_tax") and not purchase.advance_tax:
        problems.append("Enter the advance tax on this bill.")
    if field_rules.is_required(org, "purchase.gst"):
        for item in items:
            if not item.tax_percent:
                problems.append(f"{item.product}: enter the GST.")
    today = timezone.localdate()
    for item in items:
        if not item.batch_number:
            problems.append(f"{item.product}: enter the batch number printed on the pack.")
        if not item.expiry_date:
            problems.append(f"{item.product}: enter the expiry date.")
        elif item.expiry_date <= today:
            problems.append(f"{item.product}: batch {item.batch_number} is already expired.")
    return problems


@transaction.atomic
def receive(purchase, *, request, received_on=None) -> Purchase:
    """Goods receipt: post every line to stock and the total to the supplier ledger.
    received_on (optional): the day the goods really came, for a bill entered late. Stock, the
    purchase and the supplier ledger are dated that day; the audit log keeps the real time."""
    purchase = _lock(purchase, request)
    try:
        received_at = stock.received_at_for(received_on)
    except stock.StockError as err:
        raise PurchaseError(err.message) from err
    problems = receipt_problems(purchase)
    if problems:
        raise PurchaseError(problems[0])
    recalculate(purchase)
    reference = f"{purchase.number} · {purchase.supplier_invoice_number}"[:80]
    for item in purchase.items.select_related("product"):
        try:
            item.stock_txn = stock.receive(
                request=request,
                product=item.product,
                batch_number=item.batch_number,
                expiry_date=item.expiry_date,
                quantity=item.received_quantity,
                purchase_cost=item.effective_unit_cost,
                sale_price=item.sale_price,
                reference=reference,
                received_on=received_on,
            )
        except stock.StockError as err:
            raise PurchaseError(f"{item.product}: {err.message}") from err
        item.save(update_fields=["stock_txn", "updated_at"])
        product = item.product
        product.refresh_from_db(fields=["purchase_price", "sale_price"])
        if product.purchase_price != item.effective_unit_cost:
            old_cost = product.purchase_price
            product.purchase_price = item.effective_unit_cost  # latest cost becomes default
            product.save(update_fields=["purchase_price", "updated_at"])
            pricing.record_edit(
                product,
                request=request,
                old_cost=old_cost,
                old_price=product.sale_price,
                reference=reference,
            )
    purchase.status = Status.RECEIVED
    purchase.received_at = received_at or timezone.now()
    purchase.received_by = request.user
    purchase.save(update_fields=["status", "received_at", "received_by", "updated_at"])
    if purchase.grand_total:
        entry = _ledger(
            request,
            purchase.vendor,
            VendorLedgerEntry.Kind.PURCHASE,
            purchase.grand_total,
            purchase=purchase,
            note=f"Invoice {purchase.supplier_invoice_number}",
        )
        if received_at is not None:
            VendorLedgerEntry.objects.filter(pk=entry.pk).update(created_at=received_at)
    return purchase


@transaction.atomic
def cancel(purchase, *, request, reason) -> Purchase:
    purchase = _lock(purchase, request)
    if not reason.strip():
        raise PurchaseError("Give the reason for cancelling.")
    purchase.status = Status.CANCELLED
    purchase.cancelled_at = timezone.now()
    purchase.cancel_reason = reason.strip()[:200]
    purchase.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
    return purchase


# --- Purchase returns --------------------------------------------------------------------------


def _bought_line(batch, vendor, qty):
    """The purchase line this stock came from, if it can still take the whole return."""
    lines = PurchaseItem.objects.filter(stock_txn__batch=batch, purchase__vendor=vendor)
    return next((i for i in lines if i.received_quantity - i.returned_quantity >= qty), None)


@transaction.atomic
def return_to_supplier(vendor, *, request, quantities: dict[int, int], reason, reference=""):
    """Send stock back by supplier and medicine, with no purchase to open first. Credits the
    supplier at each batch's cost. quantities: {batch_id: units}. Returns the PurchaseReturn."""
    _need_manage(request)
    if not reason.strip():
        raise PurchaseError("Give the reason for returning stock to the supplier.")
    wanted = {k: v for k, v in quantities.items() if v}
    if not wanted:
        raise PurchaseError("Add at least one medicine and its quantity.")
    batches = {
        b.pk: b
        for b in InventoryBatch.objects.filter(branch=request.branch, pk__in=wanted).select_related(
            "product"
        )
    }
    org = vendor.organization
    ret = PurchaseReturn.objects.create(
        organization=org,
        branch=request.branch,
        number=f"PRN-{next_number(org, 'purchase_return'):05d}",
        vendor=vendor,
        reason=reason.strip()[:200],
        reference=reference.strip()[:80],
        amount=ZERO,
        created_by=request.user,
    )
    total = ZERO
    for batch_id, qty in wanted.items():
        batch = batches.get(batch_id)
        if batch is None:
            raise PurchaseError("That batch is not in this branch.")
        if qty < 0:
            raise PurchaseError("Enter a quantity greater than zero.")
        try:
            txn = stock.purchase_return(
                batch,
                request=request,
                quantity=qty,
                reason=reason,
                reference=ret.reference or ret.number,
            )
        except stock.StockError as err:
            raise PurchaseError(f"{batch.product}: {err.message}") from err
        amount = to_money(batch.purchase_cost * qty)
        PurchaseReturnItem.objects.create(
            purchase_return=ret,
            purchase_item=_bought_line(batch, vendor, qty),
            quantity=qty,
            unit_cost=to_money(batch.purchase_cost),
            amount=amount,
            stock_txn=txn,
        )
        total += amount
    ret.amount = total
    ret.save(update_fields=["amount", "updated_at"])
    if total:
        _ledger(
            request,
            vendor,
            VendorLedgerEntry.Kind.PURCHASE_RETURN,
            -total,
            purchase_return=ret,
            note=reason,
        )
    return ret


@transaction.atomic
def return_items(purchase, *, request, quantities: dict[int, int], reason, reference=""):
    """Send received stock back to the supplier and credit the supplier ledger.
    quantities: {purchase_item_id: units}. Returns the PurchaseReturn."""
    _need_manage(request)
    purchase = (
        Purchase.objects.select_for_update(of=("self",))
        .select_related("vendor")
        .get(pk=purchase.pk)
    )
    if purchase.branch_id != request.branch.pk or purchase.status != Status.RECEIVED:
        raise PurchaseError("Only received purchases of this branch can be returned.")
    if not reason.strip():
        raise PurchaseError("Give the reason for returning stock to the supplier.")
    wanted = {k: v for k, v in quantities.items() if v}
    if not wanted:
        raise PurchaseError("Enter the quantity returned for at least one item.")
    items = {i.pk: i for i in purchase.items.select_related("product", "stock_txn__batch")}
    org = purchase.organization
    ret = PurchaseReturn.objects.create(
        organization=org,
        branch=purchase.branch,
        number=f"PRN-{next_number(org, 'purchase_return'):05d}",
        purchase=purchase,
        vendor=purchase.vendor,
        reason=reason.strip()[:200],
        reference=reference.strip()[:80],
        amount=ZERO,
        created_by=request.user,
    )
    total = ZERO
    for item_id, qty in wanted.items():
        item = items.get(item_id)
        if item is None:
            raise PurchaseError("That item is not on this purchase.")
        left = item.received_quantity - item.returned_quantity
        if qty < 0 or qty > left:
            raise PurchaseError(f"Only {left} unit(s) of {item.product} can still be returned.")
        try:
            txn = stock.purchase_return(
                item.stock_txn.batch,
                request=request,
                quantity=qty,
                reason=reason,
                reference=ret.reference or ret.number,
            )
        except stock.StockError as err:
            raise PurchaseError(f"{item.product}: {err.message}") from err
        amount = to_money(item.effective_unit_cost * qty)
        PurchaseReturnItem.objects.create(
            purchase_return=ret,
            purchase_item=item,
            quantity=qty,
            unit_cost=item.effective_unit_cost,
            amount=amount,
            stock_txn=txn,
        )
        total += amount
    ret.amount = total
    ret.save(update_fields=["amount", "updated_at"])
    if total:
        _ledger(
            request,
            purchase.vendor,
            VendorLedgerEntry.Kind.PURCHASE_RETURN,
            -total,
            purchase=purchase,
            purchase_return=ret,
            note=reason,
        )
    return ret


# --- Report ------------------------------------------------------------------------------------


def report(purchases, returns, start: date, end: date) -> dict:
    """Received purchases and returns in a date range (querysets already scoped by the view)."""
    received = purchases.filter(
        status=Status.RECEIVED, **in_days("received_at", start, end)
    ).select_related("vendor", "received_by")
    rets = returns.filter(**in_days("created_at", start, end)).select_related("vendor", "purchase")
    by_vendor = {}
    for row in received.values("vendor_id", "vendor__name").annotate(
        n=Count("id"), total=Sum("grand_total")
    ):
        by_vendor[row["vendor_id"]] = {
            "name": row["vendor__name"], "count": row["n"], "purchased": row["total"],
            "returned": ZERO,
        }  # fmt: skip
    for row in rets.values("vendor_id", "vendor__name").annotate(total=Sum("amount")):
        by_vendor.setdefault(
            row["vendor_id"],
            {"name": row["vendor__name"], "count": 0, "purchased": ZERO, "returned": ZERO},
        )["returned"] = row["total"]
    for v in by_vendor.values():
        v["net"] = v["purchased"] - v["returned"]
    purchased = received.aggregate(s=Sum("grand_total"))["s"] or ZERO
    returned = rets.aggregate(s=Sum("amount"))["s"] or ZERO
    return {
        "purchases": received.order_by("-received_at"),
        "returns": rets,
        "by_vendor": sorted(by_vendor.values(), key=lambda v: -v["net"]),
        "purchased": purchased,
        "returned": returned,
        "net": purchased - returned,
    }


def vendors_with_balance(organization):
    return Vendor.objects.filter(organization=organization).annotate(
        balance_total=Sum("ledger__amount", default=ZERO)
    )


# --- Many lines at once and automatic orders -----------------------------------------------------


def line_quantity(product, *, boxes=0, packs=0, units=0) -> int:
    return boxes * product.box_size + packs * product.pack_size + units


def units_in(product, unit) -> int:
    return {"box": product.box_size, "pack": product.pack_size}.get(unit, 1)


def cost_per_unit(product, cost, per) -> Decimal:
    """Suppliers quote per box or strip. Convert to the per-unit cost stored on the line."""
    return (Decimal(cost) / units_in(product, per)).quantize(Decimal("0.0001"))


@transaction.atomic
def add_items(purchase, *, request, lines) -> list[PurchaseItem]:
    """Add every line or none. Each line holds add_item's keyword arguments."""
    return [add_item(purchase, request=request, **line) for line in lines]


def _last_vendor(product, branch):
    item = (
        PurchaseItem.objects.filter(
            product=product, purchase__branch=branch, purchase__status=Status.RECEIVED
        )
        .select_related("purchase__vendor")
        .order_by("-purchase__received_at")
        .first()
    )
    return item.purchase.vendor if item else None


def reorder_suggestions(request) -> list[dict]:
    """Low-stock products not already on an open order, with who to buy from and how much:
    enough to reach twice the reorder level, in whole packs."""
    on_order = set(
        PurchaseItem.objects.filter(
            purchase__branch=request.branch, purchase__status__in=(Status.DRAFT, Status.ORDERED)
        ).values_list("product_id", flat=True)
    )
    rows = []
    for product in stock.low_stock(request.branch, request.organization).select_related(
        "preferred_vendor"
    ):
        if product.pk in on_order:
            continue
        vendor = product.preferred_vendor or _last_vendor(product, request.branch)
        wanted = max(product.reorder_level * 2 - product.stock, product.pack_size)
        rows.append(
            {
                "product": product,
                "vendor": vendor,
                "packs": -(-wanted // product.pack_size),
                "stock": product.stock,
            }
        )
    return rows


@transaction.atomic
def create_reorder_purchases(*, request, lines) -> list[Purchase]:
    """One draft order per supplier. lines: (product, vendor, packs). Nothing is posted to stock
    or the supplier ledger until the goods are received."""
    by_vendor = {}
    for product, vendor, packs in lines:
        if packs <= 0:
            raise PurchaseError(f"Enter a quantity for {product}.")
        by_vendor.setdefault(vendor.pk, (vendor, []))[1].append((product, packs))
    purchases = []
    for vendor, products in by_vendor.values():
        purchase = create_purchase(request=request, vendor=vendor, notes="Automatic reorder")
        for product, packs in products:
            add_item(
                purchase,
                request=request,
                product=product,
                quantity=packs * product.pack_size,
                unit_cost=product.purchase_price,
            )
        purchases.append(purchase)
    return purchases
