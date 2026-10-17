"""Inpatient reports: admissions, discharges, bed occupancy and average length of stay."""

from collections import Counter

from django.db.models import Q
from django.utils import timezone

from apps.ipd.models import Admission, Bed, BedOccupancy, Ward

from ..engine import OPS, Column, ReportResult, chart_from, pct, report, sum_rows

IPD_FILTERS = ("date", "branch", "department", "doctor")


def _admissions(f, **period):
    qs = Admission.objects.filter(organization=f.organization, branch__in=f.branches, **period)
    if f.department is not None:
        qs = qs.filter(department=f.department)
    if f.doctor is not None:
        qs = qs.filter(admitting_doctor=f.doctor)
    return qs.select_related("patient", "department", "admitting_doctor__user", "current_bed")


def _stay_days(admission) -> float:
    end = admission.discharged_at or timezone.now()
    return round((end - admission.admitted_at).total_seconds() / 86400, 1)


@report(
    "ipd-admissions",
    "Admissions",
    "ipd",
    "Patients admitted in the period with department, doctor, bed and status.",
    permissions=OPS,
    filters=IPD_FILTERS,
)
def admissions(f):
    qs = _admissions(f, admitted_at__gte=f.start_dt, admitted_at__lt=f.end_dt).order_by(
        "admitted_at"
    )
    rows = [
        {
            "number": a.number,
            "admitted": timezone.localtime(a.admitted_at),
            "patient": a.patient.full_name,
            "mrn": a.patient.mrn,
            "department": a.department.name,
            "doctor": str(a.admitting_doctor),
            "bed": a.current_bed.code if a.current_bed_id else "",
            "status": a.get_status_display(),
        }
        for a in qs
    ]
    by_dept = Counter(r["department"] for r in rows if r["status"] != "Cancelled")
    return ReportResult(
        columns=[
            Column("number", "Admission"),
            Column("admitted", "Admitted", "datetime"),
            Column("patient", "Patient"),
            Column("mrn", "MRN"),
            Column("department", "Department"),
            Column("doctor", "Admitting doctor"),
            Column("bed", "Current bed"),
            Column("status", "Status"),
        ],
        rows=rows,
        cards=[
            {
                "label": "Admissions",
                "value": sum(by_dept.values()),
                "kind": "int",
                "note": "Cancelled admissions excluded",
            },
            {
                "label": "Still in hospital",
                "kind": "int",
                "value": sum(1 for r in rows if r["status"] in ("Admitted", "Discharge planned")),
            },
        ],
        chart=[{"label": k, "value": v} for k, v in by_dept.most_common(12)] or None,
        chart_title="Admissions by department",
    )


@report(
    "ipd-discharges",
    "Discharges",
    "ipd",
    "Patients discharged in the period with discharge type, condition and stay.",
    permissions=OPS,
    filters=IPD_FILTERS,
)
def discharges(f):
    qs = _admissions(
        f,
        status=Admission.Status.DISCHARGED,
        discharged_at__gte=f.start_dt,
        discharged_at__lt=f.end_dt,
    ).order_by("discharged_at")
    rows = [
        {
            "number": a.number,
            "admitted": timezone.localtime(a.admitted_at),
            "discharged": timezone.localtime(a.discharged_at),
            "stay": _stay_days(a),
            "patient": a.patient.full_name,
            "mrn": a.patient.mrn,
            "department": a.department.name,
            "type": a.get_discharge_type_display() if a.discharge_type else "",
            "condition": a.get_discharge_condition_display() if a.discharge_condition else "",
        }
        for a in qs
    ]
    types = Counter(r["type"] or "Not recorded" for r in rows)
    return ReportResult(
        columns=[
            Column("number", "Admission"),
            Column("admitted", "Admitted", "datetime"),
            Column("discharged", "Discharged", "datetime"),
            Column("stay", "Stay (days)", "days"),
            Column("patient", "Patient"),
            Column("mrn", "MRN"),
            Column("department", "Department"),
            Column("type", "Discharge type"),
            Column("condition", "Condition"),
        ],
        rows=rows,
        cards=[{"label": "Discharges", "value": len(rows), "kind": "int"}],
        chart=[{"label": k, "value": v} for k, v in types.most_common()] or None,
        chart_title="Discharge type",
    )


