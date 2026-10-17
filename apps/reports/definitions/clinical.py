"""Clinical reports: registrations, returning patients, OPD visits and diagnoses."""

from collections import Counter, defaultdict

from django.db.models import Count, Max, Min, Q

from apps.encounters.models import Encounter, EncounterDiagnosis
from apps.masterdata.models import Department, DoctorProfile
from apps.patients.models import Patient

from ..engine import OPS, Column, ReportResult, chart_from, local_date, pct, report, sum_rows

VISIT_FILTERS = ("date", "branch", "department", "doctor")


def encounters(f):
    """OPD visits (consultations) started in the period, in the chosen branches."""
    qs = Encounter.objects.filter(
        organization=f.organization,
        branch__in=f.branches,
        started_at__gte=f.start_dt,
        started_at__lt=f.end_dt,
    )
    if f.department is not None:
        qs = qs.filter(department=f.department)
    if f.doctor is not None:
        qs = qs.filter(doctor=f.doctor)
    return qs


def _visit_type(value) -> str:
    return value if value in ("new", "follow_up") else "walk_in"


@report(
    "new-patients",
    "New patients",
    "clinical",
    "Patients registered in the period, by day and gender.",
    permissions=OPS,
)
def new_patients(f):
    qs = Patient.objects.filter(
        organization=f.organization,
        registered_branch__in=f.branches,
        created_at__gte=f.start_dt,
        created_at__lt=f.end_dt,
    )
    by_day = defaultdict(Counter)
    for created, gender in qs.values_list("created_at", "gender"):
        by_day[local_date(created)][gender] += 1
    rows = [
        {
            "date": day,
            "total": sum(by_day[day].values()),
            "male": by_day[day]["male"],
            "female": by_day[day]["female"],
            "other": sum(by_day[day].values()) - by_day[day]["male"] - by_day[day]["female"],
        }
        for day in f.days
        if by_day.get(day)
    ]
    totals = sum_rows(rows, ["total", "male", "female", "other"])
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("total", "Registered", "int"),
            Column("male", "Male", "int"),
            Column("female", "Female", "int"),
            Column("other", "Other / not recorded", "int"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            {"label": "New registrations", "value": totals["total"], "kind": "int"},
            {
                "label": "Per day (average)",
                "value": round(totals["total"] / len(f.days), 1),
                "kind": "decimal",
                "note": f"Over {len(f.days)} day(s)",
            },
        ],
        chart=[{"label": f"{r['date']:%d %b}", "value": r["total"]} for r in rows] or None,
        chart_title="Registrations per day",
        note="Counted by the branch where the patient was registered.",
    )


@report(
    "returning-patients",
    "Returning patients",
    "clinical",
    "Patients seen in the period who had a visit before it.",
    permissions=OPS,
    filters=VISIT_FILTERS,
)
def returning_patients(f):
    period = (
        encounters(f)
        .values("patient_id")
        .annotate(visits=Count("id"), last=Max("started_at"), first=Min("started_at"))
    )
    seen = {r["patient_id"]: r for r in period}
    prior = dict(
        Encounter.objects.filter(
            organization=f.organization, patient_id__in=seen, started_at__lt=f.start_dt
        )
        .values("patient_id")
        .annotate(prev=Max("started_at"))
        .values_list("patient_id", "prev")
    )
    patients = Patient.objects.in_bulk(list(prior))
    rows = sorted(
        (
            {
                "mrn": patients[pid].mrn,
                "name": patients[pid].full_name,
                "visits": seen[pid]["visits"],
                "previous": local_date(prev),
                "last": local_date(seen[pid]["last"]),
            }
            for pid, prev in prior.items()
        ),
        key=lambda r: (-r["visits"], r["name"]),
    )
    returning, total = len(rows), len(seen)
    return ReportResult(
        columns=[
            Column("mrn", "MRN"),
            Column("name", "Patient"),
            Column("visits", "Visits in period", "int"),
            Column("previous", "Last visit before period", "date"),
            Column("last", "Latest visit", "date"),
        ],
        rows=rows,
        totals={"mrn": "Total", "visits": sum(r["visits"] for r in rows)},
        cards=[
            {"label": "Patients seen", "value": total, "kind": "int"},
            {
                "label": "Returning",
                "value": returning,
                "kind": "int",
                "note": f"{pct(returning, total)}% of patients seen",
            },
            {"label": "First visit in period", "value": total - returning, "kind": "int"},
        ],
        note="A returning patient had a consultation in any branch before the period started.",
    )


