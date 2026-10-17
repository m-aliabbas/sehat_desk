"""Pharmacy POS screens (design screens 43–53, 130–131): the till, payment, held sales, the
prescription queue, sales, returns and the receipt. Every write goes through pharmacy/pos.py.

Till actions are htmx requests that return the re-rendered cart (#pos-cart); errors come back
as a toast. Without htmx they redirect to the till with a message."""

import json
from collections import defaultdict
from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import HttpResponseClientRedirect

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.billing import services as billing
from apps.billing.models import to_money
from apps.core.scoping import get_scoped_or_404, scoped
from apps.patients.models import Patient
from apps.prescriptions.models import Prescription, PrescriptionItem

from . import pos, stock
from .models import InventoryBatch, PharmacySale, PharmacySaleItem, PharmacySaleReturn, Product

SELL, DISCOUNT, REFUND, COLLECT = pos.SELL, pos.DISCOUNT, pos.REFUND, pos.COLLECT
Status = PharmacySale.Status
MAX_PAYMENT_ROWS = 3


def _crumbs(*extra):
    return [{"label": "Pharmacy", "url": reverse("pharmacy:pos")}, *extra]


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- Till --------------------------------------------------------------------------------------


def _cart_context(request):
    cart = pos.current_cart(request)
    items = []
    if cart is not None:
        items = list(
            cart.items.select_related("product", "batch", "prescription_item").order_by(
                "created_at", "pk"
            )
        )
        product_ids = {i.product_id for i in items}
        options = defaultdict(list)
        for b in stock.saleable(request.branch).filter(product_id__in=product_ids):
            options[b.product_id].append(b)
        on_hand = defaultdict(int)
        for b in options.values():
            for batch in b:
                on_hand[batch.product_id] += batch.quantity_available
        near_days = stock.near_expiry_days(request.organization)
        in_cart = defaultdict(int)
        for i in items:
            in_cart[i.product_id] += i.quantity
        for i in items:
            i.batch_options = options.get(i.product_id, [])
            if i.batch not in i.batch_options:
                i.batch_options = [i.batch, *i.batch_options]
            i.near_expiry = 0 <= i.batch.days_to_expiry <= near_days
            left = on_hand[i.product_id] - in_cart[i.product_id]
            i.low_after = i.product.reorder_level and left <= i.product.reorder_level
            i.left_after = left
    return {
        "cart": cart,
        "items": items,
        "totals": pos.totals(cart) if cart else None,
        "problems": pos.checkout_problems(cart) if cart and items else [],
        "held_count": scoped(PharmacySale, request).filter(status=Status.HELD).count(),
        "rx_waiting": pos.dispensable(request).count(),
        "can_discount": has_perm(request, DISCOUNT),
        "can_collect": has_perm(request, COLLECT),
        "can_refund": has_perm(request, REFUND),
    }


def _today(request):
    return (
        scoped(PharmacySale, request)
        .filter(status=Status.COMPLETED, completed_at__date=timezone.localdate())
        .aggregate(n=Count("id"), total=Sum("grand_total"))
    )


def _cart_reply(request, *, error="", success="", close_overlay=False):
    if request.htmx:
        response = render(request, "pharmacy/pos/_cart.html", _cart_context(request))
        events = {}
        if error or success:
            events["toast"] = {"level": "danger" if error else "success",
                               "message": error or success}  # fmt: skip
        if close_overlay:
            events["close-overlay"] = True
            response["HX-Retarget"] = "#pos-cart"
            response["HX-Reswap"] = "outerHTML"
        if events:
            response["HX-Trigger"] = json.dumps(events)
        return response
    if error:
        messages.error(request, error)
    elif success:
        messages.success(request, success)
    return redirect("pharmacy:pos")


@require_GET
@permission_required(SELL)
def till(request):
    """Pharmacy point of sale (design screen 43)."""
    return render(
        request,
        "pharmacy/pos/till.html",
        {**_cart_context(request), "today": _today(request), "breadcrumbs": None},
    )


