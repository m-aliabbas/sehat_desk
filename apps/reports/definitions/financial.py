"""Financial reports: collections, billed revenue, outstanding, discounts and refunds.

Collections (money in and out) are built on `billing.services.day_totals`, so they agree with
the cashier's day closing: IPD deposits count on the day taken, deposit balances handed back
count as money out on the day returned, and applied-deposit payments are not counted twice.
Billed revenue comes from issued invoice lines (by issue date), which is a different number.
"""

from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import Invoice, InvoiceItem, Refund
from apps.billing.services import day_totals

from ..engine import FIN, Column, ReportResult, chart_from, local_date, pct, report, sum_rows

ZERO = Decimal("0.00")


def collections(f) -> tuple[list[dict], dict]:
    """Per-day totals and per-method totals across the chosen branches."""
    days, methods = [], {}
    for day in f.days:
        row = {
            "date": day,
            "payments": 0,
            "collected": ZERO,
            "deposits": ZERO,
            "refunded": ZERO,
            "net": ZERO,
        }
        for branch in f.branches:
            t = day_totals(branch, day)
            row["payments"] += t["count"]
            row["collected"] += t["collected"]
            row["deposits"] += t["deposits"]["received"]
            row["refunded"] += t["refunded"]
            row["net"] += t["net"]
            for name, m in t["by_method"].items():
                agg = methods.setdefault(
                    name, {"method": name, "count": 0, "collected": ZERO, "refunded": ZERO}
                )
                agg["count"] += m["count"] + m["deposits"]
                agg["collected"] += m["collected"]
                agg["refunded"] += m["refunded"]
        if row["collected"] or row["refunded"]:
            days.append(row)
    for m in methods.values():
        m["net"] = m["collected"] - m["refunded"]
    return days, methods


@report(
    "daily-collection",
    "Daily collection",
    "financial",
    "Money received and refunded per day, including IPD deposits (matches day closing).",
    permissions=FIN,
)
def daily_collection(f):
    rows, _ = collections(f)
    keys = ["payments", "collected", "deposits", "refunded", "net"]
    totals = sum_rows(rows, keys)
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("payments", "Payments", "int"),
            Column("collected", "Collected", "money"),
            Column("deposits", "of which IPD deposits", "money"),
            Column("refunded", "Refunded / returned", "money"),
            Column("net", "Net", "money"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            {"label": "Collected", "value": totals["collected"], "kind": "money"},
            {"label": "Refunded / returned", "value": totals["refunded"], "kind": "money"},
            {"label": "Net collection", "value": totals["net"], "kind": "money"},
        ],
        chart=[{"label": f"{r['date']:%d %b}", "value": r["net"]} for r in rows] or None,
        chart_kind="money",
        chart_title="Net collection per day",
        note="Collected includes inpatient deposits on the day taken. Deposit balances handed "
        "back count as money out on the day returned. Deposits applied to a final bill are not "
        "counted again.",
    )


@report(
    "payment-methods",
    "Payment methods",
    "financial",
    "Collections and refunds split by payment method.",
    permissions=FIN,
)
def payment_methods(f):
    _, methods = collections(f)
    rows = sorted(methods.values(), key=lambda m: -m["net"])
    totals = sum_rows(rows, ["count", "collected", "refunded", "net"])
    for r in rows:
        r["share"] = pct(r["net"], totals["net"])
    return ReportResult(
        columns=[
            Column("method", "Method"),
            Column("count", "Transactions", "int"),
            Column("collected", "Collected", "money"),
            Column("refunded", "Refunded / returned", "money"),
            Column("net", "Net", "money"),
            Column("share", "Share of net", "percent"),
        ],
        rows=rows,
        totals={"method": "Total", **totals},
        chart=chart_from(rows, "method", "net"),
        chart_kind="money",
        chart_title="Net by method",
    )


def billed_items(f):
    """Lines of invoices issued in the period (drafts and cancelled invoices excluded)."""
    return InvoiceItem.objects.filter(
        invoice__organization=f.organization,
        invoice__branch__in=f.branches,
        invoice__issued_at__gte=f.start_dt,
        invoice__issued_at__lt=f.end_dt,
    ).exclude(invoice__status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])


def _issued_invoices(f):
    return Invoice.objects.filter(
        organization=f.organization,
        branch__in=f.branches,
        issued_at__gte=f.start_dt,
        issued_at__lt=f.end_dt,
    ).exclude(status__in=[Invoice.Status.DRAFT, Invoice.Status.CANCELLED])


KIND_LABELS = {
    InvoiceItem.Kind.LAB: "Laboratory",
    InvoiceItem.Kind.PHARMACY: "Pharmacy",
    InvoiceItem.Kind.ROOM: "Inpatient rooms",
}


def _lab_doctors(invoice_ids) -> dict:
    from apps.lab.models import LabOrder

    return dict(
        LabOrder.objects.filter(invoice_id__in=invoice_ids, doctor__isnull=False).values_list(
            "invoice_id", "doctor_id"
        )
    )


