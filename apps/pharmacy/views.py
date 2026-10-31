"""Inventory screens: dashboard with alerts, products, batches (FEFO), stock receipt,
adjustments/write-offs and the stock ledger. Viewing needs pharmacy.inventory.manage or
pharmacy.sale; every stock change needs pharmacy.inventory.manage (checked in stock.py too)."""

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.labels import LABEL_PAPERS, label_paper
from apps.core.scoping import get_scoped_or_404, paginate, scoped
from apps.organizations.models import preferences_for

from . import barcodes, catalog, pricing, stock, views_purchasing
from .forms import BatchActionForm, ProductForm, ReceiveForm
from .models import (
    InventoryBatch,
    Product,
    ProductCategory,
    ProductPriceChange,
    StockTransaction,
    UnitOfMeasure,
)
from .packing import parse_packing

MANAGE = "pharmacy.inventory.manage"
VIEW = (MANAGE, "pharmacy.sale")


def _crumbs(*extra):
    return [{"label": "Inventory", "url": reverse("pharmacy:dashboard")}, *extra]


@require_GET
@permission_required(*VIEW, any_of=True)
def dashboard(request):
    low = list(stock.low_stock(request.branch, request.organization)[:20])
    near = list(stock.near_expiry(request.branch)[:20])
    expired = list(stock.expired(request.branch)[:20])
    return render(
        request,
        "pharmacy/dashboard.html",
        {
            "low": low,
            "near": near,
            "expired": expired,
            "low_count": stock.low_stock(request.branch, request.organization).count(),
            "near_count": stock.near_expiry(request.branch).count(),
            "expired_count": stock.expired(request.branch).count(),
            "value": stock.stock_value(request.branch),
            "product_count": scoped(Product, request).filter(is_active=True).count(),
            "recent": scoped(StockTransaction, request).select_related(
                "product", "batch", "created_by"
            )[:10],
            "near_days": stock.near_expiry_days(request.organization),
            "can_manage": has_perm(request, MANAGE),
        },
    )


@require_GET
@permission_required(*VIEW, any_of=True)
def product_list(request):
    q = request.GET.get("q", "").strip().lower()
    filt = request.GET.get("filter", "")
    products = (
        stock.products_with_stock(request.branch, request.organization)
        .select_related("category", "medicine")
        .order_by("brand_name", "strength")
    )
    if q:
        for word in q.split():
            products = products.filter(search_text__contains=word)
    if filt == "low":
        products = stock.low_stock(request.branch, request.organization)
    elif filt == "out":
        products = products.filter(stock=0, is_active=True)
    elif filt == "inactive":
        products = products.filter(is_active=False)
    return render(
        request,
        "pharmacy/product_list.html",
        {
            "products": (page := paginate(request, products)),
            "page_obj": page,
            "q": q,
            "filter": filt,
            "categories": scoped(ProductCategory, request),
            "can_manage": has_perm(request, MANAGE),
            "breadcrumbs": _crumbs({"label": "Products"}),
        },
    )


@permission_required(MANAGE)
def product_form(request, pk=None):
    product = (
        get_scoped_or_404(Product, request, pk=pk)
        if pk
        else Product(
            organization=request.organization,
            reorder_level=preferences_for(request.organization).default_reorder_level,
            tax_percent=preferences_for(request.organization).default_sale_tax_percent,
        )
    )
    creating = pk is None
    form = ProductForm(request.POST or None, instance=product, request=request)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            product = catalog.save_product(form, request=request)
        audit.record(request, "product.create" if creating else "product.update", product)
        messages.success(request, f"{product} saved.")
        return redirect("pharmacy:product-detail", pk=product.pk)
    return render(
        request,
        "pharmacy/product_form.html",
        {
            "form": form,
            "creating": creating,
            "product": product,
            "category_defaults": _category_defaults(request),
            "packing_state": form.packing_state(),
            "packing_presets": catalog.packing_presets(request.organization),
            "has_stock": not creating and stock.on_hand(request.branch, product) > 0,
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": "New product" if creating else product.display_name},
            ),
        },
    )