@require_GET
@permission_required(SELL)
def search(request):
    q = request.GET.get("q", "")
    return render(
        request,
        "pharmacy/pos/_search_results.html",
        {"results": pos.search_products(request, q), "q": q.strip()},
    )


@require_POST
@permission_required(SELL)
def add(request):
    """Add a product (from a search result), or a scanned barcode/SKU (q)."""
    quantity = max(_int(request.POST.get("quantity"), 1), 1)
    product = None
    if request.POST.get("product", "").isdigit():
        product = get_scoped_or_404(Product, request, pk=request.POST["product"])
    elif request.POST.get("q", "").strip():
        q = request.POST["q"].strip()
        matches = pos.search_products(request, q)
        exact = [p for p in matches if q.lower() in (p.barcode.lower(), p.sku.lower())]
        if len(exact) == 1 or len(matches) == 1:
            product = (exact or matches)[0]
        else:
            return _cart_reply(
                request,
                error=f"No product with barcode or SKU “{q}”. Pick one from the list."
                if not matches
                else "Several products match. Pick one from the list.",
            )
    if product is None:
        return _cart_reply(request, error="Choose a product.")
    try:
        cart = pos.open_cart(request)
        pos.add_product(cart, request=request, product=product, quantity=quantity)
    except pos.PosError as err:
        return _cart_reply(request, error=err.message)
    return _cart_reply(request)


def _cart_item(request, pk):
    return get_object_or_404(
        PharmacySaleItem.objects.select_related("sale", "batch", "product"),
        pk=pk,
        sale__organization=request.organization,
        sale__branch=request.branch,
    )


@require_POST
@permission_required(SELL)
def item(request, pk):
    """Change a cart line: quantity, batch, or remove."""
    line = _cart_item(request, pk)
    try:
        if request.POST.get("action") == "remove":
            pos.remove_item(line, request=request)
        elif "batch" in request.POST:
            batch = get_scoped_or_404(InventoryBatch, request, pk=_int(request.POST["batch"]))
            pos.change_batch(line, request=request, batch=batch)
        else:
            pos.set_quantity(line, request=request, quantity=_int(request.POST.get("quantity")))
    except pos.PosError as err:
        return _cart_reply(request, error=err.message)
    return _cart_reply(request)


@require_POST
@permission_required(SELL)
def customer(request):
    """Attach a registered patient (patient=<id>) or name a walk-in customer."""
    patient = None
    if request.POST.get("patient", "").isdigit():
        patient = get_scoped_or_404(Patient, request, pk=request.POST["patient"])
    try:
        cart = pos.open_cart(request)
        pos.attach_patient(
            cart,
            request=request,
            patient=patient,
            customer_name=request.POST.get("customer_name", ""),
        )
    except pos.PosError as err:
        return _cart_reply(request, error=err.message)
    return _cart_reply(request)


@permission_required(SELL)
def prescription(request):
    """GET: modal listing prescriptions waiting at this pharmacy. POST: attach (rx) or detach."""
    if request.method == "POST":
        try:
            cart = pos.open_cart(request)
            if request.POST.get("action") == "detach":
                pos.detach_prescription(cart, request=request)
                return _cart_reply(request, success="Prescription removed from the sale.")
            rx = get_scoped_or_404(Prescription, request, pk=_int(request.POST.get("rx")))
            pos.attach_prescription(cart, request=request, prescription=rx)
        except pos.PosError as err:
            return _cart_reply(request, error=err.message)
        return _cart_reply(request, success=f"{rx.number} attached.")
    cart = pos.current_cart(request)
    rxs = pos.dispensable(request).select_related("patient", "doctor__user")
    q = request.GET.get("q", "").strip()
    if cart and cart.patient_id:
        rxs = rxs.filter(patient_id=cart.patient_id)
    if q:
        rxs = rxs.filter(
            Q(number__icontains=q)
            | Q(patient__mrn__icontains=q)
            | Q(patient__name_search__icontains=" ".join(q.lower().split()))
        )
    return render(
        request,
        "pharmacy/pos/_rx_modal.html",
        {"prescriptions": rxs.order_by("-sent_to_pharmacy_at")[:30], "cart": cart, "q": q},
    )