def _attributed(f):
    """Each billed line with the department and doctor it is credited to."""
    items = list(
        billed_items(f).select_related(
            "service",
            "invoice__appointment",
            "invoice__admission",
        )
    )
    lab_doctors = _lab_doctors({i.invoice_id for i in items})
    out = []
    for item in items:
        inv = item.invoice
        appt = inv.appointment
        admission = getattr(inv, "admission", None)  # reverse one-to-one; None when absent
        dept_id = (
            (item.service.department_id if item.service_id else None)
            or (appt.department_id if appt else None)
            or (admission.department_id if admission else None)
        )
        doctor_id = (
            (appt.doctor_id if appt else None)
            or (admission.admitting_doctor_id if admission else None)
            or lab_doctors.get(inv.pk)
        )
        out.append((item, dept_id, doctor_id))
    return out


def _revenue_row(name):
    return {"name": name, "lines": 0, "gross": ZERO, "discount": ZERO, "tax": ZERO, "net": ZERO}


def _add(row, item):
    row["lines"] += 1
    row["gross"] += item.gross
    row["discount"] += item.discount
    row["tax"] += item.tax_amount
    row["net"] += item.line_total


REVENUE_COLUMNS = [
    Column("lines", "Lines", "int"),
    Column("gross", "Gross", "money"),
    Column("discount", "Line discounts", "money"),
    Column("tax", "Tax", "money"),
    Column("net", "Billed", "money"),
]


def _revenue_result(f, rows, first_label, chart_title):
    totals = sum_rows(rows, ["lines", "gross", "discount", "tax", "net"])
    inv_disc = sum((i.invoice_discount for i in _issued_invoices(f)), ZERO)
    return ReportResult(
        columns=[Column("name", first_label), *REVENUE_COLUMNS],
        rows=rows,
        totals={"name": "Total", **totals},
        cards=[
            {"label": "Billed (lines)", "value": totals["net"], "kind": "money"},
            {
                "label": "Invoice-level discounts",
                "value": inv_disc,
                "kind": "money",
                "note": "Not split by line",
            },
            {"label": "Net billed", "value": totals["net"] - inv_disc, "kind": "money"},
        ],
        chart=chart_from(rows, "name", "net"),
        chart_kind="money",
        chart_title=chart_title,
        note="Billed revenue from invoices issued in the period (not money collected). "
        "Invoice-level discounts are shown separately because they are not tied to a line.",
    )


@report(
    "revenue-by-department",
    "Revenue by department",
    "financial",
    "Billed revenue per department from issued invoices.",
    permissions=FIN,
    filters=("date", "branch", "department"),
)
def revenue_by_department(f):
    from apps.masterdata.models import Department

    names = dict(Department.objects.filter(organization=f.organization).values_list("id", "name"))
    groups = {}
    for item, dept_id, _ in _attributed(f):
        if f.department is not None and dept_id != f.department.pk:
            continue
        name = names.get(dept_id) or KIND_LABELS.get(item.kind, "Unassigned")
        _add(groups.setdefault(name, _revenue_row(name)), item)
    rows = sorted(groups.values(), key=lambda r: -r["net"])
    return _revenue_result(f, rows, "Department", "Billed by department")


@report(
    "revenue-by-doctor",
    "Revenue by doctor",
    "financial",
    "Billed revenue credited to the consulting, admitting or ordering doctor.",
    permissions=FIN,
    filters=("date", "branch", "doctor"),
)
def revenue_by_doctor(f):
    from apps.masterdata.models import DoctorProfile

    doctors = {
        d.pk: str(d)
        for d in DoctorProfile.objects.filter(organization=f.organization).select_related("user")
    }
    groups = {}
    for item, _, doctor_id in _attributed(f):
        if f.doctor is not None and doctor_id != f.doctor.pk:
            continue
        name = doctors.get(doctor_id, "No doctor (walk-in, pharmacy)")
        _add(groups.setdefault(name, _revenue_row(name)), item)
    rows = sorted(groups.values(), key=lambda r: -r["net"])
    return _revenue_result(f, rows, "Doctor", "Billed by doctor")


