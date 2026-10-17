"""Inventory screens: dashboard with alerts, products, batches (FEFO), stock receipt,
adjustments/write-offs and the stock ledger. Viewing needs pharmacy.inventory.manage or
pharmacy.sale; every stock change needs pharmacy.inventory.manage (checked in stock.py too)."""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.models import next_number
from apps.core.scoping import get_scoped_or_404, paginate, scoped
from apps.organizations.models import preferences_for
from apps.prescriptions.models import Medicine

from . import stock
from .forms import BatchActionForm, ProductForm, ReceiveForm
from .models import InventoryBatch, Product, ProductCategory, StockTransaction

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
        )
    )
    creating = pk is None
    form = ProductForm(request.POST or None, instance=product, request=request)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            product = form.save(commit=False)
            if not product.sku:
                product.sku = f"P-{next_number(request.organization, 'product'):05d}"
            if form.cleaned_data.get("create_medicine"):
                form_choice = (
                    product.unit if product.unit in Medicine.Form.values else Medicine.Form.OTHER
                )
                product.medicine, _ = Medicine.objects.get_or_create(
                    organization=request.organization,
                    brand_name=product.brand_name,
                    strength=product.strength,
                    form=form_choice,
                    defaults={
                        "generic_name": product.generic_name,
                        "manufacturer": product.manufacturer,
                    },
                )
            product.save()
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
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": "New product" if creating else product.display_name},
            ),
        },
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
            "ledger": StockTransaction.objects.filter(
                branch=request.branch, product=product
            ).select_related("batch", "created_by")[:50],
            "action_form": BatchActionForm(),
            "can_manage": has_perm(request, MANAGE),
            "near_days": stock.near_expiry_days(request.organization),
            "breadcrumbs": _crumbs(
                {"label": "Products", "url": reverse("pharmacy:product-list")},
                {"label": product.display_name},
            ),
        },
    )


@permission_required(MANAGE)
def receive(request):
    """Stock receipt (design screen 30 / 57): batch, expiry, quantity and cost."""
    initial = {}
    if request.GET.get("product", "").isdigit():
        product = get_scoped_or_404(Product, request, pk=request.GET["product"])
        initial = {"product": product.pk, "purchase_cost": product.purchase_price}
    form = ReceiveForm(request.POST or None, initial=initial, request=request)
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
    return render(
        request,
        "pharmacy/receive.html",
        {"form": form, "breadcrumbs": _crumbs({"label": "Receive stock"})},
    )


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
    txns = scoped(StockTransaction, request).select_related("product", "batch", "created_by")
    if kind:
        txns = txns.filter(kind=kind)
    if q:
        txns = txns.filter(product__search_text__contains=q)
    return render(
        request,
        "pharmacy/ledger.html",
        {
            "transactions": txns[:300],
            "kinds": StockTransaction.Kind.choices,
            "kind": kind,
            "q": q,
            "breadcrumbs": _crumbs({"label": "Stock ledger"}),
        },
    )