@permission_required(DISCOUNT)
def discount(request):
    cart = pos.current_cart(request)
    if cart is None:
        return _cart_reply(request, error="Add items before giving a discount.", close_overlay=True)
    if request.method == "POST":
        before = str(cart.discount)
        try:
            pos.set_discount(
                cart,
                request=request,
                amount=request.POST.get("amount") or 0,
                reason=request.POST.get("reason", ""),
            )
        except (pos.PosError, ArithmeticError, ValueError) as err:
            message = getattr(err, "message", "Enter a valid amount.")
            return render(
                request,
                "pharmacy/pos/_discount_modal.html",
                {"cart": cart, "totals": pos.totals(cart), "error": message},
            )
        cart.refresh_from_db()
        audit.record(
            request,
            "pharmacy_sale.discount",
            cart,
            changes={"discount": [before, str(cart.discount)]},
            summary=f"{cart.number}: {cart.discount} — {cart.discount_reason[:120]}",
        )
        return _cart_reply(request, success="Discount saved.", close_overlay=True)
    return render(
        request, "pharmacy/pos/_discount_modal.html", {"cart": cart, "totals": pos.totals(cart)}
    )


@require_POST
@permission_required(SELL)
def hold(request):
    cart = pos.current_cart(request)
    if cart is None:
        return _cart_reply(request, error="The cart is empty; there is nothing to hold.")
    try:
        pos.hold(cart, request=request, note=request.headers.get("HX-Prompt", "") or
                 request.POST.get("note", ""))  # fmt: skip
    except pos.PosError as err:
        return _cart_reply(request, error=err.message)
    audit.record(request, "pharmacy_sale.hold", cart, summary=cart.number)
    return _cart_reply(request, success=f"{cart.number} held. Recall it from Held sales.")


@require_POST
@permission_required(SELL)
def clear(request):
    cart = pos.current_cart(request)
    if cart is not None:
        try:
            pos.clear(cart, request=request)
        except pos.PosError as err:
            return _cart_reply(request, error=err.message)
        audit.record(request, "pharmacy_sale.clear", cart, summary=cart.number)
    return _cart_reply(request, success="Cart cleared.")


def _payment_rows(request) -> list[dict]:
    methods = {m.pk: m for m in billing.active_methods(request.organization)}
    rows = []
    for i in range(MAX_PAYMENT_ROWS):
        amount = request.POST.get(f"amount_{i}", "").strip()
        method = methods.get(_int(request.POST.get(f"method_{i}")))
        if not amount:
            continue
        if method is None:
            raise pos.PosError("Choose the payment method.")
        if method.requires_reference and not request.POST.get(f"reference_{i}", "").strip():
            raise pos.PosError(f"{method.name} payments need a reference number.")
        try:
            rows.append(
                {"method": method, "amount": to_money(amount),
                 "reference": request.POST.get(f"reference_{i}", "")}
            )  # fmt: skip
        except ArithmeticError as err:
            raise pos.PosError("Enter valid amounts.") from err
    return rows


