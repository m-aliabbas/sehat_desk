"""Suppliers, purchases, goods receipt, purchase returns and the purchase report (design screens
58–64). Every page needs pharmacy.inventory.manage; the report also opens for
reports.financial.view. Writes go through pharmacy/purchasing.py."""

import csv
from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, paginate, scoped

from . import purchasing
from .forms import PurchaseCreateForm, PurchaseDetailsForm, PurchaseItemForm, VendorForm
from .models import Purchase, PurchaseItem, PurchaseReturn, Vendor, VendorLedgerEntry

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
            "breadcrumbs": _crumbs({"label": "Purchases"}),
        },
    )


@permission_required(MANAGE)
def purchase_create(request):
    initial = {}
    if request.GET.get("vendor", "").isdigit():
        initial["vendor"] = request.GET["vendor"]
    form = PurchaseCreateForm(request.POST or None, request=request, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            purchase = purchasing.create_purchase(
                request=request,
                vendor=c["vendor"],
                supplier_invoice_number=c["supplier_invoice_number"],
                notes=c["notes"],
            )
        except purchasing.PurchaseError as err:
            form.add_error(None, err.message)
        else:
            audit.record(request, "purchase.create", purchase, summary=f"{purchase.vendor}")
            return redirect("pharmacy:purchase-detail", pk=purchase.pk)
    return render(
        request,
        "pharmacy/purchasing/purchase_create.html",
        {
            "form": form,
            "has_vendors": scoped(Vendor, request).filter(is_active=True).exists(),
            "breadcrumbs": _crumbs(
                {"label": "Purchases", "url": reverse("pharmacy:purchase-list")},
                {"label": "New purchase"},
            ),
        },
    )


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