@report(
    "opd-visits",
    "OPD visits",
    "clinical",
    "Consultations per day with new, follow-up and walk-in counts.",
    permissions=OPS,
    filters=VISIT_FILTERS,
)
def opd_visits(f):
    by_day = defaultdict(Counter)
    for started, status, vtype in encounters(f).values_list(
        "started_at", "status", "appointment__visit_type"
    ):
        day = by_day[local_date(started)]
        day["visits"] += 1
        day[_visit_type(vtype)] += 1
        if status == Encounter.Status.COMPLETED:
            day["completed"] += 1
    keys = ["visits", "completed", "new", "follow_up", "walk_in"]
    rows = [{"date": d, **{k: by_day[d][k] for k in keys}} for d in f.days if by_day.get(d)]
    totals = sum_rows(rows, keys)
    return ReportResult(
        columns=[
            Column("date", "Date", "date"),
            Column("visits", "Visits", "int"),
            Column("completed", "Signed", "int"),
            Column("new", "New", "int"),
            Column("follow_up", "Follow-up", "int"),
            Column("walk_in", "Walk-in / emergency", "int"),
        ],
        rows=rows,
        totals={"date": "Total", **totals},
        cards=[
            {"label": "OPD visits", "value": totals["visits"], "kind": "int"},
            {
                "label": "Signed",
                "value": totals["completed"],
                "kind": "int",
                "note": f"{pct(totals['completed'], totals['visits'])}% of visits",
            },
            {"label": "Follow-ups", "value": totals["follow_up"], "kind": "int"},
        ],
        chart=[{"label": f"{r['date']:%d %b}", "value": r["visits"]} for r in rows] or None,
        chart_title="Visits per day",
    )


def _grouped_visits(f, field, objects, label):
    rows = []
    for r in (
        encounters(f)
        .values(field)
        .annotate(
            visits=Count("id"),
            completed=Count("id", filter=Q(status=Encounter.Status.COMPLETED)),
            patients=Count("patient", distinct=True),
            follow_up=Count("id", filter=Q(appointment__visit_type="follow_up")),
        )
    ):
        obj = objects.get(r[field])
        rows.append({"name": label(obj), **{k: r[k] for k in r if k != field}})
    rows.sort(key=lambda r: -r["visits"])
    return rows


@report(
    "doctor-visits",
    "Doctor-wise visits",
    "clinical",
    "Consultations, patients and follow-ups per doctor.",
    permissions=OPS,
    filters=VISIT_FILTERS,
)
def doctor_visits(f):
    doctors = DoctorProfile.objects.filter(organization=f.organization).select_related(
        "user", "department"
    )
    rows = _grouped_visits(
        f, "doctor_id", {d.pk: d for d in doctors}, lambda d: str(d) if d else "—"
    )
    keys = ["visits", "completed", "patients", "follow_up"]
    return ReportResult(
        columns=[
            Column("name", "Doctor"),
            Column("visits", "Visits", "int"),
            Column("completed", "Signed", "int"),
            Column("patients", "Patients", "int"),
            Column("follow_up", "Follow-ups", "int"),
        ],
        rows=rows,
        totals={"name": "Total", **sum_rows(rows, keys)},
        chart=chart_from(rows, "name", "visits"),
        chart_title="Visits by doctor",
        note="Patient counts are distinct per doctor; the total can count a patient twice.",
    )


@report(
    "department-visits",
    "Department-wise visits",
    "clinical",
    "Consultations, patients and follow-ups per department.",
    permissions=OPS,
    filters=VISIT_FILTERS,
)
def department_visits(f):
    departments = Department.objects.filter(organization=f.organization).in_bulk()
    rows = _grouped_visits(f, "department_id", departments, lambda d: d.name if d else "—")
    keys = ["visits", "completed", "patients", "follow_up"]
    return ReportResult(
        columns=[
            Column("name", "Department"),
            Column("visits", "Visits", "int"),
            Column("completed", "Signed", "int"),
            Column("patients", "Patients", "int"),
            Column("follow_up", "Follow-ups", "int"),
        ],
        rows=rows,
        totals={"name": "Total", **sum_rows(rows, keys)},
        chart=chart_from(rows, "name", "visits"),
        chart_title="Visits by department",
    )


@report(
    "diagnosis-count",
    "Diagnosis count",
    "clinical",
    "How often each diagnosis was recorded in signed consultations.",
    permissions=OPS,
    filters=VISIT_FILTERS,
)
def diagnosis_count(f):
    signed = encounters(f).filter(status=Encounter.Status.COMPLETED)
    groups: dict = {}
    for code, title, text, patient_id in EncounterDiagnosis.objects.filter(
        encounter__in=signed
    ).values_list("code__code", "code__title", "text", "encounter__patient_id"):
        key = code or text.strip().lower()
        g = groups.setdefault(
            key, {"code": code or "", "name": title or text.strip(), "count": 0, "p": set()}
        )
        g["count"] += 1
        g["p"].add(patient_id)
    rows = sorted(
        (
            {"code": g["code"], "name": g["name"], "count": g["count"], "patients": len(g["p"])}
            for g in groups.values()
        ),
        key=lambda r: (-r["count"], r["name"]),
    )
    return ReportResult(
        columns=[
            Column("code", "Code"),
            Column("name", "Diagnosis"),
            Column("count", "Times recorded", "int"),
            Column("patients", "Patients", "int"),
        ],
        rows=rows,
        totals={"code": "Total", "count": sum(r["count"] for r in rows)},
        chart=chart_from(rows, "name", "count", limit=10),
        chart_title="Top diagnoses",
        note="Coded diagnoses are grouped by code; free-text entries by their wording.",
    )