@permission_required(SELL, COLLECT)
def pay(request):
    """Payment modal (design screen 48): one or more methods adding up to the total."""
    cart = pos.current_cart(request)
    if cart is None or not cart.items.exists():
        return _cart_reply(request, error="The cart is empty.", close_overlay=True)
    error = ""
    if request.method == "POST":
        try:
            sale = pos.checkout(cart, request=request, payments=_payment_rows(request))
        except pos.PosError as err:
            error = err.message
        else:
            audit.record(
                request,
                "pharmacy_sale.complete",
                sale,
                summary=f"{sale.number} {sale.grand_total} invoice {sale.invoice.number}"
                + (f" for {sale.prescription.number}" if sale.prescription_id else ""),
            )
            for line in sale.items.filter(substituted=True).select_related("product"):
                audit.record(
                    request,
                    "pharmacy_sale.substitution",
                    sale,
                    summary=f"{sale.number}: {line.product} for "
                    f"{line.prescription_item.medicine_name if line.prescription_item else '—'}"
                    f" — {line.substitution_reason}",
                )
            url = f"{reverse('pharmacy:sale-detail', args=[sale.pk])}?done=1"
            return HttpResponseClientRedirect(url) if request.htmx else redirect(url)
    return render(
        request,
        "pharmacy/pos/_pay_modal.html" if request.htmx else "pharmacy/pos/pay.html",
        {
            "cart": cart,
            "totals": pos.totals(cart),
            "problems": pos.checkout_problems(cart),
            "methods": billing.active_methods(request.organization),
            "error": error,
            "breadcrumbs": _crumbs({"label": "Take payment"}),
        },
    )


# --- Held sales --------------------------------------------------------------------------------


@require_GET
@permission_required(SELL)
def held(request):
    """Held sales (design screen 50)."""
    sales = (
        scoped(PharmacySale, request)
        .filter(status=Status.HELD)
        .select_related("patient", "cashier", "prescription")
        .annotate(lines=Count("items"), units=Sum("items__quantity"))
        .order_by("-held_at")
    )
    return render(
        request,
        "pharmacy/pos/held.html",
        {"sales": sales, "breadcrumbs": _crumbs({"label": "Held sales"})},
    )


@require_POST
@permission_required(SELL)
def resume(request, pk):
    sale = get_scoped_or_404(PharmacySale, request, pk=pk)
    try:
        pos.resume(sale, request=request)
    except pos.PosError as err:
        messages.error(request, err.message)
        return redirect("pharmacy:pos-held")
    audit.record(request, "pharmacy_sale.resume", sale, summary=sale.number)
    messages.success(request, f"{sale.number} is back on the till.")
    return redirect("pharmacy:pos")


@require_POST
@permission_required(SELL)
def held_clear(request, pk):
    sale = get_scoped_or_404(PharmacySale, request, pk=pk)
    try:
        pos.clear(sale, request=request)
    except pos.PosError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "pharmacy_sale.clear", sale, summary=sale.number)
        messages.success(request, f"{sale.number} cleared.")
    return redirect("pharmacy:pos-held")


# --- Prescription queue ------------------------------------------------------------------------


@require_GET
@permission_required(SELL)
def rx_queue(request):
    """Prescriptions sent to this pharmacy, with their dispensing status."""
    view = request.GET.get("view", "pending")
    q = request.GET.get("q", "").strip()
    rxs = (
        scoped(Prescription, request)
        .filter(sent_to_pharmacy_at__isnull=False, status=Prescription.Status.SIGNED)
        .select_related("patient", "doctor__user")
        .prefetch_related("items")
    )
    waiting = rxs.filter(
        dispense_status__in=[Prescription.Dispense.PENDING, Prescription.Dispense.PARTIAL]
    )
    if view == "pending":
        rxs = waiting.order_by("sent_to_pharmacy_at")
    else:
        rxs = rxs.order_by("-sent_to_pharmacy_at")
    if q:
        rxs = rxs.filter(
            Q(number__icontains=q)
            | Q(patient__mrn__icontains=q)
            | Q(patient__name_search__icontains=" ".join(q.lower().split()))
        )
    today = timezone.localdate()
    return render(
        request,
        "pharmacy/pos/rx_queue.html",
        {
            "prescriptions": rxs[:100],
            "view": view,
            "q": q,
            "waiting_count": waiting.count(),
            "partial_count": waiting.filter(dispense_status=Prescription.Dispense.PARTIAL).count(),
            "today_count": scoped(Prescription, request)
            .filter(sent_to_pharmacy_at__date=today)
            .count(),
            "breadcrumbs": _crumbs({"label": "Prescriptions"}),
        },
    )


