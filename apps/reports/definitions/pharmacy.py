"""Pharmacy reports: sales, gross summary, product movement, stock, expiry and purchases."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.pharmacy import purchasing, stock
from apps.pharmacy.models import (
    InventoryBatch,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleReturn,
    PharmacySaleReturnItem,
    Product,
    Purchase,
    PurchaseReturn,
    StockTransaction,
)

from ..engine import FIN, Column, ReportResult, chart_from, local_date, pct, report, sum_rows

ZERO = Decimal("0.00")
# Stock reports are operational: open to report viewers and to the pharmacy's stock managers.
STOCK = ("reports.view", "pharmacy.inventory.manage")


def _sales(f):
    return PharmacySale.objects.filter(
        organization=f.organization,
        branch__in=f.branches,
        status=PharmacySale.Status.COMPLETED,
        completed_at__gte=f.start_dt,
        completed_at__lt=f.end_dt,
    )


def _returns(f):
    return PharmacySaleReturn.objects.filter(
        organization=f.organization,
        branch__in=f.branches,
        created_at__gte=f.start_dt,
        created_at__lt=f.end_dt,
    )


@report(
    "pharmacy-sales",
    "Pharmacy sales",
    "pharmacy",
    "Completed pharmacy sales with cashier, discount and returns.",
    permissions=FIN,
)
def sales(f):
    rows = []
    for s in (
        _sales(f)
        .select_related("patient", "cashier")
        .annotate(returned=Sum("returns__amount"))
        .order_by("completed_at")
    ):
        rows.append(
            {
                "number": s.number,
                "completed": timezone.localtime(s.completed_at),
                "customer": s.bill_to,
                "cashier": str(s.cashier),
                "rx": "Yes" if s.prescription_id else "",
                "discount": s.discount,
                "total": s.grand_total,
                "returned": s.returned or ZERO,
            }
        )
    totals = sum_rows(rows, ["discount", "total", "returned"])
    return ReportResult(
        columns=[
            Column("number", "Sale"),
            Column("completed", "Completed", "datetime"),
            Column("customer", "Customer"),
            Column("cashier", "Cashier"),
            Column("rx", "Prescription"),
            Column("discount", "Discount", "money"),
            Column("total", "Total", "money"),
            Column("returned", "Returned later", "money"),
        ],
        rows=rows,
        totals={"number": "Total", **totals},
        cards=[
            {
                "label": "Sales",
                "value": totals["total"],
                "kind": "money",
                "note": f"{len(rows)} sale(s)",
            },
            {
                "label": "Average sale",
                "kind": "money",
                "value": (totals["total"] / len(rows)).quantize(Decimal("0.01")) if rows else ZERO,
            },
            {"label": "Discounts", "value": totals["discount"], "kind": "money"},
        ],
        note="“Returned later” is money refunded on returns of that sale, whenever returned.",
    )


@report(
    "pharmacy-gross",
    "Gross sales summary",
    "pharmacy",
    "Per day: gross, discounts, tax, returns, cost of goods and margin.",
    permissions=FIN,
)
def gross_summary(f):
    days = defaultdict(
        lambda: {
            "sales": 0,
            "gross": ZERO,
            "discount": ZERO,
            "tax": ZERO,
            "returns": ZERO,
            "cost": ZERO,
        }
    )
    for s in _sales(f).only("completed_at", "discount"):
        d = days[local_date(s.completed_at)]
        d["sales"] += 1
        d["discount"] += s.discount
    for item in PharmacySaleItem.objects.filter(sale__in=_sales(f)).select_related("sale", "batch"):
        d = days[local_date(item.sale.completed_at)]
        d["gross"] += item.gross
        d["tax"] += item.tax_amount
        d["cost"] += item.batch.purchase_cost * item.quantity
    for line in PharmacySaleReturnItem.objects.filter(sale_return__in=_returns(f)).select_related(
        "sale_return", "sale_item__batch"
    ):
        d = days[local_date(line.sale_return.created_at)]
        d["returns"] += line.amount
        d["cost"] -= line.sale_item.batch.purchase_cost * line.quantity
    rows = []
    for day in f.days:
        if day not in days:
            continue
        d = days[day]
        net = d["gross"] - d["discount"] + d["tax"] - d["returns"]
        margin = net - d["tax"] - d["cost"]
        rows.append(
            {
                "date": day,
                **d,
                "net": net,
                "margin": margin,
                "margin_pct": pct(margin, net - d["tax"]),
            }
        )
    keys = ["sales", "gross", "discount", "tax", "returns", "net", "cost", "margin"]
    totals = sum_rows(rows, keys)
    totals["margin_pct"] = pct(totals["margin"], totals["net"] - totals["tax"])
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("sales", "Sales", "int"),
            Column("gross", "Gross", "money"),
            Column("discount", "Discounts", "money"),
            Column("tax", "Tax", "money"),
            Column("returns", "Returns", "money"),
            Column("net", "Net sales", "money"),
            Column("cost", "Cost of goods", "money"),
            Column("margin", "Gross margin", "money"),
            Column("margin_pct", "Margin %", "percent"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            {"label": "Net sales", "value": totals["net"], "kind": "money"},
            {
                "label": "Gross margin",
                "value": totals["margin"],
                "kind": "money",
                "note": f"{totals['margin_pct']}% of sales before tax",
            },
            {"label": "Returns", "value": totals["returns"], "kind": "money"},
        ],
        chart=[{"label": f"{r['date']:%d %b}", "value": r["net"]} for r in rows] or None,
        chart_kind="money",
        chart_title="Net sales per day",
        note="Cost of goods uses each batch's purchase cost; returned units are taken off on "
        "the day of the return. Margin = net sales − tax − cost of goods.",
    )


MOVEMENT_KINDS = {
    "received": (StockTransaction.Kind.OPENING, StockTransaction.Kind.PURCHASE),
    "sold": (StockTransaction.Kind.SALE,),
    "sale_returns": (StockTransaction.Kind.SALE_RETURN,),
    "purchase_returns": (StockTransaction.Kind.PURCHASE_RETURN,),
    "adjusted": (StockTransaction.Kind.ADJUSTMENT,),
    "written_off": (StockTransaction.Kind.EXPIRED, StockTransaction.Kind.DAMAGED),
}


@report(
    "product-movement",
    "Product movement",
    "pharmacy",
    "Units in and out per product from the stock ledger.",
    permissions=STOCK,
)
def product_movement(f):
    groups = {}
    for r in (
        StockTransaction.objects.filter(
            organization=f.organization,
            branch__in=f.branches,
            created_at__gte=f.start_dt,
            created_at__lt=f.end_dt,
        )
        .values("product_id", "kind")
        .annotate(q=Sum("quantity"))
    ):
        g = groups.setdefault(r["product_id"], {k: 0 for k in MOVEMENT_KINDS})
        for key, kinds in MOVEMENT_KINDS.items():
            if r["kind"] in kinds:
                g[key] += r["q"]
    products = Product.objects.in_bulk(list(groups))
    rows = []
    for pid, g in groups.items():
        rows.append(
            {
                "product": products[pid].display_name,
                "sku": products[pid].sku,
                **g,
                "net": sum(g.values()),
            }
        )
    rows.sort(key=lambda r: r["sold"])  # most sold first (sales are negative)
    keys = [*MOVEMENT_KINDS, "net"]
    for r in rows:
        r["sold"] = -r["sold"]
        r["purchase_returns"] = -r["purchase_returns"]
        r["written_off"] = -r["written_off"]
    return ReportResult(
        columns=[
            Column("product", "Product"),
            Column("sku", "SKU"),
            Column("received", "Received", "int"),
            Column("sold", "Sold", "int"),
            Column("sale_returns", "Customer returns", "int"),
            Column("purchase_returns", "Returned to supplier", "int"),
            Column("adjusted", "Count adjustments (±)", "int"),
            Column("written_off", "Written off", "int"),
            Column("net", "Net change", "int"),
        ],
        rows=rows,
        totals={"product": "Total", **sum_rows(rows, keys)},
        chart=chart_from(rows, "product", "sold", limit=10),
        chart_title="Most sold (units)",
        note="Quantities are base units (tablets, bottles…). Out columns are shown as positive "
        "numbers; net change is in minus out.",
    )


def _batch_stock(f):
    today = timezone.localdate()
    return (
        InventoryBatch.objects.filter(
            organization=f.organization, branch__in=f.branches, quantity_available__gt=0
        )
        .values("product_id")
        .annotate(
            on_hand=Sum("quantity_available", filter=Q(expiry_date__gte=today), default=0),
            expired=Sum("quantity_available", filter=Q(expiry_date__lt=today), default=0),
            cost=Sum(
                F("quantity_available") * F("purchase_cost"), filter=Q(expiry_date__gte=today)
            ),
            retail=Sum(F("quantity_available") * F("sale_price"), filter=Q(expiry_date__gte=today)),
        )
    )


@report(
    "current-stock",
    "Current stock",
    "pharmacy",
    "Saleable units and stock value per product right now.",
    permissions=STOCK,
    filters=("branch",),
)
def current_stock(f):
    stock = {r["product_id"]: r for r in _batch_stock(f)}
    products = Product.objects.filter(organization=f.organization, pk__in=stock).select_related(
        "category"
    )
    rows = []
    for p in products:
        s = stock[p.pk]
        rows.append(
            {
                "product": p.display_name,
                "sku": p.sku,
                "category": p.category.name if p.category_id else "",
                "on_hand": s["on_hand"],
                "expired": s["expired"],
                "reorder": p.reorder_level,
                "cost": s["cost"] or ZERO,
                "retail": s["retail"] or ZERO,
            }
        )
    rows.sort(key=lambda r: r["product"].lower())
    totals = sum_rows(rows, ["on_hand", "expired", "cost", "retail"])
    return ReportResult(
        columns=[
            Column("product", "Product"),
            Column("sku", "SKU"),
            Column("category", "Category"),
            Column("on_hand", "Saleable units", "int"),
            Column("expired", "Expired on shelf", "int"),
            Column("reorder", "Reorder level", "int"),
            Column("cost", "Value at cost", "money"),
            Column("retail", "Value at sale price", "money"),
        ],
        rows=rows,
        totals={"product": "Total", **totals},
        cards=[
            {"label": "Products in stock", "value": len(rows), "kind": "int"},
            {"label": "Stock value (cost)", "value": totals["cost"], "kind": "money"},
            {"label": "Stock value (sale price)", "value": totals["retail"], "kind": "money"},
        ],
        note="Values count saleable (not expired) stock only.",
    )


@report(
    "low-stock",
    "Low stock",
    "pharmacy",
    "Active products at or below their reorder level, including ones with no stock.",
    permissions=STOCK,
    filters=("branch",),
)
def low_stock(f):
    today = timezone.localdate()
    products = (
        Product.objects.filter(organization=f.organization, is_active=True, reorder_level__gt=0)
        .annotate(
            stock=Sum(
                "batches__quantity_available",
                filter=Q(batches__branch__in=f.branches, batches__expiry_date__gte=today),
                default=0,
            )
        )
        .filter(stock__lte=F("reorder_level"))
        .order_by("stock", "brand_name")
    )
    rows = [
        {
            "product": p.display_name,
            "sku": p.sku,
            "on_hand": p.stock,
            "reorder": p.reorder_level,
            "short": p.reorder_level - p.stock,
            "status": "Out of stock" if p.stock <= 0 else "Low",
        }
        for p in products
    ]
    return ReportResult(
        columns=[
            Column("product", "Product"),
            Column("sku", "SKU"),
            Column("on_hand", "Saleable units", "int"),
            Column("reorder", "Reorder level", "int"),
            Column("short", "Below level by", "int"),
            Column("status", "Status"),
        ],
        rows=rows,
        cards=[
            {
                "label": "Low stock",
                "value": sum(1 for r in rows if r["on_hand"] > 0),
                "kind": "int",
            },
            {
                "label": "Out of stock",
                "value": sum(1 for r in rows if r["on_hand"] <= 0),
                "kind": "int",
            },
        ],
        note="With several branches selected, stock is added across them.",
    )


@report(
    "expiry",
    "Expiry",
    "pharmacy",
    "Batches already expired or expiring within the expiry alert window (Settings → "
    "Preferences, default 90 days).",
    permissions=STOCK,
    filters=("branch",),
)
def expiry(f):
    today = timezone.localdate()
    batches = (
        InventoryBatch.objects.filter(
            organization=f.organization,
            branch__in=f.branches,
            quantity_available__gt=0,
            expiry_date__lte=today + timedelta(days=stock.near_expiry_days(f.organization)),
        )
        .select_related("product", "branch")
        .order_by("expiry_date")
    )
    rows = [
        {
            "product": b.product.display_name,
            "batch": b.batch_number,
            "branch": b.branch.name,
            "expiry": b.expiry_date,
            "days": (b.expiry_date - today).days,
            "qty": b.quantity_available,
            "cost": b.purchase_cost * b.quantity_available,
            "status": "Expired" if b.expiry_date < today else "Near expiry",
        }
        for b in batches
    ]
    expired = [r for r in rows if r["status"] == "Expired"]
    return ReportResult(
        columns=[
            Column("product", "Product"),
            Column("batch", "Batch"),
            Column("branch", "Branch"),
            Column("expiry", "Expiry", "date"),
            Column("days", "Days left", "int"),
            Column("qty", "Units", "int"),
            Column("cost", "Value at cost", "money"),
            Column("status", "Status"),
        ],
        rows=rows,
        totals={"product": "Total", **sum_rows(rows, ["qty", "cost"])},
        cards=[
            {
                "label": "Expired batches",
                "value": len(expired),
                "kind": "int",
                "note": "Write off from Inventory",
            },
            {
                "label": "Value at risk",
                "value": sum((r["cost"] for r in rows), ZERO),
                "kind": "money",
            },
        ],
    )


@report(
    "purchases",
    "Purchases",
    "pharmacy",
    "Goods received from suppliers and returns to them.",
    permissions=("reports.financial.view", "pharmacy.inventory.manage"),
)
def purchases(f):
    data = purchasing.report(
        Purchase.objects.filter(organization=f.organization, branch__in=f.branches),
        PurchaseReturn.objects.filter(organization=f.organization, branch__in=f.branches),
        f.start,
        f.end,
    )
    rows = [
        {
            "date": local_date(p.received_at),
            "number": p.number,
            "supplier": p.vendor.name,
            "invoice": p.supplier_invoice_number,
            "subtotal": p.subtotal,
            "discount": p.discount,
            "tax": p.tax_amount,
            "total": p.grand_total,
        }
        for p in data["purchases"].order_by("received_at")
    ] + [
        {
            "date": local_date(r.created_at),
            "number": r.number,
            "supplier": r.vendor.name,
            "invoice": r.reference,
            "total": -r.amount,
        }
        for r in data["returns"]
    ]
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("number", "Purchase / return"),
            Column("supplier", "Supplier"),
            Column("invoice", "Supplier invoice"),
            Column("subtotal", "Subtotal", "money"),
            Column("discount", "Discount", "money"),
            Column("tax", "Tax", "money"),
            Column("total", "Total", "money"),
        ],
        rows=rows,
        totals={"date": "Total", "total": data["net"]},
        cards=[
            {"label": "Purchased", "value": data["purchased"], "kind": "money"},
            {"label": "Returned", "value": data["returned"], "kind": "money"},
            {"label": "Net purchases", "value": data["net"], "kind": "money"},
        ],
        chart=chart_from(data["by_vendor"], "name", "net"),
        chart_kind="money",
        chart_title="Net by supplier",
    )