MAX_LABELS = 500


def _category_defaults(request) -> dict:
    return {
        str(c.pk): {"tax": str(c.default_tax_percent), "rx": c.requires_prescription}
        for c in scoped(ProductCategory, request)
    }


@permission_required(MANAGE)
def product_bulk(request):
    """Add many products on one screen, typed or scanned, with prices per pack."""
    rows, errors, add_to_medicines = [], {}, True
    if request.method == "POST":
        rows = catalog.read_rows(request.POST)
        add_to_medicines = bool(request.POST.get("add_to_medicines"))
        products, errors = catalog.add_many(
            rows, request=request, add_to_medicines=add_to_medicines
        )
        if products:
            for product in products:
                audit.record(request, "product.create", product)
            messages.success(request, f"{len(products)} products added.")
            return redirect("pharmacy:product-list")
        if not rows:
            messages.error(request, "Enter at least one product.")
    for i, row in enumerate(rows):
        row["error"] = errors.get(i, "")
    return render(
        request,
        "pharmacy/product_bulk.html",
        {
            "rows": rows,
            "add_to_medicines": add_to_medicines,
            "categories": scoped(ProductCategory, request),
            "category_defaults": _category_defaults(request),
            "packing_presets": catalog.packing_presets(request.organization),
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": "Add many"},
            ),
        },
    )


@require_GET
@permission_required(MANAGE)
def product_suggest(request):
    """Search box for purchase entry. An exact barcode or SKU comes back alone, for scanners."""
    q = request.GET.get("q", "").strip()
    qs = scoped(Product, request).filter(is_active=True)
    exact = list(qs.filter(barcode__iexact=q)) or list(qs.filter(sku__iexact=q)) if q else []
    matches = exact or (list(qs.filter(search_text__contains=q.lower())[:8]) if len(q) >= 2 else [])
    return JsonResponse(
        {
            "exact": bool(exact),
            "results": [
                {
                    "id": p.pk,
                    "name": p.display_name,
                    "sku": p.sku,
                    "packSize": p.pack_size,
                    "packsPerBox": p.packs_per_box,
                    "packLabel": p.get_pack_unit_display(),
                    "boxLabel": p.box_unit,
                    "unitLabel": p.get_unit_display(),
                    "packCost": str((p.purchase_price * p.pack_size).quantize(Decimal("0.01"))),
                    **views_purchasing._product_row(p),
                }
                for p in matches
            ],
        }
    )


@permission_required(MANAGE)
def uom(request):
    """Units of measure: add phrases to the suggestions."""
    if request.method == "POST":
        if request.POST.get("remove", "").isdigit():
            scoped(UnitOfMeasure, request).filter(pk=request.POST["remove"]).delete()
            return redirect("pharmacy:uom")
        name = " ".join(request.POST.get("name", "").split())
        try:
            parse_packing(name)
        except ValueError as err:
            messages.error(request, str(err))
        else:
            name = name[0].upper() + name[1:]
            row, created = UnitOfMeasure.objects.get_or_create(
                organization=request.organization, name=name[:80]
            )
            if created:
                audit.record(request, "uom.create", row, summary=row.name)
        return redirect("pharmacy:uom")
    return render(
        request,
        "pharmacy/uom.html",
        {
            "saved": scoped(UnitOfMeasure, request),
            "suggestions": catalog.packing_suggestions(request.organization),
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": "Units of measure"},
            ),
        },
    )


@require_POST
@permission_required(MANAGE)
def categories_standard(request):
    added = catalog.ensure_standard_categories(request.organization)
    if added:
        audit.record(
            request, "product_category.standard", request.organization, summary=f"added={added}"
        )
    messages.success(request, f"{added} categories added." if added else "Already in place.")
    return redirect("pharmacy:categories")


