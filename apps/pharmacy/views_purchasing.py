"""Suppliers, purchases, goods receipt, purchase returns and the purchase report (design screens
58–64). Every page needs pharmacy.inventory.manage; the report also opens for
reports.financial.view. Writes go through pharmacy/purchasing.py."""

import calendar
import csv
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, paginate, scoped

from . import catalog, pos, purchasing
from .forms import PurchaseCreateForm, PurchaseDetailsForm, PurchaseItemForm, VendorForm
from .models import (
    InventoryBatch,
    Product,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    Vendor,
    VendorLedgerEntry,
)

MANAGE = purchasing.MANAGE
Status = Purchase.Status


def _crumbs(*extra):
    return [{"label": "Inventory", "url": reverse("pharmacy:dashboard")}, *extra]


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


# --- Suppliers ---------------------------------------------------------------------------------


@require_GET
@permission_required(MANAGE)
def vendor_list(request):
    """Supplier directory (design screen 58) with what is owed to each."""
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "active")
    vendors = purchasing.vendors_with_balance(request.organization).annotate(
        purchase_count=Count("purchases", distinct=True)
    )
    if show == "active":
        vendors = vendors.filter(is_active=True)
    if q:
        vendors = vendors.filter(
            Q(name__icontains=q) | Q(contact_person__icontains=q) | Q(phone__icontains=q)
        )
    vendors = list(vendors.order_by("name"))
    return render(
        request,
        "pharmacy/purchasing/vendor_list.html",
        {
            "vendors": vendors,
            "q": q,
            "show": show,
            "owed_total": sum((v.balance_total for v in vendors), 0),
            "breadcrumbs": _crumbs({"label": "Suppliers"}),
        },
    )


