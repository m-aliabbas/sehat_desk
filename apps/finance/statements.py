"""Profit & loss, the complete cash ledger, and dashboard numbers (Phase 36).

Nothing here writes. Every figure is read from the records that own it:

Income (accrual, by the day the invoice was issued; draft and cancelled invoices excluded)
  each line's amount before tax, less its share of the invoice-level discount, by segment.
  Refunds reduce income on the day they are given, split like the refunded invoice.
Costs
  Pharmacy cost of goods sold  stock-out cost of sales, less returns (StockTransaction)
  Stock write-offs             expired, damaged and adjustment movements at cost
  Doctor commission            CommissionEarning rows (apps.hr), by the line's segment
  Salaries & wages             finalized payslips (salary + bonus − deduction), in the
                               month the payroll is for
  Expenses                     non-void expenses, by category and segment
Cash ledger
  money in: payments (not applied inpatient deposits) and deposits received
  money out: refunds, deposits returned, expenses, staff payouts, supplier payments
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import ZERO, Invoice, InvoiceItem, to_money

from . import segments

INCOME, COST = "income", "cost"


@dataclass
class Row:
    day: date
    segment: str
    group: str  # INCOME or COST
    line: str  # P&L line label
    amount: Decimal


def _bounds(first: date, last: date):
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(first, time.min), tz),
        timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz),
    )


def _local(dt) -> date:
    return timezone.localtime(dt).date()


KIND_LABELS = dict(InvoiceItem.Kind.choices)


def invoice_mix(invoice_ids) -> dict:
    """{invoice_id: {segment: share}} by line totals; shares sum to 1."""
    totals: dict = defaultdict(lambda: defaultdict(lambda: ZERO))
    for inv, kind, amount in InvoiceItem.objects.filter(
        invoice_id__in=set(invoice_ids)
    ).values_list("invoice_id", "kind", "line_total"):
        totals[inv][segments.for_item_kind(kind)] += amount
    out = {}
    for inv in invoice_ids:
        segs = totals.get(inv) or {}
        whole = sum(segs.values(), ZERO)
        out[inv] = (
            {s: v / whole for s, v in segs.items()} if whole > ZERO else {segments.HOSPITAL: 1}
        )
    return out


def split(amount, mix) -> dict:
    """Allocate amount by mix, rounding so the parts add up exactly."""
    parts, given = {}, ZERO
    keys = list(mix)
    for k in keys[:-1]:
        parts[k] = to_money(amount * Decimal(mix[k]))
        given += parts[k]
    parts[keys[-1]] = to_money(amount) - given
    return parts


# --- P&L rows ----------------------------------------------------------------------------------


def income_rows(org, branches, first, last) -> list[Row]:
    start, end = _bounds(first, last)
    items = (
        InvoiceItem.objects.filter(
            invoice__organization=org,
            invoice__branch__in=branches,
            invoice__issued_at__gte=start,
            invoice__issued_at__lt=end,
        )
        .exclude(invoice__status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])
        .values_list(
            "invoice_id",
            "invoice__issued_at",
            "invoice__invoice_discount",
            "kind",
            "line_total",
            "tax_amount",
        )
    )
    by_inv: dict = defaultdict(list)
    for row in items:
        by_inv[row[0]].append(row)
    rows = []
    for lines in by_inv.values():
        whole = sum((r[4] for r in lines), ZERO)
        disc = lines[0][2]
        day = _local(lines[0][1])
        for _, _, _, kind, total, tax in lines:
            share = (disc * total / whole) if whole else ZERO
            amount = to_money(total - tax - share)
            if amount:
                rows.append(
                    Row(
                        day,
                        segments.for_item_kind(kind),
                        INCOME,
                        KIND_LABELS.get(kind, kind),
                        amount,
                    )
                )
    from apps.billing.models import Refund

    refunds = list(
        Refund.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        ).values_list("invoice_id", "created_at", "amount")
    )
    mix = invoice_mix([r[0] for r in refunds])
    for inv, when, amount in refunds:
        for seg, part in split(amount, mix[inv]).items():
            if part:
                rows.append(Row(_local(when), seg, INCOME, "Refunds", -part))
    return rows


def pharmacy_cost_rows(org, branches, first, last) -> list[Row]:
    from apps.pharmacy.models import StockTransaction as T

    start, end = _bounds(first, last)
    K = T.Kind
    label = {
        K.SALE: "Pharmacy cost of goods sold",
        K.SALE_RETURN: "Pharmacy cost of goods sold",
        K.EXPIRED: "Stock write-offs",
        K.DAMAGED: "Stock write-offs",
        K.ADJUSTMENT: "Stock write-offs",
    }
    rows = []
    for kind, when, qty, cost in T.objects.filter(
        organization=org,
        branch__in=branches,
        kind__in=list(label),
        created_at__gte=start,
        created_at__lt=end,
    ).values_list("kind", "created_at", "quantity", "unit_cost"):
        amount = to_money(-qty * cost)  # stock out (negative qty) is a cost
        if amount:
            rows.append(Row(_local(when), segments.PHARMACY, COST, label[kind], amount))
    return rows


def commission_rows(org, branches, first, last) -> list[Row]:
    from apps.hr.models import CommissionEarning

    start, end = _bounds(first, last)
    return [
        Row(_local(when), seg, COST, "Doctor commission", amount)
        for seg, when, amount in CommissionEarning.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        ).values_list("segment", "created_at", "amount")
        if amount
    ]


def salary_rows(org, branches, first, last) -> list[Row]:
    from apps.hr.models import PayrollRun, Payslip

    # A month's payroll counts in full in any period that touches that month.
    lo, hi = (first.year, first.month), (last.year, last.month)
    rows = []
    for slip in Payslip.objects.filter(
        run__organization=org, run__branch__in=branches, run__status=PayrollRun.Status.FINALIZED
    ).select_related("run"):
        if not lo <= (slip.run.year, slip.run.month) <= hi:
            continue
        day = max(first, date(slip.run.year, slip.run.month, 1))
        amount = slip.salary_pay + slip.bonus - slip.deduction
        if amount:
            rows.append(Row(day, segments.HOSPITAL, COST, "Salaries & wages", amount))
    return rows


def expense_rows(org, branches, first, last) -> list[Row]:
    from .models import Expense

    return [
        Row(day, seg, COST, f"Expense · {cat}", amount)
        for day, seg, cat, amount in Expense.objects.filter(
            organization=org, branch__in=branches, date__range=(first, last), voided_at__isnull=True
        ).values_list("date", "segment", "category__name", "amount")
    ]


def pnl_rows(org, branches, first, last) -> list[Row]:
    return (
        income_rows(org, branches, first, last)
        + pharmacy_cost_rows(org, branches, first, last)
        + commission_rows(org, branches, first, last)
        + salary_rows(org, branches, first, last)
        + expense_rows(org, branches, first, last)
    )


@dataclass
class Statement:
    income_lines: dict = field(default_factory=dict)  # label -> {segment: amount}
    cost_lines: dict = field(default_factory=dict)
    income: dict = field(default_factory=segments.empty)
    costs: dict = field(default_factory=segments.empty)

    @property
    def profit(self) -> dict:
        return {k: self.income[k] - self.costs[k] for k in segments.KEYS}

    @property
    def total_income(self) -> Decimal:
        return sum(self.income.values(), ZERO)

    @property
    def total_costs(self) -> Decimal:
        return sum(self.costs.values(), ZERO)

    @property
    def net_profit(self) -> Decimal:
        return self.total_income - self.total_costs

    @property
    def margin(self):
        return round(self.net_profit * 100 / self.total_income, 1) if self.total_income else None


def statement(rows) -> Statement:
    st = Statement()
    for r in rows:
        bucket = st.income_lines if r.group == INCOME else st.cost_lines
        line = bucket.setdefault(r.line, segments.empty())
        line[r.segment] += r.amount
        (st.income if r.group == INCOME else st.costs)[r.segment] += r.amount
    order = list(KIND_LABELS.values()) + ["Refunds"]
    st.income_lines = dict(
        sorted(st.income_lines.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    )
    st.cost_lines = dict(sorted(st.cost_lines.items(), key=lambda kv: -sum(kv[1].values())))
    return st


def monthly_trend(org, branches, months: int = 12, *, today=None) -> list[dict]:
    today = today or timezone.localdate()
    y, m = today.year, today.month
    keys = []
    for _ in range(months):
        keys.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    keys.reverse()
    first = date(*keys[0], 1)
    out = {k: {"income": ZERO, "costs": ZERO} for k in keys}
    for r in pnl_rows(org, branches, first, today):
        k = (r.day.year, r.day.month)
        if k in out:
            out[k]["income" if r.group == INCOME else "costs"] += r.amount
    top = max([max(v["income"], v["costs"]) for v in out.values()] + [ZERO]) or 1
    return [
        {
            "label": date(y, m, 1).strftime("%b %y"),
            **v,
            "profit": v["income"] - v["costs"],
            "income_pct": round(max(v["income"], 0) * 100 / top),
            "costs_pct": round(max(v["costs"], 0) * 100 / top),
        }
        for (y, m), v in out.items()
    ]


# --- Cash ledger -------------------------------------------------------------------------------


@dataclass
class Entry:
    when: datetime
    kind: str
    number: str
    description: str
    method: str
    parts: dict  # segment -> signed amount (+ in, − out)
    branch: str = ""

    @property
    def amount(self) -> Decimal:
        return sum(self.parts.values(), ZERO)


def _staff_segment(user_ids) -> dict:
    from apps.masterdata.models import DoctorProfile

    doctors = set(
        DoctorProfile.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True)
    )
    return {u: (segments.DOCTORS if u in doctors else segments.HOSPITAL) for u in user_ids}


def cash_entries(org, branches, start=None, end=None) -> list[Entry]:
    """Every money movement in [start, end) (datetimes; None = unbounded), oldest first."""
    from apps.billing.models import Payment, Refund
    from apps.billing.services import DEPOSIT_PAYMENT_KEY_PREFIX
    from apps.hr.models import StaffLedgerEntry
    from apps.ipd.models import AdmissionDeposit
    from apps.pharmacy.models import VendorLedgerEntry

    from .models import Expense

    def window(qs, f="created_at"):
        qs = qs.filter(organization=org, branch__in=branches)
        if start is not None:
            qs = qs.filter(**{f"{f}__gte": start})
        if end is not None:
            qs = qs.filter(**{f"{f}__lt": end})
        return qs

    entries: list[Entry] = []
    payments = list(
        window(Payment.objects)
        .exclude(idempotency_key__startswith=DEPOSIT_PAYMENT_KEY_PREFIX)
        .select_related("method", "invoice", "branch")
    )
    refunds = list(window(Refund.objects).select_related("method", "invoice", "branch"))
    mix = invoice_mix([p.invoice_id for p in payments] + [r.invoice_id for r in refunds])
    for p in payments:
        entries.append(
            Entry(
                p.created_at,
                "Payment received",
                p.number,
                f"Invoice {p.invoice.number}",
                p.method.name,
                split(p.amount, mix[p.invoice_id]),
                p.branch.name,
            )
        )
    for r in refunds:
        entries.append(
            Entry(
                r.created_at,
                "Refund",
                r.number,
                f"Invoice {r.invoice.number}",
                r.method.name,
                {k: -v for k, v in split(r.amount, mix[r.invoice_id]).items()},
                r.branch.name,
            )
        )
    for d in window(AdmissionDeposit.objects).select_related("method", "branch"):
        entries.append(
            Entry(
                d.created_at,
                "Inpatient deposit",
                d.number,
                "Deposit received",
                d.method.name,
                {segments.HOSPITAL: d.amount},
                d.branch.name,
            )
        )
    returned = AdmissionDeposit.objects.filter(returned_amount__gt=ZERO)
    for d in window(returned, "returned_at").select_related("method", "branch"):
        entries.append(
            Entry(
                d.returned_at,
                "Deposit returned",
                d.number,
                "Unused deposit returned",
                d.method.name,
                {segments.HOSPITAL: -d.returned_amount},
                d.branch.name,
            )
        )
    # Expenses are dated by the bill (they may be entered a few days late).
    expenses = Expense.objects.filter(organization=org, branch__in=branches, voided_at__isnull=True)
    if start is not None:
        expenses = expenses.filter(date__gte=_local(start))
    if end is not None:
        expenses = expenses.filter(date__lt=_local(end))
    for e in expenses.select_related("method", "category", "branch"):
        when = timezone.make_aware(
            datetime.combine(e.date, timezone.localtime(e.created_at).time())
        )
        entries.append(
            Entry(
                when,
                "Expense",
                e.number,
                f"{e.category}{' · ' + e.payee if e.payee else ''}",
                e.method.name,
                {e.segment: -e.amount},
                e.branch.name,
            )
        )
    payouts = list(
        window(
            StaffLedgerEntry.objects.filter(kind__in=StaffLedgerEntry.PAYOUT_KINDS)
        ).select_related("method", "user", "branch")
    )
    seg_of = _staff_segment({p.user_id for p in payouts})
    for s in payouts:
        entries.append(
            Entry(
                s.created_at,
                s.get_kind_display(),
                f"STF-{s.pk}",
                str(s.user),
                s.method.name if s.method else "",
                {seg_of[s.user_id]: s.amount},
                s.branch.name,
            )
        )
    for v in window(
        VendorLedgerEntry.objects.filter(kind=VendorLedgerEntry.Kind.PAYMENT)
    ).select_related("method", "vendor", "branch"):
        entries.append(
            Entry(
                v.created_at,
                "Supplier payment",
                f"SUP-{v.pk}",
                str(v.vendor),
                v.method.name if v.method else "",
                {segments.PHARMACY: v.amount},
                v.branch.name,
            )
        )
    entries.sort(key=lambda e: e.when)
    return entries


def ledger(org, branches, first, last, *, segment="", method="") -> dict:
    """Cash ledger for a period with opening balance and running balance."""
    start, end = _bounds(first, last)

    def amount_of(e):
        return e.parts.get(segment, ZERO) if segment else e.amount

    def keep(e):
        return (not method or e.method == method) and amount_of(e) != ZERO

    opening = sum((amount_of(e) for e in cash_entries(org, branches, None, start) if keep(e)), ZERO)
    rows, running, money_in, money_out = [], opening, ZERO, ZERO
    for e in cash_entries(org, branches, start, end):
        if not keep(e):
            continue
        a = amount_of(e)
        running += a
        if a > 0:
            money_in += a
        else:
            money_out -= a
        rows.append({"entry": e, "amount": a, "running": running})
    return {
        "opening": opening,
        "closing": running,
        "money_in": money_in,
        "money_out": money_out,
        "rows": rows,
    }


# --- Dashboard ---------------------------------------------------------------------------------


def outstanding(org, branches) -> dict:
    from django.db.models import Count

    agg = Invoice.objects.filter(
        organization=org, branch__in=branches, status__in=Invoice.OUTSTANDING_STATUSES
    ).aggregate(n=Count("id"), total=Sum("grand_total"), paid=Sum("amount_paid"))
    return {"count": agg["n"], "total": (agg["total"] or ZERO) - (agg["paid"] or ZERO)}


def doctor_shares(org, branches, first, last) -> list[dict]:
    """Per doctor: income from lines they are named on, their commission, the clinic's share."""
    from apps.hr.models import CommissionEarning

    start, end = _bounds(first, last)
    income: dict = defaultdict(lambda: ZERO)
    names = {}
    for row in income_rows_by_doctor(org, branches, start, end):
        uid, name, amount = row
        income[uid] += amount
        names[uid] = name
    earned = dict(
        CommissionEarning.objects.filter(
            organization=org, branch__in=branches, created_at__gte=start, created_at__lt=end
        )
        .values("user")
        .annotate(s=Sum("amount"))
        .values_list("user", "s")
    )
    out = []
    for uid in set(income) | set(earned):
        if uid not in names:
            from apps.accounts.models import User

            names[uid] = str(User.objects.get(pk=uid))
        inc, com = income.get(uid, ZERO), earned.get(uid) or ZERO
        out.append(
            {
                "user_id": uid,
                "name": names[uid],
                "income": inc,
                "commission": com,
                "clinic": inc - com,
            }
        )
    return sorted(out, key=lambda r: -r["income"])


def income_rows_by_doctor(org, branches, start, end):
    items = (
        InvoiceItem.objects.filter(
            invoice__organization=org,
            invoice__branch__in=branches,
            invoice__issued_at__gte=start,
            invoice__issued_at__lt=end,
            performed_by__isnull=False,
        )
        .exclude(invoice__status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])
        .select_related("performed_by", "invoice")
    )
    items = list(items)
    totals = dict(
        InvoiceItem.objects.filter(invoice_id__in={i.invoice_id for i in items})
        .values("invoice_id")
        .annotate(s=Sum("line_total"))
        .values_list("invoice_id", "s")
    )
    for i in items:
        whole = totals.get(i.invoice_id) or ZERO
        share = (i.invoice.invoice_discount * i.line_total / whole) if whole else ZERO
        yield i.performed_by_id, str(i.performed_by), to_money(i.line_total - i.tax_amount - share)