@permission_required(MANAGE)
def categories(request):
    """Category list with the tax and prescription defaults new products pick up."""
    rows = list(scoped(ProductCategory, request))
    if request.method == "POST":
        with transaction.atomic():
            for category in rows:
                raw = request.POST.get(f"tax_{category.pk}", "")
                try:
                    tax = Decimal(raw or 0)
                except ArithmeticError:
                    tax = None
                if tax is None or not 0 <= tax <= 100:
                    messages.error(request, f"{category}: tax must be between 0 and 100.")
                    return redirect("pharmacy:categories")
                rx = f"rx_{category.pk}" in request.POST
                if tax != category.default_tax_percent or rx != category.requires_prescription:
                    audit.record(
                        request,
                        "product_category.update",
                        category,
                        changes={"tax": [str(category.default_tax_percent), str(tax)]},
                    )
                    category.default_tax_percent, category.requires_prescription = tax, rx
                    category.save()
        messages.success(request, "Categories saved. Existing products keep their own rates.")
        return redirect("pharmacy:categories")
    return render(
        request,
        "pharmacy/categories.html",
        {
            "categories": rows,
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": "Categories"},
            ),
        },
    )


@require_POST
@permission_required(MANAGE)
def product_barcode(request, pk):
    """Give an existing product a generated barcode."""
    product = get_scoped_or_404(Product, request, pk=pk)
    if product.barcode:
        messages.info(request, "This product already has a barcode.")
    else:
        product.barcode = barcodes.new_barcode(request.organization)
        product.save()
        audit.record(request, "product.update", product, changes={"barcode": ["", product.barcode]})
    return redirect("pharmacy:product-detail", pk=pk)


def _label_layout(request):
    """Which paper the labels print on: an A4 sheet, or one sticker per page (the default)."""
    layout = request.GET.get("layout", "")
    if layout == "sheet":
        return {"layout": layout, "paper": None, "papers": LABEL_PAPERS.values()}
    paper = label_paper(layout)
    return {"layout": paper["key"], "paper": paper, "papers": LABEL_PAPERS.values()}


