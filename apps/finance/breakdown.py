"""Finance tables for the screen and PDF: the ledger summary and P&L per vendor, product, category,
doctor and staff member. Read-only, like statements.py, and built on the same income rules.

Product, vendor and pharmacy margin use the sale lines and their stock movements. A batch belongs
to the vendor it was received from; opening stock has no vendor. The category table adds up to the
company's net profit, and any difference (such as a refund on a pharmacy sale made without a
return) shows on its own "Not allocated" row.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import ZERO, Invoice, InvoiceItem, to_money

from . import segments, statements

MONEY, TEXT, PERCENT, INT = "money", "text", "percent", "int"
NO_VENDOR = "No supplier (opening stock)"


@dataclass
class Table:
    title: str
    columns: list  # (key, label, kind)
    rows: list = field(default_factory=list)
    totals: dict | None = None
    note: str = ""

    def header(self) -> list:
        return [label for _, label, _ in self.columns]

    def csv_rows(self) -> list:
        out = [[r.get(k, "") for k, _, _ in self.columns] for r in self.rows]
        if self.totals:
            out.append([self.totals.get(k, "") for k, _, _ in self.columns])
        return out

    @property
    def view_columns(self) -> list:
        return [{"key": k, "label": label, "kind": kind} for k, label, kind in self.columns]


def _sum(rows, *keys) -> dict:
    return {k: sum((r.get(k) or ZERO for r in rows), ZERO) for k in keys}


def _margin(income, profit):
    return round(profit * 100 / income, 1) if income else None


def priced_items(org, branches, first, last):
    """Issued invoice lines in the period, each with what it earned: before tax, after its share
    of the invoice discount (the same figure statements.income_rows counts)."""
    start, end = statements._bounds(first, last)
    items = list(
        InvoiceItem.objects.filter(
            invoice__organization=org,
            invoice__branch__in=branches,
            invoice__issued_at__gte=start,
            invoice__issued_at__lt=end,
        )
        .exclude(invoice__status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])
        .select_related("invoice", "service__category")
    )
    whole: dict = defaultdict(lambda: ZERO)
    for i in items:
        whole[i.invoice_id] += i.line_total
    for i in items:
        base = whole[i.invoice_id]
        share = (i.invoice.invoice_discount * i.line_total / base) if base else ZERO
        yield i, to_money(i.line_total - i.tax_amount - share)


def _refunds(org, branches, first, last):
    from apps.billing.models import Refund

    start, end = statements._bounds(first, last)
    return list(
        Refund.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        ).select_related("invoice")
    )


# --- Ledger summary ----------------------------------------------------------------------------


def ledger_summary(org, branches, first, last, ledger_rows, *, segment="") -> dict:
    """Billed, discounts, net income, every cost, net profit and all cash for the ledger page."""
    pnl = statements.pnl_rows(org, branches, first, last)
    st = statements.statement([r for r in pnl if not segment or r.segment == segment])

    kinds: dict = defaultdict(
        lambda: {"billed": ZERO, "discount": ZERO, "tax": ZERO, "refund": ZERO, "collected": ZERO}
    )
    for item, net in priced_items(org, branches, first, last):
        row = kinds[item.kind]
        row["billed"] += item.gross
        row["tax"] += item.tax_amount
        row["discount"] += item.gross - item.tax_amount - net
    refunds = _refunds(org, branches, first, last)
    mix = statements.invoice_mix([r.invoice_id for r in refunds], by_kind=True)
    for r in refunds:
        for kind, part in statements.split(r.amount, mix[r.invoice_id]).items():
            kinds[kind]["refund"] += part

    cash: dict = defaultdict(lambda: {"money_in": ZERO, "money_out": ZERO, "count": 0})
    methods: dict = defaultdict(lambda: {"money_in": ZERO, "money_out": ZERO})
    for row in ledger_rows:
        e, amount = row["entry"], row["amount"]
        side = "money_in" if amount > 0 else "money_out"
        cash[e.group][side] += abs(amount)
        cash[e.group]["count"] += 1
        methods[e.method or "—"][side] += abs(amount)
        if e.group == "Payments received":
            for kind, part in e.by_kind.items():
                kinds[kind]["collected"] += part

    income_rows = []
    for kind, label in InvoiceItem.Kind.choices:
        if kind not in kinds or (segment and segments.for_item_kind(kind) != segment):
            continue
        k = kinds[kind]
        income_rows.append(
            {
                "source": str(label),
                **k,
                "net": k["billed"] - k["discount"] - k["tax"] - k["refund"],
            }
        )
    keys = ("billed", "discount", "tax", "refund", "net", "collected")
    income_totals = {"source": "Total", **_sum(income_rows, *keys)}
    cost_rows = [
        {"cost": label.removeprefix("Expense · "), "amount": sum(v.values(), ZERO)}
        for label, v in st.cost_lines.items()
    ]
    tables = [
        Table(
            "Income by source",
            [
                ("source", "Source", TEXT),
                ("billed", "Billed", MONEY),
                ("discount", "Discount", MONEY),
                ("tax", "Tax", MONEY),
                ("refund", "Refunds", MONEY),
                ("net", "Net income", MONEY),
                ("collected", "Collected", MONEY),
            ],
            income_rows,
            income_totals,
        ),
        Table(
            "Costs, including doctor commission, salaries and every expense category",
            [("cost", "Cost", TEXT), ("amount", "Amount", MONEY)],
            cost_rows,
            {"cost": "Total costs", "amount": st.total_costs},
        ),
        Table(
            "Cash in and out",
            [
                ("group", "Movement", TEXT),
                ("count", "Entries", INT),
                ("money_in", "In", MONEY),
                ("money_out", "Out", MONEY),
            ],
            [{"group": k, **v} for k, v in cash.items()],
            {
                "group": "Total",
                **_sum(cash.values(), "money_in", "money_out"),
                "count": sum(v["count"] for v in cash.values()),
            },
        ),
        Table(
            "By payment method",
            [("method", "Method", TEXT), ("money_in", "In", MONEY), ("money_out", "Out", MONEY)],
            [{"method": k, **v} for k, v in sorted(methods.items())],
            {"method": "Total", **_sum(methods.values(), "money_in", "money_out")},
        ),
    ]
    cards = [
        {"label": "Total billed", "value": income_totals["billed"]},
        {"label": "Discounts given", "value": income_totals["discount"]},
        {"label": "Net income", "value": st.total_income},
        {"label": "Total costs", "value": st.total_costs},
        {"label": "Net profit" if st.net_profit >= 0 else "Net loss", "value": st.net_profit},
        {"label": "Total collected", "value": income_totals["collected"]},
    ]
    if not segment:
        owed = statements.outstanding(org, branches)
        cards.append({"label": f"Still to collect ({owed['count']} bills)", "value": owed["total"]})
    return {"cards": cards, "tables": tables}


def ledger_cards(data) -> list:
    return [
        {"label": "Opening balance", "value": data["opening"]},
        {"label": "Money in", "value": data["money_in"]},
        {"label": "Money out", "value": data["money_out"]},
        {"label": "Closing balance", "value": data["closing"]},
    ]


def ledger_table(data) -> Table:
    """Every entry, oldest first, for the PDF."""
    rows = [
        {
            "when": timezone.localtime(r["entry"].when).strftime("%d %b %Y, %H:%M"),
            "entry": f"{r['entry'].kind} {r['entry'].number}",
            "source": r["entry"].source,
            "detail": r["entry"].description,
            "method": r["entry"].method,
            "branch": r["entry"].branch,
            "money_in": r["amount"] if r["amount"] > 0 else None,
            "money_out": -r["amount"] if r["amount"] < 0 else None,
            "balance": r["running"],
        }
        for r in data["rows"]
    ]
    return Table(
        "All entries",
        [
            ("when", "When", TEXT),
            ("entry", "Entry", TEXT),
            ("source", "Source", TEXT),
            ("detail", "Detail", TEXT),
            ("method", "Method", TEXT),
            ("branch", "Branch", TEXT),
            ("money_in", "In", MONEY),
            ("money_out", "Out", MONEY),
            ("balance", "Balance", MONEY),
        ],
        rows,
        {"when": "Total", "money_in": data["money_in"], "money_out": data["money_out"],
         "balance": data["closing"]},
    )  # fmt: skip


# --- Tables for the other finance PDFs ---------------------------------------------------------


def pnl_cards(st) -> list:
    return [
        {"label": "Total income", "value": st.total_income},
        {"label": "Total costs", "value": st.total_costs},
        {"label": "Net profit" if st.net_profit >= 0 else "Net loss", "value": st.net_profit},
    ]


def pnl_table(st) -> Table:
    def line(label, values):
        return {"line": label, **values, "total": sum(values.values(), ZERO)}

    rows = [line(k, v) for k, v in st.income_lines.items()]
    rows.append(line("Total income", st.income))
    rows += [line(k, v) for k, v in st.cost_lines.items()]
    rows.append(line("Total costs", st.costs))
    return Table(
        "By segment",
        [
            ("line", "Line", TEXT),
            *[(k, str(label), MONEY) for k, label in segments.SEGMENTS],
            ("total", "Total", MONEY),
        ],
        rows,
        line("Net profit", st.profit),
    )


def doctors_ledger_table(rows) -> Table:
    return Table(
        "Doctors",
        [
            ("name", "Doctor", TEXT),
            ("income", "Income", MONEY),
            ("commission", "Commission", MONEY),
            ("clinic", "Clinic share", MONEY),
            ("unsettled", "Not yet on payslip", MONEY),
            ("balance", "Owed now", MONEY),
        ],
        rows,
        {"name": "Total", **_sum(rows, "income", "commission", "clinic")},
    )


def expenses_table(qs) -> Table:
    rows = []
    for e in qs:
        rows.append(
            {
                "date": e.date.strftime("%d %b %Y"),
                "number": e.number,
                "category": e.category.name,
                "segment": e.get_segment_display(),
                "payee": e.payee,
                "method": e.method.name,
                "status": "Void" if e.is_void else "",
                "amount": e.amount,
            }
        )
    live = sum((e.amount for e in qs if not e.is_void), ZERO)
    return Table(
        "All expenses (void entries are not counted in the total)",
        [
            ("date", "Date", TEXT),
            ("number", "Number", TEXT),
            ("category", "Category", TEXT),
            ("segment", "Segment", TEXT),
            ("payee", "Payee", TEXT),
            ("method", "Method", TEXT),
            ("status", "Status", TEXT),
            ("amount", "Amount", MONEY),
        ],
        rows,
        {"date": "Total", "amount": live},
    )


# --- Pharmacy events shared by the product, vendor, category and staff tables ------------------


@dataclass
class PharmacyEvent:
    product: object
    batch_id: int
    cashier_id: int | None = None
    quantity: int = 0
    sales: Decimal = ZERO
    cost: Decimal = ZERO
    writeoff: Decimal = ZERO


def pharmacy_events(org, branches, first, last) -> list[PharmacyEvent]:
    from apps.pharmacy.models import PharmacySaleItem, PharmacySaleReturnItem
    from apps.pharmacy.models import StockTransaction as T

    start, end = statements._bounds(first, last)
    events = []
    items = list(
        PharmacySaleItem.objects.filter(
            sale__organization=org,
            sale__branch__in=branches,
            sale__invoice__issued_at__gte=start,
            sale__invoice__issued_at__lt=end,
        )
        .exclude(sale__invoice__status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])
        .select_related("sale__invoice", "product__category")
    )
    whole: dict = defaultdict(lambda: ZERO)
    for i in items:
        whole[i.sale_id] += i.line_total
    for i in items:
        base = whole[i.sale_id]
        share = (i.sale.invoice.invoice_discount * i.line_total / base) if base else ZERO
        events.append(
            PharmacyEvent(
                i.product,
                i.batch_id,
                i.sale.cashier_id,
                i.quantity,
                sales=to_money(i.line_total - i.tax_amount - share),
            )
        )
    for r in PharmacySaleReturnItem.objects.filter(
        sale_return__organization=org,
        sale_return__branch__in=branches,
        sale_return__created_at__gte=start,
        sale_return__created_at__lt=end,
    ).select_related("sale_return__sale", "sale_item__product__category"):
        events.append(
            PharmacyEvent(
                r.sale_item.product,
                r.sale_item.batch_id,
                r.sale_return.sale.cashier_id,
                -r.quantity,
                sales=-r.amount,
            )
        )
    for t in T.objects.filter(
        organization=org,
        branch__in=branches,
        kind__in=[T.Kind.SALE, T.Kind.SALE_RETURN, T.Kind.EXPIRED, T.Kind.DAMAGED,
                  T.Kind.ADJUSTMENT],
        created_at__gte=start,
        created_at__lt=end,
    ).select_related(
        "product__category", "sale_item__sale", "sale_return_item__sale_item__sale"
    ):  # fmt: skip
        amount = to_money(-t.quantity * t.unit_cost)
        if t.kind == T.Kind.SALE:
            sold = getattr(t, "sale_item", None)
            events.append(
                PharmacyEvent(
                    t.product, t.batch_id, sold.sale.cashier_id if sold else None, cost=amount
                )
            )
        elif t.kind == T.Kind.SALE_RETURN:
            back = getattr(t, "sale_return_item", None)
            events.append(
                PharmacyEvent(
                    t.product,
                    t.batch_id,
                    back.sale_item.sale.cashier_id if back else None,
                    cost=amount,
                )
            )
        else:
            events.append(PharmacyEvent(t.product, t.batch_id, writeoff=amount))
    return events


def _batch_vendors(org, batch_ids) -> dict:
    from apps.pharmacy.models import PurchaseItem

    return dict(
        PurchaseItem.objects.filter(
            purchase__organization=org,
            stock_txn__isnull=False,
            stock_txn__batch_id__in=set(batch_ids),
        ).values_list("stock_txn__batch_id", "purchase__vendor__name")
    )


PHARMACY_KEYS = ("sales", "cost", "writeoff", "profit")


def _add_event(row, e):
    row["sales"] += e.sales
    row["cost"] += e.cost
    row["writeoff"] += e.writeoff
    row["profit"] += e.sales - e.cost - e.writeoff


def _pharmacy_row(**label):
    return {**label, **dict.fromkeys(PHARMACY_KEYS, ZERO)}


def by_product(org, branches, first, last) -> Table:
    rows: dict = {}
    for e in pharmacy_events(org, branches, first, last):
        row = rows.setdefault(
            e.product.pk,
            _pharmacy_row(
                product=e.product.display_name,
                category=e.product.category.name if e.product.category_id else "Uncategorised",
                quantity=0,
            ),
        )
        row["quantity"] += e.quantity
        _add_event(row, e)
    ordered = sorted(rows.values(), key=lambda r: -r["profit"])
    for r in ordered:
        r["margin"] = _margin(r["sales"], r["profit"])
    totals = {"product": "Total", **_sum(ordered, *PHARMACY_KEYS)}
    totals["quantity"] = sum(r["quantity"] for r in ordered)
    totals["margin"] = _margin(totals["sales"], totals["profit"])
    return Table(
        "Profit & loss by product",
        [
            ("product", "Product", TEXT),
            ("category", "Category", TEXT),
            ("quantity", "Units sold", INT),
            ("sales", "Sales", MONEY),
            ("cost", "Cost of goods", MONEY),
            ("writeoff", "Write-offs", MONEY),
            ("profit", "Profit / loss", MONEY),
            ("margin", "Margin %", PERCENT),
        ],
        ordered,
        totals,
        "Sales are after discounts and returns, before tax.",
    )


def by_vendor(org, branches, first, last) -> Table:
    from apps.pharmacy.models import VendorLedgerEntry as V

    events = pharmacy_events(org, branches, first, last)
    vendor_of = _batch_vendors(org, [e.batch_id for e in events])
    keys = ("purchased", "returned", "paid", "owed")
    rows: dict = {}

    def row(name):
        return rows.setdefault(
            name, {"vendor": name, **_pharmacy_row(), **dict.fromkeys(keys, ZERO)}
        )

    for e in events:
        _add_event(row(vendor_of.get(e.batch_id, NO_VENDOR)), e)
    start, end = statements._bounds(first, last)
    ledger = V.objects.filter(organization=org, branch__in=branches)
    for name, amount in (
        ledger.values("vendor__name").annotate(s=Sum("amount")).values_list("vendor__name", "s")
    ):
        row(name)["owed"] = amount
    field_of = {V.Kind.PURCHASE: "purchased", V.Kind.PURCHASE_RETURN: "returned",
                V.Kind.PAYMENT: "paid"}  # fmt: skip
    for name, kind, amount in (
        ledger.filter(kind__in=list(field_of), created_at__gte=start, created_at__lt=end)
        .values("vendor__name", "kind")
        .annotate(s=Sum("amount"))
        .values_list("vendor__name", "kind", "s")
    ):
        row(name)[field_of[kind]] = abs(amount)
    # Vendors with no activity in the period and nothing owed add only noise.
    ordered = sorted(
        (r for r in rows.values() if any(r[k] for k in (*keys, *PHARMACY_KEYS))),
        key=lambda r: r["vendor"],
    )
    return Table(
        "Profit & loss by vendor",
        [
            ("vendor", "Vendor", TEXT),
            ("purchased", "Purchased", MONEY),
            ("returned", "Returned", MONEY),
            ("paid", "Paid", MONEY),
            ("owed", "Owed now", MONEY),
            ("sales", "Sales of their goods", MONEY),
            ("cost", "Cost of goods", MONEY),
            ("writeoff", "Write-offs", MONEY),
            ("profit", "Profit / loss", MONEY),
        ],
        ordered,
        {"vendor": "Total", **_sum(ordered, *keys, *PHARMACY_KEYS)},
        "Profit is on goods sold in this period, by the vendor each batch was received from. "
        "Owed now is the balance today.",
    )


def _category_key(item):
    if item.service_id:
        return "Service", item.service.category.name
    return "Service", str(item.get_kind_display())


def by_category(org, branches, first, last) -> Table:
    from apps.hr.models import CommissionEarning
    from apps.pharmacy.models import PharmacySale

    from .models import Expense

    rows: dict = {}

    def row(kind, name):
        return rows.setdefault(
            (kind, name), {"type": kind, "category": name, "income": ZERO, "cost": ZERO}
        )

    for e in pharmacy_events(org, branches, first, last):
        name = e.product.category.name if e.product.category_id else "Uncategorised"
        r = row("Pharmacy", name)
        r["income"] += e.sales
        r["cost"] += e.cost + e.writeoff
    for item, net in priced_items(org, branches, first, last):
        if item.kind != InvoiceItem.Kind.PHARMACY:
            row(*_category_key(item))["income"] += net
    refunds = _refunds(org, branches, first, last)
    pos_invoices = set(
        PharmacySale.objects.filter(invoice_id__in={r.invoice_id for r in refunds}).values_list(
            "invoice_id", flat=True
        )
    )
    lines: dict = defaultdict(lambda: defaultdict(lambda: ZERO))
    for item in InvoiceItem.objects.filter(
        invoice_id__in={r.invoice_id for r in refunds} - pos_invoices
    ).select_related("service__category"):
        lines[item.invoice_id][_category_key(item)] += item.line_total
    for r in refunds:
        whole = sum(lines[r.invoice_id].values(), ZERO)
        if r.invoice_id in pos_invoices or not whole:
            continue  # pharmacy refunds are counted through their return lines above
        mix = {k: v / whole for k, v in lines[r.invoice_id].items()}
        for key, part in statements.split(r.amount, mix).items():
            row(*key)["income"] -= part
    start, end = statements._bounds(first, last)
    for c in CommissionEarning.objects.filter(
        organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
    ).select_related("invoice_item__service__category"):
        if c.invoice_item.kind != InvoiceItem.Kind.PHARMACY:
            row(*_category_key(c.invoice_item))["cost"] += c.amount
    for name, amount in (
        Expense.objects.filter(
            organization=org, branch__in=branches, date__range=(first, last), voided_at__isnull=True
        )
        .values("category__name")
        .annotate(s=Sum("amount"))
        .values_list("category__name", "s")
    ):
        row("Expense", name)["cost"] += amount
    for r in statements.salary_rows(org, branches, first, last):
        row("Payroll", "Salaries & wages")["cost"] += r.amount
    ordered = sorted(rows.values(), key=lambda r: (r["type"], r["category"]))
    for r in ordered:
        r["profit"] = r["income"] - r["cost"]
    st = statements.statement(statements.pnl_rows(org, branches, first, last))
    other_income = st.total_income - sum((r["income"] for r in ordered), ZERO)
    other_cost = st.total_costs - sum((r["cost"] for r in ordered), ZERO)
    if other_income or other_cost:
        ordered.append(
            {
                "type": "Other",
                "category": "Not allocated",
                "income": other_income,
                "cost": other_cost,
                "profit": other_income - other_cost,
            }
        )
    return Table(
        "Profit & loss by category",
        [
            ("type", "Type", TEXT),
            ("category", "Category", TEXT),
            ("income", "Income", MONEY),
            ("cost", "Costs", MONEY),
            ("profit", "Profit / loss", MONEY),
        ],
        ordered,
        {"type": "Net profit / loss", **_sum(ordered, "income", "cost", "profit")},
        "Adds up to the company's net profit for the period. Costs of a service category are the "
        "doctor commission on its lines.",
    )


# --- People ------------------------------------------------------------------------------------


def _doctor_ids(org, branches, first, last) -> set:
    from apps.hr.models import CommissionEarning
    from apps.masterdata.models import DoctorProfile

    start, end = statements._bounds(first, last)
    ids = set(DoctorProfile.objects.filter(organization=org).values_list("user_id", flat=True))
    ids |= set(
        CommissionEarning.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        ).values_list("user_id", flat=True)
    )
    return ids


def _pay_figures(org, branches, first, last) -> dict:
    """{user_id: {"pay", "paid", "owed"}}: pay earned in the period, paid out, balance today."""
    from apps.hr.models import StaffLedgerEntry as S

    start, end = statements._bounds(first, last)
    out: dict = defaultdict(lambda: {"pay": ZERO, "paid": ZERO, "owed": ZERO})
    for r in statements.salary_rows(org, branches, first, last):
        out[r.user_id]["pay"] += r.amount
    ledger = S.objects.filter(organization=org, branch__in=branches)
    for uid, amount in ledger.values("user").annotate(s=Sum("amount")).values_list("user", "s"):
        out[uid]["owed"] = amount
    for uid, amount in (
        ledger.filter(kind__in=S.PAYOUT_KINDS, created_at__gte=start, created_at__lt=end)
        .values("user")
        .annotate(s=Sum("amount"))
        .values_list("user", "s")
    ):
        out[uid]["paid"] = -amount
    return out


def _names(user_ids) -> dict:
    from apps.accounts.models import User

    return {u.pk: str(u) for u in User.objects.filter(pk__in=user_ids)}


def by_doctor(org, branches, first, last) -> Table:
    from apps.hr.models import CommissionEarning

    start, end = statements._bounds(first, last)
    income: dict = defaultdict(lambda: ZERO)
    refund: dict = defaultdict(lambda: ZERO)
    for item, net in priced_items(org, branches, first, last):
        if item.performed_by_id:
            income[item.performed_by_id] += net
    refunds = _refunds(org, branches, first, last)
    lines: dict = defaultdict(list)
    for inv, uid, total in InvoiceItem.objects.filter(
        invoice_id__in={r.invoice_id for r in refunds}
    ).values_list("invoice_id", "performed_by_id", "line_total"):
        lines[inv].append((uid, total))
    for r in refunds:
        whole = sum((t for _, t in lines[r.invoice_id]), ZERO)
        for uid, total in lines[r.invoice_id]:
            if uid and whole:
                refund[uid] += to_money(r.amount * total / whole)
    commission = dict(
        CommissionEarning.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        )
        .values("user")
        .annotate(s=Sum("amount"))
        .values_list("user", "s")
    )
    pay = _pay_figures(org, branches, first, last)
    doctors = _doctor_ids(org, branches, first, last) | set(income)
    active = [
        u for u in doctors
        if income.get(u) or refund.get(u) or commission.get(u) or any((pay.get(u) or {}).values())
    ]  # fmt: skip
    names = _names(active)
    rows = []
    for uid in active:
        p = pay.get(uid) or {"pay": ZERO, "paid": ZERO, "owed": ZERO}
        got = income.get(uid, ZERO) - refund.get(uid, ZERO)
        rows.append(
            {
                "name": names.get(uid, ""),
                "income": income.get(uid, ZERO),
                "refund": refund.get(uid, ZERO),
                "commission": commission.get(uid) or ZERO,
                "pay": p["pay"],
                "profit": got - (commission.get(uid) or ZERO) - p["pay"],
                "paid": p["paid"],
                "owed": p["owed"],
            }
        )
    rows.sort(key=lambda r: -r["profit"])
    keys = ("income", "refund", "commission", "pay", "profit", "paid", "owed")
    return Table(
        "Profit & loss by doctor",
        [
            ("name", "Doctor", TEXT),
            ("income", "Income", MONEY),
            ("refund", "Refunds", MONEY),
            ("commission", "Commission", MONEY),
            ("pay", "Salary", MONEY),
            ("profit", "Left for hospital", MONEY),
            ("paid", "Paid out", MONEY),
            ("owed", "Owed now", MONEY),
        ],
        rows,
        {"name": "Total", **_sum(rows, *keys)},
        "Income is from invoice lines naming the doctor, after discounts and before tax. "
        "Owed now is the balance today.",
    )


def by_staff(org, branches, first, last) -> Table:
    from apps.billing.models import Payment
    from apps.billing.services import DEPOSIT_PAYMENT_KEY_PREFIX

    start, end = statements._bounds(first, last)
    doctors = _doctor_ids(org, branches, first, last)
    sales: dict = {}
    for e in pharmacy_events(org, branches, first, last):
        if e.cashier_id:
            _add_event(sales.setdefault(e.cashier_id, _pharmacy_row()), e)
    collected = dict(
        Payment.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        )
        .exclude(idempotency_key__startswith=DEPOSIT_PAYMENT_KEY_PREFIX)
        .values("received_by")
        .annotate(s=Sum("amount"))
        .values_list("received_by", "s")
    )
    pay = _pay_figures(org, branches, first, last)
    active = (set(sales) | set(collected) | set(pay)) - doctors - {None}
    names = _names(active)
    rows = []
    for uid in active:
        s = sales.get(uid) or _pharmacy_row()
        p = pay.get(uid) or {"pay": ZERO, "paid": ZERO, "owed": ZERO}
        if not (s["sales"] or collected.get(uid) or any(p.values())):
            continue
        rows.append(
            {
                "name": names.get(uid, ""),
                "collected": collected.get(uid) or ZERO,
                "sales": s["sales"],
                "margin": s["sales"] - s["cost"],
                "pay": p["pay"],
                "profit": s["sales"] - s["cost"] - p["pay"],
                "paid": p["paid"],
                "owed": p["owed"],
            }
        )
    rows.sort(key=lambda r: r["name"])
    keys = ("collected", "sales", "margin", "pay", "profit", "paid", "owed")
    return Table(
        "Profit & loss by staff member",
        [
            ("name", "Staff member", TEXT),
            ("collected", "Cash collected", MONEY),
            ("sales", "Pharmacy sales", MONEY),
            ("margin", "Margin on sales", MONEY),
            ("pay", "Pay", MONEY),
            ("profit", "Margin less pay", MONEY),
            ("paid", "Paid out", MONEY),
            ("owed", "Owed now", MONEY),
        ],
        rows,
        {"name": "Total", **_sum(rows, *keys)},
        "Only pharmacy sales can be credited to a person, so for other staff this shows what they "
        "cost. Cash collected is for information and is not counted as profit.",
    )


BUILDERS = {
    "product": by_product,
    "category": by_category,
    "vendor": by_vendor,
    "doctor": by_doctor,
    "staff": by_staff,
}
CHOICES = [
    ("product", "Product"),
    ("category", "Category"),
    ("vendor", "Vendor"),
    ("doctor", "Doctor"),
    ("staff", "Staff"),
]