@permission_required(MANAGE)
def vendor_form(request, pk=None):
    vendor = (
        get_scoped_or_404(Vendor, request, pk=pk)
        if pk
        else Vendor(organization=request.organization)
    )
    creating = pk is None
    form = VendorForm(request.POST or None, instance=vendor, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        vendor = form.save()
        if creating and form.cleaned_data.get("opening_balance"):
            purchasing.post_opening_balance(
                vendor, request=request, amount=form.cleaned_data["opening_balance"]
            )
        audit.record(request, "vendor.create" if creating else "vendor.update", vendor)
        messages.success(request, f"Supplier {vendor} saved.")
        return redirect("pharmacy:vendor-detail", pk=vendor.pk)
    return render(
        request,
        "pharmacy/purchasing/vendor_form.html",
        {
            "form": form,
            "vendor": vendor,
            "creating": creating,
            "breadcrumbs": _crumbs(
                {"label": "Suppliers", "url": reverse("pharmacy:vendor-list")},
                {"label": "New supplier" if creating else vendor.name},
            ),
        },
    )


@require_GET
@permission_required(MANAGE)
def vendor_detail(request, pk):
    vendor = get_scoped_or_404(Vendor, request, pk=pk)
    entries = list(
        VendorLedgerEntry.objects.filter(vendor=vendor).select_related(
            "purchase", "purchase_return", "created_by", "branch"
        )
    )
    running = 0
    for e in entries:
        running += e.amount
        e.balance_after = running
    return render(
        request,
        "pharmacy/purchasing/vendor_detail.html",
        {
            "vendor": vendor,
            "entries": list(reversed(entries)),
            "balance": running,
            "purchases": scoped(Purchase, request).filter(vendor=vendor)[:20],
            "stats": _vendor_stats(entries),
            "breadcrumbs": _crumbs(
                {"label": "Suppliers", "url": reverse("pharmacy:vendor-list")},
                {"label": vendor.name},
            ),
        },
    )


@require_POST
@permission_required(MANAGE)
def vendor_toggle(request, pk):
    vendor = get_scoped_or_404(Vendor, request, pk=pk)
    vendor.is_active = not vendor.is_active
    vendor.save(update_fields=["is_active", "updated_at"])
    audit.record(request, "vendor.enable" if vendor.is_active else "vendor.disable", vendor)
    messages.success(
        request, f"{vendor} {'is active again' if vendor.is_active else 'deactivated'}."
    )
    return redirect("pharmacy:vendor-detail", pk=pk)


# --- Purchases ---------------------------------------------------------------------------------


@require_GET
@permission_required(MANAGE)
def purchase_list(request):
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    purchases = scoped(Purchase, request).select_related("vendor", "created_by")
    if status in Status.values:
        purchases = purchases.filter(status=status)
    if q:
        purchases = purchases.filter(
            Q(number__icontains=q)
            | Q(supplier_invoice_number__icontains=q)
            | Q(vendor__name__icontains=q)
        )
    open_ = scoped(Purchase, request).filter(status__in=Purchase.EDITABLE)
    return render(
        request,
        "pharmacy/purchasing/purchase_list.html",
        {
            "purchases": (
                page := paginate(
                    request, purchases.annotate(lines=Count("items")).order_by("-created_at")
                )
            ),
            "page_obj": page,
            "status": status,
            "statuses": Status.choices,
            "q": q,
            "open_count": open_.count(),
            "reorder_count": len(purchasing.reorder_suggestions(request)),
            "breadcrumbs": _crumbs({"label": "Purchases"}),
        },
    )


def _product_row(product) -> dict:
    """A product as the entry grid and its search box need it."""
    return {
        "id": product.pk,
        "name": product.display_name,
        "hint": product.sku,
        "packSize": product.pack_size,
        "boxSize": product.box_size,
        "packLabel": product.pack_unit.capitalize(),
        "boxLabel": product.box_unit.capitalize(),
        "unitLabel": product.unit.capitalize(),
        "unitCost": float(product.purchase_price),
    }


GRID_FIELDS = (
    "product", "qty", "unit", "free", "cost", "discount", "tax", "batch", "expiry", "sale_price",
)  # fmt: skip


def grid_rows(request) -> list[dict]:
    """The grid's columns zipped into rows, as posted."""
    columns = {f: request.POST.getlist(f) for f in GRID_FIELDS}
    return [
        {f: (columns[f][i].strip() if i < len(columns[f]) else "") for f in GRID_FIELDS}
        for i in range(len(columns["product"]))
    ]


def echo_rows(request) -> list[dict]:
    """Rows to put back in the grid when a save is refused, so no typing is lost."""
    rows = grid_rows(request)
    ids = [r["product"] for r in rows if r["product"].isdigit()]
    products = {p.pk: p for p in scoped(Product, request).filter(pk__in=ids)}
    echoed = []
    for row in rows:
        product = products.get(int(row["product"])) if row["product"].isdigit() else None
        if product:
            echoed.append(
                {
                    **_product_row(product),
                    "qty": row["qty"],
                    "unit": row["unit"],
                    "free": row["free"],
                    "cost": row["cost"],
                    "discount": row["discount"],
                    "tax": row["tax"],
                    "batch": row["batch"],
                    "expiry": row["expiry"],
                    "sale": row["sale_price"],
                }
            )
    return echoed


class GridError(ValueError):
    """A problem with one row, in words the person at the counter can act on."""


def parse_expiry(text):
    """Packs print "12/27". That means the end of the month. A full date also works."""
    if not text:
        return None
    if match := re.fullmatch(r"(\d{1,2})[/-](\d{2}|\d{4})", text.strip()):
        month, year = int(match[1]), int(match[2])
        year += 2000 if year < 100 else 0
        return date(year, month, calendar.monthrange(year, month)[1])
    return date.fromisoformat(text)


def _row_expiry(product, text):
    try:
        return parse_expiry(text)
    except ValueError as err:
        raise GridError(
            f"{product}: expiry “{text}” should be month and year, like 12/27."
        ) from err


def grid_lines(request) -> list[dict]:
    """Posted rows as stored values: everything per base unit. Raises ValueError on bad input."""
    rows = grid_rows(request)
    ids = [r["product"] for r in rows if r["product"].isdigit()]
    products = {p.pk: p for p in scoped(Product, request).filter(pk__in=ids)}
    lines = []
    for row in rows:
        product = products.get(int(row["product"])) if row["product"].isdigit() else None
        if product is None:
            raise ValueError("A row has no product.")
        size = purchasing.units_in(product, row["unit"])
        sale = row["sale_price"]
        lines.append(
            {
                "product": product,
                "quantity": int(row["qty"] or 0) * size,
                "free_quantity": int(row["free"] or 0) * size,
                "unit_cost": purchasing.cost_per_unit(product, row["cost"] or 0, row["unit"]),
                "discount_percent": Decimal(row["discount"] or 0),
                "tax_percent": Decimal(row["tax"] or 0),
                "batch_number": row["batch"],
                "expiry_date": _row_expiry(product, row["expiry"]),
                "sale_price": (Decimal(sale) / size).quantize(Decimal("0.000001"))
                if sale
                else None,
            }
        )
    return lines


BAD_GRID = "Check the numbers and dates in the rows."


@permission_required(MANAGE)
def purchase_create(request):
    """Supplier, bill and items on one screen; save as a draft or post straight to stock."""
    initial = {}
    if request.GET.get("vendor", "").isdigit():
        initial["vendor"] = request.GET["vendor"]
    form = PurchaseCreateForm(request.POST or None, request=request, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            with transaction.atomic():
                purchase = purchasing.create_purchase(
                    request=request,
                    vendor=c["vendor"],
                    supplier_invoice_number=c["supplier_invoice_number"],
                    notes=c["notes"],
                )
                purchasing.add_items(purchase, request=request, lines=grid_lines(request))
                purchasing.set_invoice_details(
                    purchase,
                    request=request,
                    supplier_invoice_number=c["supplier_invoice_number"],
                    supplier_invoice_date=c["supplier_invoice_date"],
                    discount=c["discount"] or 0,
                    tax_amount=c["tax_amount"] or 0,
                    further_tax=c["further_tax"] or 0,
                    advance_tax=c["advance_tax"] or 0,
                    notes=c["notes"],
                )
                if request.POST.get("post"):
                    purchasing.receive(purchase, request=request)
        except purchasing.PurchaseError as err:
            form.add_error(None, err.message)
        except GridError as err:
            form.add_error(None, str(err))
        except (ValueError, ArithmeticError, IndexError):
            form.add_error(None, BAD_GRID)
        else:
            audit.record(request, "purchase.create", purchase, summary=f"{purchase.vendor}")
            if request.POST.get("post"):
                audit.record(request, "purchase.receive", purchase, summary=purchase.number)
                messages.success(request, f"{purchase.number} posted to stock.")
            return redirect("pharmacy:purchase-detail", pk=purchase.pk)
    return render(
        request,
        "pharmacy/purchasing/purchase_create.html",
        {
            "form": form,
            "initial_rows": echo_rows(request) if request.method == "POST" else [],
            "breadcrumbs": _crumbs(
                {"label": "Purchases", "url": reverse("pharmacy:purchase-list")},
                {"label": "New purchase"},
            ),
        },
    )


@require_POST
@permission_required(MANAGE)
def vendor_quick(request):
    """Add a supplier from the purchase screen without leaving it."""
    form = VendorForm(
        {
            "name": request.POST.get("name", ""),
            "phone": request.POST.get("phone", ""),
            "tax_number": request.POST.get("tax_number", ""),
            "payment_terms_days": 0,
        },
        instance=Vendor(organization=request.organization),
        organization=request.organization,
    )
    if not form.is_valid():
        message = " ".join(e for errors in form.errors.values() for e in errors)
        return JsonResponse({"error": message}, status=400)
    vendor = form.save()
    audit.record(request, "vendor.create", vendor, summary=vendor.name)
    return JsonResponse({"id": vendor.pk, "name": vendor.name})


@require_POST
@permission_required(MANAGE)
def product_quick(request):
    """Add a product from the entry grid; the new row carries on from there."""
    row = {f: "" for f in catalog.BULK_FIELDS} | {
        "brand_name": request.POST.get("brand_name", "").strip(),
        **{
            f: request.POST.get(f, "")
            for f in (
                "unit", "pack_unit", "pack_size", "box_unit", "packs_per_box", "pack_cost",
                "pack_price", "price_per", "packing",
            )
        },
    }  # fmt: skip
    row["unit"] = row["unit"] or "tablet"
    if not row["brand_name"]:
        return JsonResponse({"error": "Enter the product name."}, status=400)
    # Mid-purchase the shelf details can wait; the admin's rules apply when it is next edited.
    products, errors = catalog.add_many([row], request=request, apply_rules=False)
    if not products:
        return JsonResponse({"error": errors.get(0, "Check the product details.")}, status=400)
    audit.record(request, "product.create", products[0])
    return JsonResponse({"result": _product_row(products[0])})


def _vendor_stats(entries) -> dict:
    kinds = VendorLedgerEntry.Kind
    total = {k: sum((e.amount for e in entries if e.kind == k), Decimal("0")) for k in kinds.values}
    return {
        "purchased": total[kinds.PURCHASE],
        "paid": -total[kinds.PAYMENT],
        "returned": -total[kinds.PURCHASE_RETURN],
    }


def _get_purchase(request, pk):
    return get_scoped_or_404(
        Purchase.objects.select_related("vendor", "created_by", "received_by"), request, pk=pk
    )


@permission_required(MANAGE)
def purchase_detail(request, pk):
    """Purchase order steps (items, review) and goods receipt (batch details, post stock)."""
    purchase = _get_purchase(request, pk)
    item_form = PurchaseItemForm(
        request.POST if request.POST.get("form") == "item" else None, request=request
    )
    if request.method == "POST" and item_form.is_bound and item_form.is_valid():
        c = item_form.cleaned_data
        try:
            item = purchasing.add_item(
                purchase,
                request=request,
                product=c["product"],
                quantity=c["quantity"],
                unit_cost=c["unit_cost"],
                free_quantity=c.get("free_quantity") or 0,
                batch_number=c["batch_number"],
                expiry_date=c["expiry_date"],
                sale_price=c["sale_price"],
            )
        except purchasing.PurchaseError as err:
            item_form.add_error(None, err.message)
        else:
            messages.success(request, f"{item.product} × {item.quantity} added.")
            return redirect("pharmacy:purchase-detail", pk=pk)
    details_form = PurchaseDetailsForm(
        initial={
            "supplier_invoice_number": purchase.supplier_invoice_number,
            "supplier_invoice_date": purchase.supplier_invoice_date,
            "discount": purchase.discount,
            "tax_amount": purchase.tax_amount,
            "further_tax": purchase.further_tax,
            "advance_tax": purchase.advance_tax,
            "notes": purchase.notes,
        }
    )
    items = list(purchase.items.select_related("product", "stock_txn__batch"))
    for i in items:
        i.returned = i.returned_quantity
    return render(
        request,
        "pharmacy/purchasing/purchase_detail.html",
        {
            "purchase": purchase,
            "items": items,
            "item_form": item_form,
            "details_form": details_form,
            "problems": purchasing.receipt_problems(purchase) if purchase.is_editable else [],
            "returns": purchase.returns.select_related("created_by").prefetch_related(
                "items__purchase_item__product"
            ),
            "can_return": purchase.status == Status.RECEIVED
            and any(i.returned < i.received_quantity for i in items),
            "today": timezone.localdate(),
            "breadcrumbs": _crumbs(
                {"label": "Purchases", "url": reverse("pharmacy:purchase-list")},
                {"label": purchase.number},
            ),
        },
    )


def _back(pk):
    return redirect("pharmacy:purchase-detail", pk=pk)


@require_POST
@permission_required(MANAGE)
def purchase_items_add(request, pk):
    """Add every row of the purchase grid in one go: all or none."""
    purchase = _get_purchase(request, pk)
    try:
        lines = grid_lines(request)
        purchasing.add_items(purchase, request=request, lines=lines)
    except GridError as err:
        messages.error(request, f"{err} Nothing was added.")
    except (ValueError, IndexError, InvalidOperation):
        messages.error(request, f"{BAD_GRID} Nothing was added.")
    except purchasing.PurchaseError as err:
        messages.error(request, f"{err.message} Nothing was added.")
    else:
        messages.success(request, f"{len(lines)} item{'s' if len(lines) != 1 else ''} added.")
    return _back(pk)


@permission_required(MANAGE)
def purchase_auto(request):
    """Low stock turned into draft orders, one per supplier."""
    rows = purchasing.reorder_suggestions(request)
    vendors = list(scoped(Vendor, request).filter(is_active=True))
    if request.method == "POST":
        by_pk = {v.pk: v for v in vendors}
        lines = []
        for row in rows:
            product = row["product"]
            if f"take_{product.pk}" not in request.POST:
                continue
            vendor_id = request.POST.get(f"vendor_{product.pk}", "")
            vendor = by_pk.get(int(vendor_id)) if vendor_id.isdigit() else None
            packs = request.POST.get(f"packs_{product.pk}", "")
            if vendor is None:
                messages.error(request, f"Choose a supplier for {product}. Nothing was created.")
                return redirect("pharmacy:purchase-auto")
            lines.append((product, vendor, int(packs) if packs.isdigit() else 0))
        if not lines:
            messages.error(request, "Tick at least one product.")
            return redirect("pharmacy:purchase-auto")
        try:
            purchases = purchasing.create_reorder_purchases(request=request, lines=lines)
        except purchasing.PurchaseError as err:
            messages.error(request, err.message)
            return redirect("pharmacy:purchase-auto")
        for purchase in purchases:
            audit.record(request, "purchase.create", purchase, summary="automatic reorder")
        messages.success(
            request, f"{len(purchases)} draft order{'s' if len(purchases) != 1 else ''} created."
        )
        return redirect("pharmacy:purchase-list")
    return render(
        request,
        "pharmacy/purchasing/purchase_auto.html",
        {
            "rows": rows,
            "vendors": vendors,
            "breadcrumbs": _crumbs(
                {"label": "Purchases", "url": reverse("pharmacy:purchase-list")},
                {"label": "Automatic reorder"},
            ),
        },
    )


@require_GET
@permission_required(MANAGE)
def purchase_print(request, pk):
    purchase = _get_purchase(request, pk)
    return render(
        request,
        "pharmacy/purchasing/purchase_print.html",
        {"purchase": purchase, "items": purchase.items.select_related("product")},
    )


@require_POST
@permission_required(MANAGE)
def purchase_item(request, pk, item_pk):
    """Goods receipt details for one line (batch, expiry, delivered quantity) or remove it."""
    purchase = _get_purchase(request, pk)
    item = PurchaseItem.objects.filter(pk=item_pk, purchase=purchase).first()
    if item is None:
        messages.error(request, "That line is not on this purchase.")
        return _back(pk)
    try:
        if request.POST.get("action") == "remove":
            purchasing.remove_item(item, request=request)
            messages.success(request, f"{item.product} removed.")
        else:
            sale_price = request.POST.get("sale_price", "").strip() or None
            purchasing.update_receipt(
                item,
                request=request,
                batch_number=request.POST.get("batch_number", ""),
                expiry_date=_date(request.POST.get("expiry_date"), None),
                sale_price=sale_price,
                quantity=_int(request.POST.get("quantity"), item.quantity),
                free_quantity=_int(request.POST.get("free_quantity"), item.free_quantity),
            )
            messages.success(request, f"{item.product}: receipt details saved.")
    except (purchasing.PurchaseError, ArithmeticError, ValueError) as err:
        messages.error(request, getattr(err, "message", "Check the values entered."))
    return _back(pk)


@require_POST
@permission_required(MANAGE)
def purchase_details(request, pk):
    purchase = _get_purchase(request, pk)
    form = PurchaseDetailsForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Check the invoice details: amounts must be zero or more.")
        return _back(pk)
    c = form.cleaned_data
    try:
        purchasing.set_invoice_details(
            purchase,
            request=request,
            supplier_invoice_number=c["supplier_invoice_number"],
            supplier_invoice_date=c["supplier_invoice_date"],
            discount=c["discount"] or 0,
            tax_amount=c["tax_amount"] or 0,
            further_tax=c["further_tax"] or 0,
            advance_tax=c["advance_tax"] or 0,
            notes=c["notes"],
        )
    except purchasing.PurchaseError as err:
        messages.error(request, err.message)
    else:
        messages.success(request, "Invoice details saved.")
    return _back(pk)


@require_POST
@permission_required(MANAGE)
def purchase_order(request, pk):
    purchase = _get_purchase(request, pk)
    try:
        purchasing.mark_ordered(purchase, request=request)
    except purchasing.PurchaseError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "purchase.order", purchase, summary=f"{purchase.vendor}")
        messages.success(request, f"{purchase.number} marked as ordered.")
    return _back(pk)


@require_POST
@permission_required(MANAGE)
def purchase_receive(request, pk):
    purchase = _get_purchase(request, pk)
    try:
        purchase = purchasing.receive(purchase, request=request)
    except purchasing.PurchaseError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "purchase.receive",
            purchase,
            summary=(
                f"{purchase.number} {purchase.vendor} invoice {purchase.supplier_invoice_number}"
                f" total {purchase.grand_total}"
            ),
        )
        messages.success(request, f"{purchase.number} received. Stock is now available for sale.")
    return _back(pk)