@report(
    "ipd-occupancy",
    "Bed occupancy",
    "ipd",
    "Beds per ward, occupied now, and bed-days used in the period.",
    permissions=OPS,
    filters=("date", "branch", "department"),
)
def occupancy(f):
    wards = Ward.objects.filter(organization=f.organization, branch__in=f.branches, is_active=True)
    if f.department is not None:
        wards = wards.filter(department=f.department)
    beds = list(
        Bed.objects.filter(room__ward__in=wards, is_active=True, room__is_active=True).values_list(
            "room__ward_id", "status"
        )
    )
    used = Counter()
    for ward_id, start, end in (
        BedOccupancy.objects.filter(bed__room__ward__in=wards, started_at__lt=f.end_dt)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=f.start_dt))
        .values_list("bed__room__ward_id", "started_at", "ended_at")
    ):
        lo, hi = max(start, f.start_dt), min(end or timezone.now(), f.end_dt)
        if hi > lo:
            used[ward_id] += (hi - lo).total_seconds() / 86400
    elapsed = (min(f.end_dt, timezone.now()) - f.start_dt).total_seconds() / 86400
    rows = []
    for ward in wards.order_by("sort_order", "name"):
        statuses = Counter(s for w, s in beds if w == ward.pk)
        total = sum(statuses.values())
        capacity = total * max(elapsed, 0)
        rows.append(
            {
                "ward": ward.name,
                "beds": total,
                "occupied": statuses[Bed.Status.OCCUPIED],
                "available": statuses[Bed.Status.AVAILABLE],
                "other": total - statuses[Bed.Status.OCCUPIED] - statuses[Bed.Status.AVAILABLE],
                "now": pct(statuses[Bed.Status.OCCUPIED], total),
                "bed_days": round(used[ward.pk], 1),
                "capacity": round(capacity, 1),
                "period": pct(used[ward.pk], capacity),
            }
        )
    totals = sum_rows(rows, ["beds", "occupied", "available", "other", "bed_days", "capacity"])
    totals["now"] = pct(totals["occupied"], totals["beds"])
    totals["period"] = pct(totals["bed_days"], totals["capacity"])
    return ReportResult(
        columns=[
            Column("ward", "Ward"),
            Column("beds", "Beds", "int"),
            Column("occupied", "Occupied now", "int"),
            Column("available", "Available now", "int"),
            Column("other", "Cleaning / blocked", "int"),
            Column("now", "Occupancy now", "percent"),
            Column("bed_days", "Bed-days used", "days"),
            Column("capacity", "Bed-days available", "days"),
            Column("period", "Occupancy in period", "percent"),
        ],
        rows=rows,
        totals={"ward": "Total", **totals},
        cards=[
            {
                "label": "Occupancy now",
                "value": totals["now"],
                "kind": "percent",
                "note": f"{totals['occupied']} of {totals['beds']} beds",
            },
            {
                "label": "Occupancy in period",
                "value": totals["period"],
                "kind": "percent",
                "note": f"{totals['bed_days']} bed-days used",
            },
        ],
        chart=chart_from(rows, "ward", "period"),
        chart_kind="percent",
        chart_title="Occupancy in period by ward",
        note="Bed-days available uses today's active beds for the whole period (up to now).",
    )


@report(
    "ipd-average-stay",
    "Average stay",
    "ipd",
    "Average length of stay of patients discharged in the period, by department.",
    permissions=OPS,
    filters=IPD_FILTERS,
)
def average_stay(f):
    groups = {}
    for a in _admissions(
        f,
        status=Admission.Status.DISCHARGED,
        discharged_at__gte=f.start_dt,
        discharged_at__lt=f.end_dt,
    ):
        groups.setdefault(a.department.name, []).append(_stay_days(a))
    rows = [
        {
            "department": name,
            "count": len(stays),
            "avg": round(sum(stays) / len(stays), 1),
            "min": min(stays),
            "max": max(stays),
            "total": round(sum(stays), 1),
        }
        for name, stays in groups.items()
    ]
    rows.sort(key=lambda r: -r["count"])
    count = sum(r["count"] for r in rows)
    total = round(sum(r["total"] for r in rows), 1)
    return ReportResult(
        columns=[
            Column("department", "Department"),
            Column("count", "Discharges", "int"),
            Column("avg", "Average stay (days)", "days"),
            Column("min", "Shortest", "days"),
            Column("max", "Longest", "days"),
            Column("total", "Total bed-days", "days"),
        ],
        rows=rows,
        totals={
            "department": "Total",
            "count": count,
            "total": total,
            "avg": round(total / count, 1) if count else None,
        },
        cards=[
            {
                "label": "Average stay",
                "value": round(total / count, 1) if count else 0,
                "kind": "days",
                "note": f"{count} discharge(s)",
            },
        ],
        chart=chart_from(rows, "department", "avg"),
        chart_kind="days",
        chart_title="Average stay by department (days)",
    )
