"""Daily sale and purchase reports: one row per day, added up from the source documents."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.billing.models import Invoice, Refund
from apps.pharmacy.models import (
    PharmacySale,
    PharmacySaleReturn,
    Purchase,
    PurchaseReturn,
    VendorLedgerEntry,
)

from ..engine import FIN, Column, ReportResult, report, sum_rows
from .financial import collections

ZERO = Decimal("0.00")
PURCHASING = (*FIN, "pharmacy.inventory.manage")


def _by_day(qs, f, field, amount):
    """{local date: (documents, total)} inside the period, across the chosen branches."""
    tz = timezone.get_current_timezone()
    found = (
        qs.filter(
            organization=f.organization,
            branch__in=f.branches,
            **{f"{field}__gte": f.start_dt, f"{field}__lt": f.end_dt},
        )
        .annotate(day=TruncDate(field, tzinfo=tz))
        .values("day")
        .order_by("day")
        .annotate(n=Count("pk"), total=Sum(amount))
    )
    return defaultdict(lambda: (0, ZERO), {r["day"]: (r["n"], r["total"] or ZERO) for r in found})


def _sales(f):
    pharmacy = _by_day(
        PharmacySale.objects.filter(status=PharmacySale.Status.COMPLETED),
        f, "completed_at", "grand_total",
    )  # fmt: skip
    hospital = _by_day(
        Invoice.objects.filter(pharmacy_sale__isnull=True).exclude(
            status__in=(Invoice.Status.DRAFT, Invoice.Status.CANCELLED)
        ),
        f, "issued_at", "grand_total",
    )  # fmt: skip
    returns = _by_day(PharmacySaleReturn.objects.all(), f, "created_at", "amount")
    refunds = _by_day(
        Refund.objects.filter(invoice__pharmacy_sale__isnull=True), f, "created_at", "amount"
    )
    rows = []
    for day in f.days:
        if not any(day in d for d in (pharmacy, hospital, returns, refunds)):
            continue
        back = returns[day][1] + refunds[day][1]
        rows.append(
            {
                "date": day,
                "bills": pharmacy[day][0] + hospital[day][0],
                "pharmacy": pharmacy[day][1],
                "hospital": hospital[day][1],
                "returns": back,
                "net": pharmacy[day][1] + hospital[day][1] - back,
            }
        )
    return rows


def _purchases(f):
    bills = _by_day(
        Purchase.objects.filter(status=Purchase.Status.RECEIVED),
        f, "received_at", "grand_total",
    )  # fmt: skip
    returned = _by_day(PurchaseReturn.objects.all(), f, "created_at", "amount")
    paid = _by_day(
        VendorLedgerEntry.objects.filter(kind=VendorLedgerEntry.Kind.PAYMENT),
        f, "created_at", "amount",
    )  # fmt: skip
    rows = []
    for day in f.days:
        if not any(day in d for d in (bills, returned, paid)):
            continue
        rows.append(
            {
                "date": day,
                "bills": bills[day][0],
                "purchased": bills[day][1],
                "returned": returned[day][1],
                "net": bills[day][1] - returned[day][1],
                "paid": -paid[day][1],  # payments are negative on the supplier ledger
            }
        )
    return rows


def _money_card(label, value):
    return {"label": label, "value": value, "kind": "money"}


@report(
    "daily-sales",
    "Daily sales",
    "daily",
    "Pharmacy sales and hospital bills per day, less returns and refunds.",
    permissions=FIN,
)
def daily_sales(f):
    rows = _sales(f)
    totals = sum_rows(rows, ("bills", "pharmacy", "hospital", "returns", "net"))
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("bills", "Bills", "int"),
            Column("pharmacy", "Pharmacy sales", "money"),
            Column("hospital", "Hospital bills", "money"),
            Column("returns", "Returns and refunds", "money"),
            Column("net", "Net sales", "money"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            _money_card("Pharmacy sales", totals["pharmacy"]),
            _money_card("Hospital bills", totals["hospital"]),
            _money_card("Returns and refunds", totals["returns"]),
            _money_card("Net sales", totals["net"]),
        ],
        chart=[{"label": f"{r['date']:%d %b}", "value": r["net"]} for r in rows if r["net"] > 0]
        or None,
        chart_kind="money",
        chart_title="Net sales per day",
        note="Billed amounts on the day of the sale or bill, paid or not. Hospital bills are "
        "consultations, lab, procedures and ward. For money received, see Daily collection.",
    )


@report(
    "daily-purchases",
    "Daily purchases",
    "daily",
    "Supplier bills received per day, returns to suppliers and money paid to them.",
    permissions=PURCHASING,
)
def daily_purchases(f):
    rows = _purchases(f)
    totals = sum_rows(rows, ("bills", "purchased", "returned", "net", "paid"))
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("bills", "Bills", "int"),
            Column("purchased", "Purchased", "money"),
            Column("returned", "Returned", "money"),
            Column("net", "Net purchases", "money"),
            Column("paid", "Paid to suppliers", "money"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            _money_card("Purchased", totals["purchased"]),
            _money_card("Returned", totals["returned"]),
            _money_card("Net purchases", totals["net"]),
            _money_card("Paid to suppliers", totals["paid"]),
        ],
        chart=[{"label": f"{r['date']:%d %b}", "value": r["net"]} for r in rows if r["net"] > 0]
        or None,
        chart_kind="money",
        chart_title="Net purchases per day",
    )


@report(
    "daily-sale-purchase",
    "Daily sale and purchase",
    "daily",
    "Net sales against net purchases per day, with money received and paid to suppliers.",
    permissions=FIN,
)
def daily_sale_purchase(f):
    sales = {r["date"]: r["net"] for r in _sales(f)}
    bought = {r["date"]: r for r in _purchases(f)}
    received = {r["date"]: r["net"] for r in collections(f)[0]}
    rows = []
    for day in f.days:
        if day not in sales and day not in bought and day not in received:
            continue
        purchase = bought.get(day, {})
        net_purchases = purchase.get("net", ZERO)
        rows.append(
            {
                "date": day,
                "sales": sales.get(day, ZERO),
                "purchases": net_purchases,
                "difference": sales.get(day, ZERO) - net_purchases,
                "received": received.get(day, ZERO),
                "paid": purchase.get("paid", ZERO),
            }
        )
    totals = sum_rows(rows, ("sales", "purchases", "difference", "received", "paid"))
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("sales", "Net sales", "money"),
            Column("purchases", "Net purchases", "money"),
            Column("difference", "Sales less purchases", "money"),
            Column("received", "Money received", "money"),
            Column("paid", "Paid to suppliers", "money"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            _money_card("Net sales", totals["sales"]),
            _money_card("Net purchases", totals["purchases"]),
            _money_card("Sales less purchases", totals["difference"]),
            _money_card("Money received", totals["received"]),
        ],
        note="Sales less purchases is not profit: stock bought today may sell later. "
        "See Profit and loss by date for profit.",
    )