@permission_required(SELL)
def rx_detail(request, pk):
    """Open a prescription (design screen 45): availability per line, choose products and
    quantities (partial fulfilment allowed, substitutes need a reason) and send to the till."""
    rx = get_scoped_or_404(
        Prescription.objects.select_related("patient", "doctor__user", "encounter"),
        request,
        pk=pk,
        sent_to_pharmacy_at__isnull=False,
        status=Prescription.Status.SIGNED,
    )
    lines = pos.prescription_lines(rx, request.branch)
    errors = []
    if request.method == "POST":
        products = {p.pk: p for p in scoped(Product, request).filter(is_active=True)}
        items = {i.pk: i for i in PrescriptionItem.objects.filter(prescription=rx)}
        chosen = []
        for line in lines:
            key = line["item"].pk
            qty = _int(request.POST.get(f"qty_{key}"))
            product = products.get(_int(request.POST.get(f"product_{key}")))
            if qty and product is None:
                errors.append(f"{line['item'].medicine_name}: choose the product to dispense.")
            elif qty:
                chosen.append(
                    {"rx_item": items[key], "product": product, "quantity": qty,
                     "reason": request.POST.get(f"reason_{key}", "")}
                )  # fmt: skip
        if not errors:
            try:
                cart = pos.open_cart(request)
                pos.fill_from_prescription(cart, request=request, prescription=rx, lines=chosen)
            except pos.PosError as err:
                errors.append(err.message)
            else:
                audit.record(request, "prescription.dispense_start", rx, summary=cart.number)
                messages.success(request, f"{rx.number} added to {cart.number}. Take payment.")
                return redirect("pharmacy:pos")
    return render(
        request,
        "pharmacy/pos/rx_detail.html",
        {
            "rx": rx,
            "lines": lines,
            "errors": errors,
            "post": request.POST,
            "sales": rx.pharmacy_sales.filter(status=Status.COMPLETED).order_by("completed_at"),
            "can_dispense": rx.dispense_status
            in (Prescription.Dispense.PENDING, Prescription.Dispense.PARTIAL),
            "breadcrumbs": _crumbs(
                {"label": "Prescriptions", "url": reverse("pharmacy:rx-queue")},
                {"label": rx.number},
            ),
        },
    )


@require_POST
@permission_required(SELL)
def rx_close(request, pk):
    rx = get_scoped_or_404(Prescription, request, pk=pk)
    try:
        pos.close_prescription(rx, request=request, note=request.POST.get("note", ""))
    except pos.PosError as err:
        messages.error(request, err.message)
        return redirect("pharmacy:rx-detail", pk=pk)
    audit.record(request, "prescription.dispense_close", rx, summary=request.POST["note"][:150])
    messages.success(request, f"{rx.number} closed.")
    return redirect("pharmacy:rx-queue")


# --- Sales, receipt and returns ----------------------------------------------------------------


def _parse_date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


@require_GET
@permission_required(SELL, REFUND, any_of=True)
def sale_list(request):
    """Completed sales (find the original sale for a return, design screen 51)."""
    today = timezone.localdate()
    start = _parse_date(request.GET.get("from"), today - timedelta(days=6))
    end = _parse_date(request.GET.get("to"), today)
    q = request.GET.get("q", "").strip()
    sales = (
        scoped(PharmacySale, request)
        .filter(status=Status.COMPLETED)
        .select_related("patient", "cashier", "invoice", "prescription")
        .order_by("-completed_at")
    )
    if q:
        sales = sales.filter(
            Q(number__icontains=q)
            | Q(invoice__number__icontains=q)
            | Q(customer_name__icontains=q)
            | Q(patient__mrn__icontains=q)
            | Q(patient__name_search__icontains=" ".join(q.lower().split()))
        )
    else:
        sales = sales.filter(completed_at__date__gte=start, completed_at__date__lte=end)
    returns = scoped(PharmacySaleReturn, request).filter(
        created_at__date__gte=start, created_at__date__lte=end
    )
    return render(
        request,
        "pharmacy/pos/sale_list.html",
        {
            "sales": sales[:300],
            "q": q,
            "start": start,
            "end": end,
            "total": sales.aggregate(s=Sum("grand_total"))["s"] or 0,
            "returned": returns.aggregate(s=Sum("amount"))["s"] or 0,
            "breadcrumbs": _crumbs({"label": "Sales"}),
        },
    )


