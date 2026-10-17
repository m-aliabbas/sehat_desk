"""Laboratory reports: test volume, lab revenue, pending tests and turnaround time (TAT)."""

from django.db.models import Count, Q
from django.utils import timezone

from apps.billing.models import InvoiceItem
from apps.lab.models import LabOrder, LabOrderItem, LabResult

from ..engine import FIN, OPS, Column, ReportResult, chart_from, pct, report, sum_rows
from .financial import _lab_doctors, billed_items

LAB_FILTERS = ("date", "branch", "doctor")
OPEN_ITEM_STATUSES = [
    LabOrderItem.Status.PENDING,
    LabOrderItem.Status.COLLECTED,
    LabOrderItem.Status.PROCESSING,
    LabOrderItem.Status.RESULT_ENTERED,
]


def order_items(f):
    qs = LabOrderItem.objects.filter(
        order__organization=f.organization,
        order__branch__in=f.branches,
        order__created_at__gte=f.start_dt,
        order__created_at__lt=f.end_dt,
    ).exclude(status=LabOrderItem.Status.CANCELLED)
    if f.doctor is not None:
        qs = qs.filter(order__doctor=f.doctor)
    return qs


@report(
    "lab-test-volume",
    "Test volume",
    "lab",
    "Tests ordered per test, with how many are verified or still open.",
    permissions=OPS,
    filters=LAB_FILTERS,
)
def test_volume(f):
    rows = [
        {
            "test": r["test_name"],
            "category": r["test__category__name"],
            "ordered": r["n"],
            "verified": r["verified"],
            "open": r["n"] - r["verified"],
        }
        for r in order_items(f)
        .values("test_id", "test_name", "test__category__name")
        .annotate(n=Count("id"), verified=Count("id", filter=Q(status="verified")))
        .order_by("-n", "test_name")
    ]
    totals = sum_rows(rows, ["ordered", "verified", "open"])
    orders = LabOrder.objects.filter(pk__in=order_items(f).values("order_id")).count()
    return ReportResult(
        columns=[
            Column("test", "Test"),
            Column("category", "Category"),
            Column("ordered", "Ordered", "int"),
            Column("verified", "Verified", "int"),
            Column("open", "Still open", "int"),
        ],
        rows=rows,
        totals={"test": "Total", **totals},
        cards=[
            {"label": "Tests ordered", "value": totals["ordered"], "kind": "int"},
            {"label": "Lab orders", "value": orders, "kind": "int"},
            {
                "label": "Verified",
                "value": totals["verified"],
                "kind": "int",
                "note": f"{pct(totals['verified'], totals['ordered'])}% of tests",
            },
        ],
        chart=chart_from(rows, "test", "ordered", limit=10),
        chart_title="Most ordered tests",
        note="Cancelled tests are excluded. Dates are the order date.",
    )


@report(
    "lab-revenue",
    "Lab revenue",
    "lab",
    "Billed laboratory charges from issued invoices, per test or panel.",
    permissions=FIN,
    filters=LAB_FILTERS,
)
def lab_revenue(f):
    items = list(billed_items(f).filter(kind=InvoiceItem.Kind.LAB))
    if f.doctor is not None:
        doctors = _lab_doctors({i.invoice_id for i in items})
        items = [i for i in items if doctors.get(i.invoice_id) == f.doctor.pk]
    groups = {}
    for i in items:
        g = groups.setdefault(
            i.description, {"name": i.description, "qty": 0, "gross": 0, "discount": 0, "net": 0}
        )
        g["qty"] += i.quantity
        g["gross"] += i.gross
        g["discount"] += i.discount
        g["net"] += i.line_total
    rows = sorted(groups.values(), key=lambda r: -r["net"])
    totals = sum_rows(rows, ["qty", "gross", "discount", "net"])
    return ReportResult(
        columns=[
            Column("name", "Test / panel"),
            Column("qty", "Billed", "int"),
            Column("gross", "Gross", "money"),
            Column("discount", "Discounts", "money"),
            Column("net", "Billed amount", "money"),
        ],
        rows=rows,
        totals={"name": "Total", **totals},
        cards=[{"label": "Lab revenue billed", "value": totals["net"], "kind": "money"}],
        chart=chart_from(rows, "name", "net", limit=10),
        chart_kind="money",
        chart_title="Top lab revenue",
        note="From invoices issued in the period. Cancelled and draft invoices are excluded.",
    )