@require_POST
@permission_required(MANAGE)
def purchase_cancel(request, pk):
    purchase = _get_purchase(request, pk)
    try:
        purchasing.cancel(purchase, request=request, reason=request.POST.get("reason", ""))
    except purchasing.PurchaseError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "purchase.cancel", purchase, summary=purchase.cancel_reason)
        messages.success(request, f"{purchase.number} cancelled.")
    return _back(pk)


@permission_required(MANAGE)
def purchase_return(request, pk):
    """Return received stock to the supplier."""
    purchase = _get_purchase(request, pk)
    if purchase.status != Status.RECEIVED:
        messages.error(request, "Only received purchases can be returned.")
        return _back(pk)
    items = list(purchase.items.select_related("product", "stock_txn__batch"))
    for i in items:
        i.returnable = min(
            i.received_quantity - i.returned_quantity, i.stock_txn.batch.quantity_available
        )
        i.posted = request.POST.get(f"qty_{i.pk}", "")
    error = ""
    if request.method == "POST":
        quantities = {i.pk: _int(request.POST.get(f"qty_{i.pk}")) for i in items}
        try:
            ret = purchasing.return_items(
                purchase,
                request=request,
                quantities=quantities,
                reason=request.POST.get("reason", ""),
                reference=request.POST.get("reference", ""),
            )
        except purchasing.PurchaseError as err:
            error = err.message
        else:
            audit.record(
                request,
                "purchase.return",
                ret,
                summary=f"{ret.number} on {purchase.number}: {ret.amount} — {ret.reason}",
            )
            messages.success(
                request, f"{ret.number}: stock sent back, supplier credited {ret.amount:,.2f}."
            )
            return _back(pk)
    return render(
        request,
        "pharmacy/purchasing/purchase_return.html",
        {
            "purchase": purchase,
            "items": items,
            "error": error,
            "post": request.POST,
            "breadcrumbs": _crumbs(
                {"label": "Purchases", "url": reverse("pharmacy:purchase-list")},
                {"label": purchase.number, "url": reverse("pharmacy:purchase-detail", args=[pk])},
                {"label": "Return to supplier"},
            ),
        },
    )