@require_GET
@permission_required(*VIEW, any_of=True)
def product_labels(request, pk):
    """A sheet of barcode labels for shelves and loose stock."""
    product = get_scoped_or_404(Product, request, pk=pk)
    if not product.barcode:
        messages.error(request, "Give this product a barcode first.")
        return redirect("pharmacy:product-detail", pk=pk)
    try:
        svg = barcodes.code128_svg(product.barcode)
    except ValueError as err:
        messages.error(request, str(err))
        return redirect("pharmacy:product-detail", pk=pk)
    on_hand = stock.on_hand(request.branch, product)
    count = request.GET.get("count", "")
    count = min(int(count), MAX_LABELS) if count.isdigit() and int(count) > 0 else None
    # One label per pack on the shelf unless told otherwise.
    count = count or max(on_hand // product.pack_size, 1)
    return render(
        request,
        "pharmacy/product_labels.html",
        {
            "product": product,
            "labels": [(product, svg)] * count,
            "count": count,
            **_label_layout(request),
            "per_pack": max(on_hand // product.pack_size, 1),
            "per_unit": max(on_hand, 1),
        },
    )


@permission_required(MANAGE)
def barcodes_page(request):
    """Existing products: generate missing barcodes and choose how many labels to print."""
    products = stock.products_with_stock(request.branch, request.organization).filter(
        is_active=True
    )
    missing = products.filter(barcode="")
    if request.method == "POST":
        with transaction.atomic():
            made = 0
            for product in missing:
                product.barcode = barcodes.new_barcode(request.organization)
                product.save()
                made += 1
        audit.record(request, "product.barcodes", request.organization, summary=f"generated={made}")
        messages.success(request, f"{made} barcodes generated.")
        return redirect("pharmacy:barcodes")
    q = request.GET.get("q", "").strip().lower()
    rows = products.order_by("brand_name", "strength")
    if q:
        rows = rows.filter(search_text__contains=q)
    page = paginate(request, rows)
    for product in page:
        product.label_count = max(product.stock // product.pack_size, 0)
    return render(
        request,
        "pharmacy/barcodes.html",
        {
            "page_obj": page,
            "q": q,
            "missing_count": missing.count(),
            "papers": LABEL_PAPERS.values(),
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": "Barcodes"},
            ),
        },
    )


@require_GET
@permission_required(MANAGE)
def barcodes_print(request):
    """A label sheet for several products: count_<id>=how many."""
    wanted = {}
    for key, value in request.GET.items():
        if key.startswith("count_") and key[6:].isdigit() and value.isdigit() and int(value) > 0:
            wanted[int(key[6:])] = int(value)
    labels = []
    for product in scoped(Product, request).filter(pk__in=wanted).exclude(barcode=""):
        try:
            svg = barcodes.code128_svg(product.barcode)
        except ValueError:
            continue
        labels += [(product, svg)] * wanted[product.pk]
    if not labels:
        messages.error(request, "Enter how many labels to print for at least one product.")
        return redirect("pharmacy:barcodes")
    return render(
        request,
        "pharmacy/product_labels.html",
        {"labels": labels[:MAX_LABELS], "sheet": True, **_label_layout(request)},
    )


@permission_required(MANAGE)
def category_create(request):
    name = request.POST.get("name", "").strip()[:80]
    if request.method == "POST" and name:
        cat, created = ProductCategory.objects.get_or_create(
            organization=request.organization, name=name
        )
        if created:
            audit.record(request, "product_category.create", cat)
        messages.success(request, f"Category “{name}” ready.")
    return redirect("pharmacy:product-list")


def _barcode_svg(product):
    if not product.barcode:
        return ""
    try:
        return barcodes.code128_svg(product.barcode)
    except ValueError:  # a manufacturer's code with characters Code 128 cannot draw
        return ""


LEDGER_RELATED = (
    "product",
    "batch",
    "created_by",
    "sale_item__sale",
    "sale_return_item__sale_return",
    "purchase_item__purchase",
    "purchase_return_item__purchase_return",
)


@require_POST
@permission_required(MANAGE)
def product_price(request, pk):
    """Change the sale price, typed for any size the product comes in."""
    product = get_scoped_or_404(Product, request, pk=pk)
    level = request.POST.get("level", product.top_level)
    scope = ProductPriceChange.Scope.SHELF if request.POST.get("shelf") else None
    try:
        typed = Decimal(request.POST.get("price", ""))
        change = pricing.change_price(
            product,
            request=request,
            sale_price=pricing.unit_value(typed, product.units_in(level)),
            scope=scope or ProductPriceChange.Scope.NEW_STOCK,
            reason=request.POST.get("reason", ""),
        )
    except (ArithmeticError, ValueError):
        messages.error(request, "Enter the new price as a number.")
    except stock.StockError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "product.price_change",
            product,
            summary=f"{change.old_price} to {change.new_price} per unit, "
            f"{change.get_scope_display()}",
        )
        messages.success(request, f"New price saved for {product}.")
    return redirect("pharmacy:product-detail", pk=product.pk)


@require_POST
@permission_required(MANAGE)
def batch_price(request, pk):
    """Change the price of one batch, such as clearing stock close to expiry."""
    batch = get_scoped_or_404(InventoryBatch.objects.select_related("product"), request, pk=pk)
    product = batch.product
    try:
        typed = Decimal(request.POST.get("price", ""))
        change = pricing.reprice_batch(
            batch,
            request=request,
            sale_price=pricing.unit_value(typed, product.units_in(product.top_level)),
            reason=request.POST.get("reason", ""),
        )
    except (ArithmeticError, ValueError):
        messages.error(request, "Enter the new price as a number.")
    except stock.StockError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "batch.price_change",
            batch,
            summary=f"{batch.label}: {change.old_price} to {change.new_price} per unit",
        )
        messages.success(request, f"Batch {batch.label} repriced.")
    return redirect("pharmacy:product-detail", pk=product.pk)


def _date_param(request, name):
    try:
        return date.fromisoformat(request.GET.get(name, ""))
    except ValueError:
        return None