def _get_sale(request, pk):
    return get_scoped_or_404(
        PharmacySale.objects.select_related(
            "patient", "cashier", "invoice", "prescription__doctor__user", "discount_by"
        ),
        request,
        pk=pk,
        status=Status.COMPLETED,
    )


@require_GET
@permission_required(SELL, REFUND, any_of=True)
def sale_detail(request, pk):
    """Sale completed (design screen 49) and its returns."""
    sale = _get_sale(request, pk)
    items = list(sale.items.select_related("product", "batch", "prescription_item"))
    for i in items:
        i.returned = i.returned_quantity
    return render(
        request,
        "pharmacy/pos/sale_detail.html",
        {
            "sale": sale,
            "items": items,
            "payments": sale.invoice.payments.select_related("method"),
            "returns": sale.returns.select_related("created_by").prefetch_related(
                "items__sale_item__product", "refunds__method"
            ),
            "done": request.GET.get("done") == "1",
            "can_return": has_perm(request, REFUND) and any(i.returned < i.quantity for i in items),
            "breadcrumbs": _crumbs(
                {"label": "Sales", "url": reverse("pharmacy:sale-list")}, {"label": sale.number}
            ),
        },
    )


@require_GET
@permission_required(SELL, REFUND, any_of=True)
def sale_receipt(request, pk):
    """Pharmacy receipt (design screen 130), sized for 80 mm printers."""
    sale = _get_sale(request, pk)
    return render(
        request,
        "pharmacy/pos/receipt.html",
        {
            "sale": sale,
            "items": sale.items.select_related("product", "batch"),
            "payments": sale.invoice.payments.select_related("method"),
            "billing_settings": billing.settings_for(request.organization),
        },
    )


@permission_required(REFUND)
def sale_return(request, pk):
    """Review return quantities and refund (design screens 52–53)."""
    sale = _get_sale(request, pk)
    items = list(sale.items.select_related("product", "batch"))
    for i in items:
        i.returnable = i.quantity - i.returned_quantity
        i.posted = request.POST.get(f"qty_{i.pk}", "")
    error = ""
    if request.method == "POST":
        quantities = {i.pk: _int(request.POST.get(f"qty_{i.pk}")) for i in items}
        try:
            ret = pos.return_items(
                sale, request=request, quantities=quantities, reason=request.POST.get("reason", "")
            )
        except pos.PosError as err:
            error = err.message
        else:
            audit.record(
                request,
                "pharmacy_sale.return",
                ret,
                summary=f"{ret.number} on {sale.number}: {ret.amount} refunded — {ret.reason}",
            )
            messages.success(
                request,
                f"{ret.number}: stock returned to shelf, {ret.amount:,.2f} refunded.",
            )
            return redirect("pharmacy:sale-detail", pk=sale.pk)
    return render(
        request,
        "pharmacy/pos/sale_return.html",
        {
            "sale": sale,
            "items": items,
            "error": error,
            "post": request.POST,
            "breadcrumbs": _crumbs(
                {"label": "Sales", "url": reverse("pharmacy:sale-list")},
                {"label": sale.number, "url": reverse("pharmacy:sale-detail", args=[sale.pk])},
                {"label": "Return"},
            ),
        },
    )
