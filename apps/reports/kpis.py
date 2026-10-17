"""Dashboard KPIs (Phase 21) for the current branch: admin, laboratory and pharmacy.

Each function takes the request (organization + current branch) and returns plain numbers, so
dashboards stay cheap to render and the views decide what each role may see.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.utils import timezone

ZERO = Decimal("0.00")
TREND_DAYS = 14


def _day_bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def admin_kpis(request, *, financial: bool) -> dict:
    from apps.billing.models import Invoice
    from apps.billing.services import day_totals
    from apps.encounters.models import Encounter
    from apps.ipd.models import Admission, Bed
    from apps.lab.models import LabOrder
    from apps.patients.models import Patient
    from apps.pharmacy.models import PharmacySale

    org, branch = request.organization, request.branch
    today = timezone.localdate()
    start, end = _day_bounds(today)
    visits = Encounter.objects.filter(
        organization=org, branch=branch, started_at__gte=start, started_at__lt=end
    )
    beds = Bed.objects.filter(branch=branch, is_active=True, room__is_active=True)
    beds_total = beds.count()
    occupied = beds.filter(status=Bed.Status.OCCUPIED).count()
    k = {
        "patients_today": visits.values("patient").distinct().count(),
        "registered_today": Patient.objects.filter(
            organization=org, registered_branch=branch, created_at__gte=start, created_at__lt=end
        ).count(),
        "opd_today": visits.count(),
        "opd_signed": visits.filter(status=Encounter.Status.COMPLETED).count(),
        "admissions_today": Admission.objects.filter(
            branch=branch, admitted_at__gte=start, admitted_at__lt=end
        )
        .exclude(status=Admission.Status.CANCELLED)
        .count(),
        "inpatients": Admission.objects.filter(
            branch=branch,
            status__in=[Admission.Status.ADMITTED, Admission.Status.DISCHARGE_PLANNED],
        ).count(),
        "lab_today": LabOrder.objects.filter(
            branch=branch, created_at__gte=start, created_at__lt=end
        )
        .exclude(status=LabOrder.Status.CANCELLED)
        .count(),
        "beds_total": beds_total,
        "beds_occupied": occupied,
        "occupancy": round(occupied * 100 / beds_total) if beds_total else None,
    }
    sales = PharmacySale.objects.filter(
        branch=branch,
        status=PharmacySale.Status.COMPLETED,
        completed_at__gte=start,
        completed_at__lt=end,
    ).aggregate(n=Count("id"), total=Sum("grand_total"))
    k["pharmacy_sales_count"] = sales["n"]
    if financial:
        k["revenue_today"] = day_totals(branch, today)["net"]
        k["pharmacy_sales_total"] = sales["total"] or ZERO
        outstanding = Invoice.objects.filter(
            branch=branch, status__in=Invoice.OUTSTANDING_STATUSES
        ).aggregate(n=Count("id"), total=Sum("grand_total"), paid=Sum("amount_paid"))
        k["outstanding_count"] = outstanding["n"]
        k["outstanding_total"] = (outstanding["total"] or ZERO) - (outstanding["paid"] or ZERO)
    k["trend"] = trend(request, financial=financial)
    return k


def trend(request, *, financial: bool, days: int = TREND_DAYS) -> list[dict]:
    """OPD visits (and net collection) per day for the last `days` days, oldest first."""
    from apps.billing.services import net_by_day
    from apps.encounters.models import Encounter

    today = timezone.localdate()
    first = today - timedelta(days=days - 1)
    start, _ = _day_bounds(first)
    _, end = _day_bounds(today)
    counts = {}
    for started in Encounter.objects.filter(
        organization=request.organization,
        branch=request.branch,
        started_at__gte=start,
        started_at__lt=end,
    ).values_list("started_at", flat=True):
        d = timezone.localtime(started).date()
        counts[d] = counts.get(d, 0) + 1
    collections = net_by_day(request.branch, first, today) if financial else {}
    rows = []
    for i in range(days):
        d = first + timedelta(days=i)
        row = {"date": d, "visits": counts.get(d, 0)}
        if financial:
            row["collection"] = collections[d]
        rows.append(row)
    top_v = max((r["visits"] for r in rows), default=0) or 1
    top_c = max((r.get("collection", 0) for r in rows), default=0) or 1
    for r in rows:
        r["visits_pct"] = round(r["visits"] * 100 / top_v)
        if financial:
            r["collection_pct"] = round(max(r["collection"], 0) * 100 / top_c)
    return rows


def lab_kpis(request) -> dict:
    from apps.lab.models import LabOrder, LabOrderItem, LabResult, LabSample

    branch = request.branch
    start, end = _day_bounds(timezone.localdate())
    items = LabOrderItem.objects.filter(order__branch=branch).exclude(
        order__status=LabOrder.Status.CANCELLED
    )
    critical = LabResult.objects.filter(
        item__order__branch=branch,
        is_current=True,
        flag__in=[LabResult.Flag.CRITICAL_LOW, LabResult.Flag.CRITICAL_HIGH],
        critical_acknowledged_at__isnull=True,
    ).exclude(item__order__status=LabOrder.Status.CANCELLED)
    pending_samples = LabSample.objects.filter(
        branch=branch, status=LabSample.Status.PENDING
    ).exclude(order__status=LabOrder.Status.CANCELLED)
    return {
        "pending_samples": pending_samples.count(),
        "awaiting_payment": pending_samples.filter(
            order__status=LabOrder.Status.PAYMENT_PENDING
        ).count(),
        "processing": items.filter(
            status__in=[LabOrderItem.Status.COLLECTED, LabOrderItem.Status.PROCESSING]
        ).count(),
        "verification_pending": items.filter(status=LabOrderItem.Status.RESULT_ENTERED).count(),
        "critical": critical.count(),
        "critical_list": critical.select_related("item__order__patient").order_by("entered_at")[:8],
        "verified_today": LabResult.objects.filter(
            item__order__branch=branch, is_current=True, verified_at__gte=start, verified_at__lt=end
        ).count(),
        "released_today": LabOrder.objects.filter(
            branch=branch, released_at__gte=start, released_at__lt=end
        ).count(),
        "ordered_today": LabOrder.objects.filter(
            branch=branch, created_at__gte=start, created_at__lt=end
        )
        .exclude(status=LabOrder.Status.CANCELLED)
        .count(),
        "oldest_open": items.filter(
            status__in=[
                LabOrderItem.Status.PENDING,
                LabOrderItem.Status.COLLECTED,
                LabOrderItem.Status.PROCESSING,
                LabOrderItem.Status.RESULT_ENTERED,
            ]
        )
        .select_related("order__patient")
        .order_by("order__created_at")[:8],
    }


def pharmacy_kpis(request, *, show_money: bool) -> dict:
    from apps.pharmacy import stock
    from apps.pharmacy.models import InventoryBatch, PharmacySale, PharmacySaleItem, Product

    org, branch = request.organization, request.branch
    today = timezone.localdate()
    start, end = _day_bounds(today)
    sales = PharmacySale.objects.filter(
        branch=branch,
        status=PharmacySale.Status.COMPLETED,
        completed_at__gte=start,
        completed_at__lt=end,
    ).aggregate(n=Count("id"), total=Sum("grand_total"))
    stocked_here = InventoryBatch.objects.filter(branch=branch).values("product_id")
    out_of_stock = (
        stock.products_with_stock(branch, org)
        .filter(is_active=True, pk__in=stocked_here, stock__lte=0)
        .order_by("brand_name")
    )
    low = stock.low_stock(branch, org).filter(stock__gt=0)
    since, _ = _day_bounds(today - timedelta(days=29))
    top = (
        PharmacySaleItem.objects.filter(
            sale__branch=branch,
            sale__status=PharmacySale.Status.COMPLETED,
            sale__completed_at__gte=since,
        )
        .values("product_id", "product__brand_name", "product__strength")
        .annotate(units=Sum("quantity"), revenue=Sum(F("quantity") * F("unit_price")))
        .order_by("-units")[:8]
    )
    k = {
        "sales_count": sales["n"],
        "low_stock": low.count(),
        "low_list": low[:8],
        "near_expiry": stock.near_expiry(branch).count(),
        "expired": stock.expired(branch).count(),
        "expiry_list": stock.near_expiry(branch)[:8],
        "out_of_stock": out_of_stock.count(),
        "out_list": out_of_stock[:8],
        "top_products": list(top),
        "products": Product.objects.filter(organization=org, is_active=True).count(),
    }
    if show_money:
        k["sales_total"] = sales["total"] or ZERO
    return k
