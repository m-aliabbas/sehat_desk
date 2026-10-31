"""Detailed statements: supplier khata, customer ledgers, sales by person and product, doctors'
work, staff pay and the management books. Each is read-only over the source rows."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

from apps.billing.models import Invoice, InvoiceItem, Payment
from apps.finance import statements
from apps.hr.models import PayrollRun, Payslip
from apps.pharmacy import purchasing, stock
from apps.pharmacy.models import (
    InventoryBatch,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleReturnItem,
    Purchase,
    PurchaseReturn,
    VendorLedgerEntry,
)

from ..engine import FIN, Column, ReportResult, chart_from, local_date, report, sum_rows

ZERO = Decimal("0.00")
_DATE = ("date", "branch")
# Inventory managers already see each supplier's account on the supplier page.
SUPPLIER_BOOKS = (*FIN, "pharmacy.inventory.manage")


def _money_cols(*pairs):
    return [Column(k, label, "money") for k, label in pairs]


def _window(qs, f, field="created_at"):
    return qs.filter(**{f"{field}__gte": f.start_dt, f"{field}__lt": f.end_dt})


# --- Suppliers ---------------------------------------------------------------------------------


@report(
    "supplier-statement",
    "Supplier ledger (khata)",
    "suppliers",
    "Every purchase, return and payment with a running balance. Pick one supplier or see all.",
    permissions=SUPPLIER_BOOKS,
    filters=(*_DATE, "vendor"),
)
def supplier_statement(f):
    qs = VendorLedgerEntry.objects.filter(organization=f.organization, branch__in=f.branches)
    if f.vendor is not None:
        qs = qs.filter(vendor=f.vendor)
    opening = qs.filter(created_at__lt=f.start_dt).aggregate(s=Sum("amount"))["s"] or ZERO
    balance = opening
    rows = []
    for e in (
        _window(qs, f)
        .select_related("vendor", "purchase", "purchase_return")
        .order_by("created_at", "pk")
    ):
        balance += e.amount
        doc = e.purchase_return or e.purchase
        rows.append(
            {
                "date": local_date(e.created_at),
                "vendor": e.vendor.name,
                "kind": e.get_kind_display(),
                "number": doc.number if doc else e.reference,
                "note": e.note,
                "owed": e.amount if e.amount > 0 else None,
                "paid": -e.amount if e.amount < 0 else None,
                "balance": balance,
            }
        )
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("vendor", "Supplier"),
            Column("kind", "Entry"),
            Column("number", "Number"),
            Column("note", "Details"),
            *_money_cols(("owed", "Added"), ("paid", "Paid / returned"), ("balance", "Balance")),
        ],
        rows=rows,
        totals={"date": "Total", **sum_rows(rows, ("owed", "paid"))},
        cards=[
            {"label": "Opening balance", "value": opening, "kind": "money"},
            {"label": "Closing balance", "value": balance, "kind": "money"},
        ],
    )


@report(
    "supplier-balances",
    "Supplier unpaid balances",
    "suppliers",
    "What is owed to each supplier right now.",
    permissions=SUPPLIER_BOOKS,
    filters=("branch",),
)
def supplier_balances(f):
    rows = [
        {"vendor": v.name, "phone": v.phone, "balance": v.balance_total}
        for v in purchasing.vendors_with_balance(f.organization).order_by("name")
        if v.balance_total
    ]
    return ReportResult(
        columns=[Column("vendor", "Supplier"), Column("phone", "Phone"),
                 Column("balance", "Owed", "money")],
        rows=rows,
        totals=sum_rows(rows, ("balance",)),
        note="Across all branches, because a supplier has one account with the hospital.",
    )  # fmt: skip


@report(
    "supplier-invoices",
    "Supplier invoices",
    "suppliers",
    "Purchases received, by date and supplier.",
    permissions=SUPPLIER_BOOKS,
    filters=(*_DATE, "vendor"),
)
def supplier_invoices(f):
    qs = _window(
        Purchase.objects.filter(
            organization=f.organization, branch__in=f.branches, status=Purchase.Status.RECEIVED
        ),
        f,
        "received_at",
    ).select_related("vendor")
    if f.vendor is not None:
        qs = qs.filter(vendor=f.vendor)
    rows = [
        {
            "date": local_date(p.received_at),
            "number": p.number,
            "vendor": p.vendor.name,
            "invoice": p.supplier_invoice_number,
            "total": p.grand_total,
        }
        for p in qs.order_by("received_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("number", "Purchase"),
                 Column("vendor", "Supplier"), Column("invoice", "Their invoice"),
                 Column("total", "Total", "money")],
        rows=rows,
        totals=sum_rows(rows, ("total",)),
    )  # fmt: skip


@report(
    "supplier-paid",
    "Supplier payments",
    "suppliers",
    "Money paid to suppliers, by date.",
    permissions=SUPPLIER_BOOKS,
    filters=(*_DATE, "vendor"),
)
def supplier_paid(f):
    qs = _window(
        VendorLedgerEntry.objects.filter(
            organization=f.organization,
            branch__in=f.branches,
            kind=VendorLedgerEntry.Kind.PAYMENT,
        ),
        f,
    ).select_related("vendor", "method")
    if f.vendor is not None:
        qs = qs.filter(vendor=f.vendor)
    rows = [
        {
            "date": local_date(e.created_at),
            "vendor": e.vendor.name,
            "method": str(e.method or ""),
            "reference": e.reference,
            "amount": -e.amount,
        }
        for e in qs.order_by("created_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("vendor", "Supplier"),
                 Column("method", "Paid by"), Column("reference", "Reference"),
                 Column("amount", "Amount", "money")],
        rows=rows,
        totals=sum_rows(rows, ("amount",)),
    )  # fmt: skip


@report(
    "supplier-returns",
    "Returns to suppliers",
    "suppliers",
    "Goods sent back, by date and supplier.",
    permissions=SUPPLIER_BOOKS,
    filters=(*_DATE, "vendor"),
)
def supplier_returns(f):
    qs = _window(
        PurchaseReturn.objects.filter(organization=f.organization, branch__in=f.branches), f
    ).select_related("vendor", "purchase")
    if f.vendor is not None:
        qs = qs.filter(vendor=f.vendor)
    rows = [
        {
            "date": local_date(r.created_at),
            "number": r.number,
            "vendor": r.vendor.name,
            "purchase": r.purchase.number,
            "reason": r.reason,
            "amount": r.amount,
        }
        for r in qs.order_by("created_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("number", "Return"),
                 Column("vendor", "Supplier"), Column("purchase", "Purchase"),
                 Column("reason", "Reason"), Column("amount", "Amount", "money")],
        rows=rows,
        totals=sum_rows(rows, ("amount",)),
    )  # fmt: skip


@report(
    "purchase-tax",
    "Tax paid on purchases",
    "suppliers",
    "GST, further and advance tax on each supplier bill, for the hospital's tax records.",
    permissions=FIN,
    filters=(*_DATE, "vendor"),
)
def purchase_tax(f):
    qs = _window(
        Purchase.objects.filter(
            organization=f.organization, branch__in=f.branches, status=Purchase.Status.RECEIVED
        ),
        f,
        "received_at",
    ).select_related("vendor")
    if f.vendor is not None:
        qs = qs.filter(vendor=f.vendor)
    rows = [
        {
            "date": local_date(p.received_at),
            "number": p.number,
            "invoice": p.supplier_invoice_number,
            "vendor": p.vendor.name,
            "ntn": p.vendor.tax_number,
            "taxable": p.subtotal - p.discount,
            "gst": p.line_tax + p.tax_amount,
            "further": p.further_tax,
            "advance": p.advance_tax,
            "total": p.grand_total,
        }
        for p in qs.order_by("received_at")
    ]
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("number", "Purchase"),
            Column("invoice", "Their invoice"),
            Column("vendor", "Supplier"),
            Column("ntn", "NTN"),
            *_money_cols(
                ("taxable", "Before tax"), ("gst", "GST"), ("further", "Further tax"),
                ("advance", "Advance tax"), ("total", "Bill total"),
            ),
        ],
        rows=rows,
        totals=sum_rows(rows, ("taxable", "gst", "further", "advance", "total")),
    )  # fmt: skip


@report(
    "must-purchase",
    "Products to buy",
    "suppliers",
    "At or below minimum stock, with the supplier they normally come from.",
    permissions=(*FIN, "pharmacy.inventory.manage"),
    filters=("branch", "vendor"),
)
def must_purchase(f):
    rows = []
    for branch in f.branches:
        for p in stock.low_stock(branch, f.organization).select_related("preferred_vendor"):
            if f.vendor is not None and p.preferred_vendor_id != f.vendor.pk:
                continue
            rows.append(
                {
                    "product": p.display_name,
                    "branch": branch.name,
                    "stock": p.stock,
                    "minimum": p.reorder_level,
                    "vendor": p.preferred_vendor.name if p.preferred_vendor_id else "",
                }
            )
    return ReportResult(
        columns=[Column("product", "Product"), Column("branch", "Branch"),
                 Column("stock", "In stock", "int"), Column("minimum", "Minimum", "int"),
                 Column("vendor", "Preferred supplier")],
        rows=rows,
    )  # fmt: skip


# --- Customers ---------------------------------------------------------------------------------


@report(
    "customer-invoices",
    "Customer invoices",
    "customers",
    "All bills by date. Search one patient by MRN or phone for their ledger.",
    permissions=FIN,
    filters=(*_DATE, "patient"),
)
def customer_invoices(f):
    qs = _window(
        Invoice.objects.filter(organization=f.organization, branch__in=f.branches).exclude(
            status__in=(Invoice.Status.DRAFT, Invoice.Status.CANCELLED)
        ),
        f,
        "issued_at",
    ).select_related("patient")
    if f.patient_query:
        if f.patient is None:
            qs = qs.none()
        else:
            qs = qs.filter(patient=f.patient)
    rows = [
        {
            "date": local_date(i.issued_at),
            "number": i.number,
            "customer": i.bill_to,
            "status": i.get_status_display(),
            "total": i.grand_total,
            "paid": i.net_paid,
            "due": i.balance_due,
        }
        for i in qs.order_by("issued_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("number", "Invoice"),
                 Column("customer", "Customer"), Column("status", "Status"),
                 *_money_cols(("total", "Total"), ("paid", "Paid"), ("due", "Due"))],
        rows=rows,
        totals=sum_rows(rows, ("total", "paid", "due")),
    )  # fmt: skip


@report(
    "customer-products",
    "Products by customer",
    "customers",
    "What each customer bought.",
    permissions=FIN,
    filters=(*_DATE, "product", "patient"),
)
def customer_products(f):
    rows = [
        {
            "date": local_date(i.sale.completed_at),
            "customer": i.sale.patient.full_name
            if i.sale.patient_id
            else (i.sale.customer_name or "Walk-in"),
            "product": i.product.display_name,
            "quantity": i.quantity,
            "total": i.line_total,
        }
        for i in _sale_lines(f).order_by("sale__completed_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("customer", "Customer"),
                 Column("product", "Product"), Column("quantity", "Qty", "int"),
                 Column("total", "Total", "money")],
        rows=rows,
        totals=sum_rows(rows, ("quantity", "total")),
    )  # fmt: skip


@report(
    "customer-returns",
    "Customer returns",
    "customers",
    "Medicine brought back, by date, product and salesperson.",
    permissions=FIN,
    filters=(*_DATE, "product", "cashier"),
)
def customer_returns(f):
    qs = PharmacySaleReturnItem.objects.filter(
        sale_return__organization=f.organization,
        sale_return__branch__in=f.branches,
        sale_return__created_at__gte=f.start_dt,
        sale_return__created_at__lt=f.end_dt,
    ).select_related("sale_return__sale", "sale_return__created_by", "sale_item__product")
    if f.product is not None:
        qs = qs.filter(sale_item__product=f.product)
    if f.cashier is not None:
        qs = qs.filter(sale_return__created_by=f.cashier)
    rows = [
        {
            "date": local_date(r.sale_return.created_at),
            "number": r.sale_return.number,
            "sale": r.sale_return.sale.number,
            "product": r.sale_item.product.display_name,
            "quantity": r.quantity,
            "by": str(r.sale_return.created_by),
            "reason": r.sale_return.reason,
            "amount": r.amount,
        }
        for r in qs.order_by("sale_return__created_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("number", "Return"),
                 Column("sale", "Sale"), Column("product", "Product"),
                 Column("quantity", "Qty", "int"), Column("by", "Taken by"),
                 Column("reason", "Reason"), Column("amount", "Refunded", "money")],
        rows=rows,
        totals=sum_rows(rows, ("quantity", "amount")),
    )  # fmt: skip


# --- Sales -------------------------------------------------------------------------------------


def _sale_lines(f):
    qs = PharmacySaleItem.objects.filter(
        sale__organization=f.organization,
        sale__branch__in=f.branches,
        sale__status=PharmacySale.Status.COMPLETED,
        sale__completed_at__gte=f.start_dt,
        sale__completed_at__lt=f.end_dt,
    ).select_related("sale__patient", "sale__cashier", "product", "stock_txn")
    if f.product is not None:
        qs = qs.filter(product=f.product)
    if f.cashier is not None:
        qs = qs.filter(sale__cashier=f.cashier)
    if f.patient_query:
        qs = qs.filter(sale__patient=f.patient) if f.patient else qs.none()
    return qs


@report(
    "sale-detail",
    "Sale detail",
    "pharmacy",
    "Every line sold, by date, product and salesperson.",
    permissions=FIN,
    filters=(*_DATE, "product", "cashier"),
)
def sale_detail(f):
    rows = [
        {
            "date": local_date(i.sale.completed_at),
            "sale": i.sale.number,
            "product": i.product.display_name,
            "quantity": i.quantity,
            "price": i.unit_price,
            "by": str(i.sale.cashier),
            "total": i.line_total,
        }
        for i in _sale_lines(f).order_by("sale__completed_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("sale", "Sale"),
                 Column("product", "Product"), Column("quantity", "Qty", "int"),
                 Column("price", "Price", "money"), Column("by", "Salesperson"),
                 Column("total", "Total", "money")],
        rows=rows,
        totals=sum_rows(rows, ("quantity", "total")),
    )  # fmt: skip


@report(
    "profit-by-product",
    "Profit by product",
    "pharmacy",
    "Sold quantity, sale value, cost and profit before any sale-level discount.",
    permissions=FIN,
    filters=(*_DATE, "product", "cashier"),
)
def profit_by_product(f):
    totals = defaultdict(lambda: {"quantity": 0, "sales": ZERO, "cost": ZERO})
    for i in _sale_lines(f):
        row = totals[i.product.display_name]
        row["quantity"] += i.quantity
        row["sales"] += i.gross - i.discount_amount
        if i.stock_txn_id:
            row["cost"] += i.stock_txn.unit_cost * i.quantity
    rows = sorted(
        (
            {"product": name, **t, "profit": t["sales"] - t["cost"]}
            for name, t in totals.items()
        ),
        key=lambda r: -r["profit"],
    )  # fmt: skip
    return ReportResult(
        columns=[Column("product", "Product"), Column("quantity", "Qty", "int"),
                 *_money_cols(("sales", "Sales"), ("cost", "Cost"), ("profit", "Profit"))],
        rows=rows,
        totals=sum_rows(rows, ("quantity", "sales", "cost", "profit")),
        chart=chart_from(rows, "product", "profit"),
        chart_kind="money",
        chart_title="Most profit",
    )  # fmt: skip


@report(
    "discount-trail",
    "Discount trail",
    "staff",
    "Every pharmacy discount: who gave it, who asked for it and why.",
    permissions=FIN,
    filters=(*_DATE, "product", "cashier"),
)
def discount_trail(f):
    rows = []
    lines = _sale_lines(f).filter(discount_percent__gt=0).select_related(
        "discount_by", "sale__discount_asked_by"
    )  # fmt: skip
    for i in lines:
        rows.append(
            {
                "date": local_date(i.sale.completed_at),
                "sale": i.sale.number,
                "item": i.product.display_name,
                "percent": i.discount_percent,
                "amount": i.discount_amount,
                "given": str(i.discount_by or i.sale.cashier),
                "asked": str(i.sale.discount_asked_by or "Till person"),
                "reason": i.sale.discount_reason,
            }
        )
    sales = PharmacySale.objects.filter(
        organization=f.organization,
        branch__in=f.branches,
        status=PharmacySale.Status.COMPLETED,
        completed_at__gte=f.start_dt,
        completed_at__lt=f.end_dt,
        discount__gt=0,
    ).select_related("discount_by", "discount_asked_by", "cashier")
    if f.cashier is not None:
        sales = sales.filter(cashier=f.cashier)
    if f.product is None:
        for s in sales:
            rows.append(
                {
                    "date": local_date(s.completed_at),
                    "sale": s.number,
                    "item": "Whole sale",
                    "percent": None,
                    "amount": s.discount,
                    "given": str(s.discount_by or s.cashier),
                    "asked": str(s.discount_asked_by or "Till person"),
                    "reason": s.discount_reason,
                }
            )
    rows.sort(key=lambda r: (r["date"], r["sale"]))
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("sale", "Sale"), Column("item", "Item"),
                 Column("percent", "%", "decimal"), Column("amount", "Amount", "money"),
                 Column("given", "Given by"), Column("asked", "Asked by"),
                 Column("reason", "Reason")],
        rows=rows,
        totals=sum_rows(rows, ("amount",)),
    )  # fmt: skip


@report(
    "sale-by-salesperson",
    "Sales by salesperson",
    "staff",
    "Sales, returns and profit per person at the till.",
    permissions=FIN,
    filters=(*_DATE, "cashier"),
)
def sale_by_salesperson(f):
    people = defaultdict(lambda: {"sales": ZERO, "cost": ZERO, "lines": 0})
    for i in _sale_lines(f):
        row = people[str(i.sale.cashier)]
        row["sales"] += i.gross - i.discount_amount
        row["lines"] += 1
        if i.stock_txn_id:
            row["cost"] += i.stock_txn.unit_cost * i.quantity
    rows = [
        {"person": k, "lines": v["lines"], "sales": v["sales"], "cost": v["cost"],
         "profit": v["sales"] - v["cost"]}
        for k, v in sorted(people.items())
    ]  # fmt: skip
    return ReportResult(
        columns=[Column("person", "Salesperson"), Column("lines", "Lines sold", "int"),
                 *_money_cols(("sales", "Sales"), ("cost", "Cost"), ("profit", "Profit"))],
        rows=rows,
        totals=sum_rows(rows, ("lines", "sales", "cost", "profit")),
        chart=chart_from(rows, "person", "sales"),
        chart_kind="money",
        chart_title="Sales",
    )  # fmt: skip


@report(
    "user-collection",
    "Money collected per user",
    "staff",
    "Payments each user took, by method: the end-of-shift count.",
    permissions=FIN,
    filters=(*_DATE, "cashier"),
)
def user_collection(f):
    qs = _window(Payment.objects.filter(organization=f.organization, branch__in=f.branches), f)
    if f.cashier is not None:
        qs = qs.filter(received_by=f.cashier)
    groups = defaultdict(lambda: {"count": 0, "amount": ZERO})
    for p in qs.select_related("received_by", "method"):
        row = groups[(str(p.received_by), p.method.name)]
        row["count"] += 1
        row["amount"] += p.amount
    rows = [
        {"person": person, "method": method, **v} for (person, method), v in sorted(groups.items())
    ]
    return ReportResult(
        columns=[Column("person", "User"), Column("method", "Method"),
                 Column("count", "Payments", "int"), Column("amount", "Amount", "money")],
        rows=rows,
        totals=sum_rows(rows, ("count", "amount")),
    )  # fmt: skip


@report(
    "doctor-work",
    "Doctor work statement",
    "staff",
    "Every fee, procedure and test billed for a doctor, by date.",
    permissions=FIN,
    filters=(*_DATE, "doctor"),
)
def doctor_work(f):
    qs = InvoiceItem.objects.filter(
        invoice__organization=f.organization,
        invoice__branch__in=f.branches,
        invoice__issued_at__gte=f.start_dt,
        invoice__issued_at__lt=f.end_dt,
        performed_by__isnull=False,
    ).exclude(invoice__status__in=(Invoice.Status.DRAFT, Invoice.Status.CANCELLED))
    if f.doctor is not None:
        qs = qs.filter(performed_by=f.doctor.user)
    rows = [
        {
            "date": local_date(i.invoice.issued_at),
            "doctor": str(i.performed_by),
            "invoice": i.invoice.number,
            "kind": i.get_kind_display(),
            "item": i.description,
            "total": i.line_total,
        }
        for i in qs.select_related("invoice", "performed_by").order_by("invoice__issued_at")
    ]
    return ReportResult(
        columns=[Column("date", "Date", "date"), Column("doctor", "Doctor"),
                 Column("invoice", "Invoice"), Column("kind", "Type"), Column("item", "Item"),
                 Column("total", "Total", "money")],
        rows=rows,
        totals=sum_rows(rows, ("total",)),
    )  # fmt: skip


@report(
    "staff-pay",
    "Staff salaries",
    "staff",
    "Payslips of months touching the period. Print each slip from HR → Payroll.",
    permissions=FIN,
    filters=(*_DATE, "cashier"),
)
def staff_pay(f):
    months = {(d.year, d.month) for d in f.days}
    runs = PayrollRun.objects.filter(organization=f.organization, branch__in=f.branches)
    runs = [r.pk for r in runs if (r.year, r.month) in months]
    qs = Payslip.objects.filter(run_id__in=runs).select_related("run", "user")
    if f.cashier is not None:
        qs = qs.filter(user=f.cashier)
    rows = [
        {
            "month": f"{p.run.year}-{p.run.month:02d}",
            "person": str(p.user),
            "present": p.present_days,
            "base": p.base_pay,
            "absence": p.absence_deduction,
            "commission": p.commission,
            "bonus": p.bonus,
            "deduction": p.deduction,
            "net": p.net_pay,
        }
        for p in qs.order_by("run__year", "run__month", "user__first_name")
    ]
    return ReportResult(
        columns=[Column("month", "Month"), Column("person", "Staff"),
                 Column("present", "Days present", "decimal"),
                 *_money_cols(("base", "Base"), ("absence", "Absence"),
                              ("commission", "Commission"), ("bonus", "Bonus"),
                              ("deduction", "Deduction"), ("net", "Net pay"))],
        rows=rows,
        totals=sum_rows(rows, ("base", "absence", "commission", "bonus", "deduction", "net")),
    )  # fmt: skip


# --- Management --------------------------------------------------------------------------------


@report(
    "profit-and-loss",
    "Profit and loss by date",
    "management",
    "Income, cost of medicines sold, commission, salaries and expenses in the period.",
    permissions=FIN,
)
def profit_and_loss(f):
    groups = defaultdict(lambda: ZERO)
    for row in statements.pnl_rows(f.organization, f.branches, f.start, f.end):
        groups[(row.group, row.segment, row.line)] += row.amount
    rows = [
        {"kind": group.capitalize(), "segment": segment, "line": line, "amount": amount}
        for (group, segment, line), amount in sorted(groups.items())
    ]
    income = sum((a for (g, _, _), a in groups.items() if g == statements.INCOME), ZERO)
    spent = sum((a for (g, _, _), a in groups.items() if g != statements.INCOME), ZERO)
    return ReportResult(
        columns=[Column("kind", "Type"), Column("segment", "Area"),
                 Column("line", "Line"), Column("amount", "Amount", "money")],
        rows=rows,
        cards=[
            {"label": "Income", "value": income, "kind": "money"},
            {"label": "Costs", "value": spent, "kind": "money"},
            {"label": "Profit", "value": income - spent, "kind": "money"},
        ],
    )  # fmt: skip


@report(
    "stock-value-by-supplier",
    "Stock value by supplier",
    "management",
    "Value at cost of what is on the shelf, by each product's preferred supplier.",
    permissions=FIN,
    filters=("branch", "vendor"),
)
def stock_value_by_supplier(f):
    qs = InventoryBatch.objects.filter(branch__in=f.branches, quantity_available__gt=0)
    if f.vendor is not None:
        qs = qs.filter(product__preferred_vendor=f.vendor)
    groups = defaultdict(lambda: {"units": 0, "value": ZERO})
    for b in qs.select_related("product__preferred_vendor"):
        vendor = b.product.preferred_vendor
        row = groups[vendor.name if vendor else "No preferred supplier"]
        row["units"] += b.quantity_available
        row["value"] += b.purchase_cost * b.quantity_available
    rows = [{"vendor": k, **v} for k, v in sorted(groups.items())]
    return ReportResult(
        columns=[Column("vendor", "Supplier"), Column("units", "Units", "int"),
                 Column("value", "Value at cost", "money")],
        rows=rows,
        totals=sum_rows(rows, ("units", "value")),
    )  # fmt: skip