@report(
    "lab-pending",
    "Pending tests",
    "lab",
    "Tests not yet verified right now, oldest first, with overdue flags.",
    permissions=OPS,
    filters=("branch", "doctor"),
)
def pending_tests(f):
    now = timezone.now()
    qs = (
        LabOrderItem.objects.filter(
            order__organization=f.organization,
            order__branch__in=f.branches,
            status__in=OPEN_ITEM_STATUSES,
        )
        .exclude(order__status=LabOrder.Status.CANCELLED)
        .select_related("order__patient", "test")
        .order_by("order__created_at", "pk")
    )
    if f.doctor is not None:
        qs = qs.filter(order__doctor=f.doctor)
    rows = []
    for item in qs:
        hours = round((now - item.order.created_at).total_seconds() / 3600, 1)
        target = item.test.turnaround_hours or 0
        rows.append(
            {
                "order": item.order.number,
                "ordered": timezone.localtime(item.order.created_at),
                "patient": item.order.patient.full_name,
                "mrn": item.order.patient.mrn,
                "test": item.test_name,
                "priority": item.order.get_priority_display(),
                "status": item.get_status_display(),
                "waiting": hours,
                "overdue": "Yes" if target and hours > target else "",
            }
        )
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return ReportResult(
        columns=[
            Column("order", "Order"),
            Column("ordered", "Ordered", "datetime"),
            Column("patient", "Patient"),
            Column("mrn", "MRN"),
            Column("test", "Test"),
            Column("priority", "Priority"),
            Column("status", "Status"),
            Column("waiting", "Waiting (h)", "hours"),
            Column("overdue", "Past TAT"),
        ],
        rows=rows,
        cards=[
            {"label": "Tests pending", "value": len(rows), "kind": "int"},
            {
                "label": "Past target TAT",
                "value": sum(1 for r in rows if r["overdue"]),
                "kind": "int",
            },
        ],
        chart=[{"label": k, "value": v} for k, v in by_status.items()] or None,
        chart_title="Pending by stage",
        note="Includes orders waiting for payment. Target TAT comes from the test catalogue.",
    )


@report(
    "lab-tat",
    "Turnaround time (TAT)",
    "lab",
    "Hours from order to verified result per test, against the catalogue target.",
    permissions=OPS,
    filters=LAB_FILTERS,
)
def tat(f):
    results = LabResult.objects.filter(
        is_current=True,
        verified_at__isnull=False,
        item__in=order_items(f),
    ).values_list(
        "item__test_name",
        "item__test__turnaround_hours",
        "item__order__created_at",
        "item__sample__collected_at",
        "verified_at",
    )
    groups = {}
    for name, target, ordered, collected, verified in results:
        g = groups.setdefault(name, {"test": name, "target": target or 0, "tat": [], "lab": []})
        g["tat"].append((verified - ordered).total_seconds() / 3600)
        if collected:
            g["lab"].append((verified - collected).total_seconds() / 3600)
    rows = []
    for g in groups.values():
        n = len(g["tat"])
        within = sum(1 for h in g["tat"] if g["target"] and h <= g["target"])
        rows.append(
            {
                "test": g["test"],
                "count": n,
                "avg": round(sum(g["tat"]) / n, 1),
                "max": round(max(g["tat"]), 1),
                "lab_avg": round(sum(g["lab"]) / len(g["lab"]), 1) if g["lab"] else None,
                "target": g["target"] or None,
                "within": pct(within, n) if g["target"] else None,
            }
        )
    rows.sort(key=lambda r: -r["count"])
    all_tat = [h for g in groups.values() for h in g["tat"]]
    return ReportResult(
        columns=[
            Column("test", "Test"),
            Column("count", "Verified", "int"),
            Column("avg", "Average TAT (h)", "hours"),
            Column("max", "Longest (h)", "hours"),
            Column("lab_avg", "Collection → verified (h)", "hours"),
            Column("target", "Target (h)", "hours"),
            Column("within", "Within target", "percent"),
        ],
        rows=rows,
        totals={"test": "Total", "count": len(all_tat)},
        cards=[
            {"label": "Results verified", "value": len(all_tat), "kind": "int"},
            {
                "label": "Average TAT",
                "kind": "hours",
                "value": round(sum(all_tat) / len(all_tat), 1) if all_tat else 0,
            },
        ],
        chart=chart_from(rows, "test", "avg", limit=10),
        chart_kind="hours",
        chart_title="Average TAT by test (hours)",
        note="TAT runs from the order time to verification of the current result version.",
    )