# --- Report ------------------------------------------------------------------------------------


@require_GET
@permission_required(MANAGE, "reports.financial.view", any_of=True)
def purchase_report(request):
    today = timezone.localdate()
    start = _date(request.GET.get("from"), today.replace(day=1))
    end = _date(request.GET.get("to"), today)
    if end < start:
        start, end = end, start
    purchases, returns = scoped(Purchase, request), scoped(PurchaseReturn, request)
    vendor_id = request.GET.get("vendor", "")
    if vendor_id.isdigit():
        purchases = purchases.filter(vendor_id=vendor_id)
        returns = returns.filter(vendor_id=vendor_id)
    data = purchasing.report(purchases, returns, start, end)
    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="purchases-{start:%Y%m%d}-{end:%Y%m%d}.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(["Received", "Purchase", "Supplier", "Supplier invoice", "Subtotal",
                         "Discount", "Tax", "Total"])  # fmt: skip
        for p in data["purchases"]:
            writer.writerow([
                f"{timezone.localtime(p.received_at):%Y-%m-%d %H:%M}", p.number, p.vendor.name,
                p.supplier_invoice_number, p.subtotal, p.discount, p.tax_amount, p.grand_total,
            ])  # fmt: skip
        for r in data["returns"]:
            writer.writerow([
                f"{timezone.localtime(r.created_at):%Y-%m-%d %H:%M}", r.number, r.vendor.name,
                r.reference, "", "", "", -r.amount,
            ])  # fmt: skip
        audit.record(request, "report.export", summary=f"purchases {start}–{end}")
        return response
    return render(
        request,
        "pharmacy/purchasing/purchase_report.html",
        {
            **data,
            "start": start,
            "end": end,
            "vendor_id": vendor_id,
            "vendors": scoped(Vendor, request),
            "presets": {
                "this_month": (today.replace(day=1), today),
                "last_30": (today - timedelta(days=29), today),
            },
            "units": data["purchases"].aggregate(s=Sum("items__quantity"))["s"] or 0,
            "breadcrumbs": _crumbs({"label": "Purchase report"}),
        },
    )