@require_GET
@permission_required(*VIEW, any_of=True)
def product_ledger(request, pk):
    """One product's stock account: opening, each movement with a running balance, closing."""
    product = get_scoped_or_404(Product, request, pk=pk)
    today = timezone.localdate()
    start = _date_param(request, "from") or today.replace(day=1)
    end = _date_param(request, "to") or today
    batches = InventoryBatch.objects.filter(branch=request.branch, product=product)
    batch = None
    if request.GET.get("batch", "").isdigit():
        batch = batches.filter(pk=request.GET["batch"]).first()
    statement = stock.product_statement(request.branch, product, start=start, end=end, batch=batch)
    return render(
        request,
        "pharmacy/product_ledger.html",
        {
            "product": product,
            "statement": statement,
            "start": start,
            "end": end,
            "batch": batch,
            "batches": batches.order_by("expiry_date", "pk"),
            "price_changes": product.price_changes.filter(
                created_at__date__gte=start, created_at__date__lte=end
            ).select_related("batch", "created_by"),
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {
                    "label": product.display_name,
                    "url": reverse("pharmacy:product-detail", args=[product.pk]),
                },
                {"label": "Stock ledger"},
            ),
        },
    )


@require_GET
@permission_required(*VIEW, any_of=True)
def product_detail(request, pk):
    product = get_scoped_or_404(
        Product.objects.select_related("category", "medicine"), request, pk=pk
    )
    today = timezone.localdate()
    batches = list(
        InventoryBatch.objects.filter(branch=request.branch, product=product).order_by(
            "expiry_date", "pk"
        )
    )
    next_batch = next(
        (b for b in batches if b.quantity_available > 0 and b.expiry_date >= today), None
    )
    return render(
        request,
        "pharmacy/product_detail.html",
        {
            "product": product,
            "batches": batches,
            "next_batch": next_batch,
            "on_hand": stock.on_hand(request.branch, product),
            "barcode_svg": _barcode_svg(product),
            "label_papers": LABEL_PAPERS.values(),
            "ledger": StockTransaction.objects.filter(
                branch=request.branch, product=product
            ).select_related(*LEDGER_RELATED)[:10],
            "price_changes": product.price_changes.select_related("batch", "created_by")[:5],
            "action_form": BatchActionForm(),
            "can_manage": has_perm(request, MANAGE),
            "near_days": stock.near_expiry_days(request.organization),
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": product.display_name},
            ),
        },
    )


def _receive_page(request, *, rows, errors=(), reference="", opening=False):
    return render(
        request,
        "pharmacy/receive.html",
        {
            "initial_rows": rows,
            "errors": errors,
            "reference": reference,
            "opening": opening,
            "breadcrumbs": _crumbs({"label": "Receive stock"}),
        },
    )


def _shelf_cost(line) -> Decimal:
    """Cost per unit on the shelf: the line's discount and any free units lower it."""
    paid = line["unit_cost"] * line["quantity"] * (100 - line["discount_percent"]) / 100
    return (paid / (line["quantity"] + line["free_quantity"])).quantize(Decimal("0.000001"))


def _receive_rows(request):
    """All rows post together, or none."""
    reference = request.POST.get("reference", "").strip()[:80]
    opening = bool(request.POST.get("opening"))
    try:
        lines = views_purchasing.grid_lines(request)
        if not lines:
            raise stock.StockError("Add at least one row.")
        with transaction.atomic():
            done = []
            for line in lines:
                if line["expiry_date"] is None:
                    raise stock.StockError(f"Enter the expiry date for {line['product']}.")
                done.append(
                    stock.receive(
                        request=request,
                        product=line["product"],
                        batch_number=line["batch_number"],
                        expiry_date=line["expiry_date"],
                        quantity=line["quantity"] + line["free_quantity"],
                        purchase_cost=_shelf_cost(line),
                        sale_price=line["sale_price"],
                        reference=reference,
                        opening=opening,
                    )
                )
    except stock.StockError as err:
        errors = [err.message]
    except views_purchasing.GridError as err:
        errors = [str(err)]
    except (ValueError, ArithmeticError):
        errors = [views_purchasing.BAD_GRID]
    else:
        for txn in done:
            audit.record(
                request,
                f"stock.{txn.kind}",
                txn.batch,
                summary=f"{txn.product.sku} {txn.batch.batch_number} +{txn.quantity}",
            )
        messages.success(request, f"{len(done)} row{'s' if len(done) != 1 else ''} received.")
        return redirect("pharmacy:product-list")
    return _receive_page(
        request,
        rows=views_purchasing.echo_rows(request),
        errors=errors,
        reference=reference,
        opening=opening,
    )


