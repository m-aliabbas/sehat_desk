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
from apps.core.models import next_number

from . import stock
from .models import (
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
    purchase.grand_total = purchase.subtotal - purchase.discount + to_money(purchase.tax_amount)
    purchase.save(update_fields=["subtotal", "discount", "grand_total", "updated_at"])
    return purchase


def _clean_receipt_fields(product, batch_number, expiry_date, sale_price):
    return {
        "batch_number": (batch_number or "").strip().upper()[:40],
        "expiry_date": expiry_date,
        "sale_price": None if sale_price in (None, "") else to_money(sale_price),
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
) -> PurchaseItem:
    purchase = _lock(purchase, request)
    if product.organization_id != purchase.organization_id or not product.is_active:
        raise PurchaseError("That product is not available.")
    if quantity <= 0:
        raise PurchaseError("Enter a quantity greater than zero.")
    if free_quantity < 0 or to_money(unit_cost) < 0:
        raise PurchaseError("Cost and free quantity cannot be negative.")
    item = PurchaseItem.objects.create(
        purchase=purchase,
        product=product,
        quantity=quantity,
        free_quantity=free_quantity,
        unit_cost=to_money(unit_cost),
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
    notes="",
) -> Purchase:  # fmt: skip
    purchase = _lock(purchase, request)
    discount, tax_amount = to_money(discount), to_money(tax_amount)
    if discount < 0 or tax_amount < 0:
        raise PurchaseError("Discount and tax cannot be negative.")
    if discount > purchase.subtotal:
        raise PurchaseError(f"The discount cannot be more than {purchase.subtotal}.")
    purchase.supplier_invoice_number = supplier_invoice_number.strip()[:60]
    purchase.supplier_invoice_date = supplier_invoice_date
    purchase.discount = discount
    purchase.tax_amount = tax_amount
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
    if not purchase.supplier_invoice_number:
        problems.append("Enter the supplier's invoice number.")
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
def receive(purchase, *, request) -> Purchase:
    """Goods receipt: post every line to stock and the total to the supplier ledger."""
    purchase = _lock(purchase, request)
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
            )
        except stock.StockError as err:
            raise PurchaseError(f"{item.product}: {err.message}") from err
        item.save(update_fields=["stock_txn", "updated_at"])
        if item.product.purchase_price != item.effective_unit_cost:
            item.product.purchase_price = item.effective_unit_cost  # latest cost becomes default
            item.product.save(update_fields=["purchase_price", "updated_at"])
    purchase.status = Status.RECEIVED
    purchase.received_at = timezone.now()
    purchase.received_by = request.user
    purchase.save(update_fields=["status", "received_at", "received_by", "updated_at"])
    if purchase.grand_total:
        _ledger(
            request,
            purchase.vendor,
            VendorLedgerEntry.Kind.PURCHASE,
            purchase.grand_total,
            purchase=purchase,
            note=f"Invoice {purchase.supplier_invoice_number}",
        )
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
        status=Status.RECEIVED, received_at__date__gte=start, received_at__date__lte=end
    ).select_related("vendor", "received_by")
    rets = returns.filter(created_at__date__gte=start, created_at__date__lte=end).select_related(
        "vendor", "purchase"
    )
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