@permission_required(MANAGE)
def supplier_returns(request):
    """Return medicine to a supplier: choose the supplier, scan or type medicines, send back."""
    vendors = scoped(Vendor, request).filter(is_active=True).order_by("name")
    error = ""
    if request.method == "POST":
        vendor = vendors.filter(pk=_int(request.POST.get("vendor"))).first()
        quantities = {
            _int(key.removeprefix("qty_")): _int(value)
            for key, value in request.POST.items()
            if key.startswith("qty_")
        }
        try:
            if vendor is None:
                raise purchasing.PurchaseError("Choose the supplier.")
            ret = purchasing.return_to_supplier(
                vendor,
                request=request,
                quantities=quantities,
                reason=request.POST.get("reason", ""),
                reference=request.POST.get("reference", ""),
            )
        except purchasing.PurchaseError as err:
            error = err.message
        else:
            audit.record(
                request,
                "purchase.return",
                ret,
                summary=f"{ret.number} to {vendor}: {ret.amount} — {ret.reason}",
            )
            messages.success(
                request, f"{ret.number}: stock sent back, {vendor} credited {ret.amount:,.2f}."
            )
            return redirect("pharmacy:supplier-returns")
    returns = (
        scoped(PurchaseReturn, request)
        .select_related("vendor", "purchase", "created_by")
        .prefetch_related("items__stock_txn__product")[:50]
    )
    return render(
        request,
        "pharmacy/purchasing/supplier_returns.html",
        {
            "vendors": vendors,
            "chosen_vendor": _int(request.POST.get("vendor") or request.GET.get("vendor")),
            "returns": returns,
            "error": error,
            "post": request.POST,
            "active": "returns",
            "breadcrumbs": _crumbs({"label": "Returns to supplier"}),
        },
    )


@require_GET
@permission_required(MANAGE)
def supplier_return_batches(request):
    """Type-to-search for the return screen: the batches on the shelf, first to expire first."""
    q = request.GET.get("q", "").strip()
    rows = []
    for product in pos.search_products(request, q, limit=8) if q else []:
        batches = InventoryBatch.objects.filter(
            branch=request.branch, product=product, quantity_available__gt=0
        ).order_by("expiry_date")
        rows += [
            {
                "id": b.pk,
                "name": product.display_name,
                "batch": b.label,
                "expiry": f"{b.expiry_date:%b %Y}",
                "available": b.quantity_available,
                "unit": product.unit,
                "cost": float(b.purchase_cost),
            }
            for b in batches
        ]
    return JsonResponse({"rows": rows})