@report(
    "outstanding",
    "Outstanding",
    "financial",
    "Issued invoices that still have a balance, oldest first.",
    permissions=FIN,
    filters=("branch",),
)
def outstanding(f):
    today = timezone.localdate()
    rows = []
    for inv in (
        Invoice.objects.filter(
            organization=f.organization,
            branch__in=f.branches,
            status__in=Invoice.OUTSTANDING_STATUSES,
        )
        .select_related("patient", "branch")
        .order_by("issued_at", "pk")
    ):
        issued = local_date(inv.issued_at or inv.created_at)
        rows.append(
            {
                "number": inv.number,
                "issued": issued,
                "bill_to": inv.bill_to,
                "mrn": inv.patient.mrn if inv.patient_id else "",
                "branch": inv.branch.name,
                "total": inv.grand_total,
                "paid": inv.amount_paid,
                "balance": inv.balance_due,
                "age": (today - issued).days,
            }
        )
    totals = sum_rows(rows, ["total", "paid", "balance"])
    buckets = {"0–7 days": ZERO, "8–30 days": ZERO, "31–90 days": ZERO, "Over 90 days": ZERO}
    for r in rows:
        key = (
            "0–7 days"
            if r["age"] <= 7
            else "8–30 days"
            if r["age"] <= 30
            else "31–90 days"
            if r["age"] <= 90
            else "Over 90 days"
        )
        buckets[key] += r["balance"]
    return ReportResult(
        columns=[
            Column("number", "Invoice"),
            Column("issued", "Issued", "date"),
            Column("bill_to", "Bill to"),
            Column("mrn", "MRN"),
            Column("branch", "Branch"),
            Column("total", "Total", "money"),
            Column("paid", "Paid", "money"),
            Column("balance", "Balance", "money"),
            Column("age", "Age (days)", "int"),
        ],
        rows=rows,
        totals={"number": "Total", **totals},
        cards=[
            {
                "label": "Outstanding balance",
                "value": totals["balance"],
                "kind": "money",
                "note": f"{len(rows)} invoice(s)",
            },
            {
                "label": "Over 30 days",
                "value": buckets["31–90 days"] + buckets["Over 90 days"],
                "kind": "money",
            },
        ],
        chart=[{"label": k, "value": v} for k, v in buckets.items() if v] or None,
        chart_kind="money",
        chart_title="Balance by age",
    )


@report(
    "discounts",
    "Discounts",
    "financial",
    "Line and invoice discounts given, with reason and approver.",
    permissions=FIN,
)
def discounts(f):
    invoices = (
        _issued_invoices(f)
        .select_related("patient", "discount_by")
        .prefetch_related("items")
        .order_by("issued_at")
    )
    rows = []
    for inv in invoices:
        if not (inv.line_discount_total or inv.invoice_discount):
            continue
        reasons = [inv.discount_reason] + [
            i.discount_reason for i in inv.items.all() if i.discount and i.discount_reason
        ]
        rows.append(
            {
                "number": inv.number,
                "issued": local_date(inv.issued_at),
                "bill_to": inv.bill_to,
                "subtotal": inv.subtotal,
                "line": inv.line_discount_total,
                "invoice": inv.invoice_discount,
                "total": inv.discount_total,
                "reason": "; ".join(r for r in reasons if r),
                "by": str(inv.discount_by) if inv.discount_by_id else "",
            }
        )
    totals = sum_rows(rows, ["subtotal", "line", "invoice", "total"])
    return ReportResult(
        columns=[
            Column("number", "Invoice"),
            Column("issued", "Issued", "date"),
            Column("bill_to", "Bill to"),
            Column("subtotal", "Gross", "money"),
            Column("line", "Line discounts", "money"),
            Column("invoice", "Invoice discount", "money"),
            Column("total", "Total discount", "money"),
            Column("reason", "Reason"),
            Column("by", "Invoice discount by"),
        ],
        rows=rows,
        totals={"number": "Total", **totals},
        cards=[
            {
                "label": "Discounts given",
                "value": totals["total"],
                "kind": "money",
                "note": f"{len(rows)} invoice(s)",
            },
            {
                "label": "Share of gross",
                "value": pct(totals["total"], totals["subtotal"]),
                "kind": "percent",
                "note": "On discounted invoices",
            },
        ],
    )


@report(
    "refunds",
    "Refunds",
    "financial",
    "Refunds issued in the period, with method, reason and who approved them.",
    permissions=FIN,
)
def refunds(f):
    rows = [
        {
            "number": r.number,
            "date": local_date(r.created_at),
            "invoice": r.invoice.number,
            "bill_to": r.invoice.bill_to,
            "method": r.method.name,
            "amount": r.amount,
            "reason": r.reason,
            "by": str(r.refunded_by),
        }
        for r in Refund.objects.filter(
            organization=f.organization,
            branch__in=f.branches,
            created_at__gte=f.start_dt,
            created_at__lt=f.end_dt,
        )
        .select_related("invoice__patient", "method", "refunded_by")
        .order_by("created_at")
    ]
    total = sum((r["amount"] for r in rows), ZERO)
    by_method = defaultdict(lambda: ZERO)
    for r in rows:
        by_method[r["method"]] += r["amount"]
    return ReportResult(
        columns=[
            Column("number", "Refund"),
            Column("date", "Date", "date"),
            Column("invoice", "Invoice"),
            Column("bill_to", "Bill to"),
            Column("method", "Method"),
            Column("amount", "Amount", "money"),
            Column("reason", "Reason"),
            Column("by", "Approved by"),
        ],
        rows=rows,
        totals={"number": "Total", "amount": total},
        cards=[
            {"label": "Refunded", "value": total, "kind": "money", "note": f"{len(rows)} refund(s)"}
        ],
        chart=[{"label": k, "value": v} for k, v in by_method.items()] or None,
        chart_kind="money",
        chart_title="Refunds by method",
        note="Unused IPD deposit balances handed back are in Daily collection, not here.",
    )