@permission_required(MANAGE)
def receive(request):
    """Stock receipt (design screen 30 / 57): batch, expiry, quantity and cost, many rows."""
    if request.POST.get("rows"):
        return _receive_rows(request)
    if request.method == "GET":
        rows = []
        if request.GET.get("product", "").isdigit():
            product = get_scoped_or_404(Product, request, pk=request.GET["product"])
            rows = [
                {
                    **views_purchasing._product_row(product),
                    "qty": 1,
                    "unit": "pack" if product.has_packs else "unit",
                    "cost": str(
                        (product.purchase_price * product.pack_size).quantize(Decimal("0.01"))
                    ),
                }
            ]
        return _receive_page(request, rows=rows)
    form = ReceiveForm(request.POST or None, request=request)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            txn = stock.receive(
                request=request,
                product=c["product"],
                batch_number=c["batch_number"],
                expiry_date=c["expiry_date"],
                quantity=c["quantity"],
                purchase_cost=c["purchase_cost"],
                sale_price=c["sale_price"],
                reference=c["reference"],
                opening=c["opening"],
            )
        except stock.StockError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                f"stock.{txn.kind}",
                txn.batch,
                summary=f"{txn.product.sku} {txn.batch.batch_number} +{txn.quantity}",
            )
            messages.success(
                request,
                f"Received {txn.quantity} × {txn.product} (batch {txn.batch.batch_number}).",
            )
            if request.POST.get("another"):
                return redirect("pharmacy:receive")
            return redirect("pharmacy:product-detail", pk=txn.product_id)
    return _receive_page(request, rows=[], errors=[e for es in form.errors.values() for e in es])


@permission_required(MANAGE)
def batch_action(request, pk):
    batch = get_scoped_or_404(InventoryBatch.objects.select_related("product"), request, pk=pk)
    form = BatchActionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            if c["action"] == "count":
                txn = stock.adjust_to_count(
                    batch, request=request, counted=c["quantity"], reason=c["reason"]
                )
            elif c["action"] == StockTransaction.Kind.PURCHASE_RETURN:
                txn = stock.purchase_return(
                    batch,
                    request=request,
                    quantity=c["quantity"],
                    reason=c["reason"],
                    reference=c["reference"],
                )
            else:
                txn = stock.write_off(
                    batch, request=request, quantity=c["quantity"], kind=c["action"],
                    reason=c["reason"],
                )  # fmt: skip
        except stock.StockError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                f"stock.{txn.kind}",
                batch,
                summary=f"{batch.batch_number} {txn.quantity:+d}: {c['reason'][:100]}",
            )
            messages.success(request, f"{txn.get_kind_display()} recorded ({txn.quantity:+d}).")
    elif request.method == "POST":
        messages.error(request, "Check the quantity and give a reason.")
    return redirect("pharmacy:product-detail", pk=batch.product_id)


@require_GET
@permission_required(*VIEW, any_of=True)
def ledger(request):
    kind = request.GET.get("kind", "")
    q = request.GET.get("q", "").strip().lower()
    txns = scoped(StockTransaction, request).select_related(*LEDGER_RELATED)
    start, end = _date_param(request, "from"), _date_param(request, "to")
    if kind:
        txns = txns.filter(kind=kind)
    if q:
        txns = txns.filter(product__search_text__contains=q)
    if start:
        txns = txns.filter(created_at__date__gte=start)
    if end:
        txns = txns.filter(created_at__date__lte=end)
    return render(
        request,
        "pharmacy/ledger.html",
        {
            "transactions": txns[:300],
            "kinds": StockTransaction.Kind.choices,
            "kind": kind,
            "q": q,
            "start": start,
            "end": end,
            "mismatches": stock.ledger_mismatches(request.branch),
            "breadcrumbs": _crumbs({"label": "Stock ledger"}),
        },
    )
