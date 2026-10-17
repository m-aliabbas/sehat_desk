"""Report engine (Phase 20): filters, permission checks and the registry of reports.

A report is a function that takes parsed `Filters` and returns a `ReportResult` (columns, rows,
totals, summary cards and an optional bar chart). Views, exports (CSV / XLSX / PDF) and the
reports hub all read the same registry, so a new report needs one decorated function.

Scoping: every report queries with `filters.organization` and `filters.branches`. The branch
list only ever holds branches where the user has the report's permission (checked per branch,
not only in the current one), so "All branches" never widens access.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.core.exceptions import PermissionDenied
from django.utils import timezone

OPS = ("reports.view",)
FIN = ("reports.financial.view",)
MAX_DAYS = 366

GROUPS = [
    ("clinical", "Clinical"),
    ("financial", "Financial"),
    ("lab", "Laboratory"),
    ("pharmacy", "Pharmacy"),
    ("ipd", "Inpatient (IPD)"),
]


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = "text"  # text | int | money | decimal | date | datetime | percent | hours | days


@dataclass
class ReportResult:
    columns: list[Column]
    rows: list[dict]
    totals: dict | None = None
    cards: list[dict] = field(default_factory=list)  # {"label", "value", "kind", "note"}
    chart: list[dict] | None = None  # [{"label", "value"}] drawn as horizontal bars
    chart_kind: str = "int"
    chart_title: str = ""
    note: str = ""


@dataclass(frozen=True)
class ReportDef:
    key: str
    title: str
    group: str
    description: str
    permissions: tuple[str, ...]  # any one of these
    filters: frozenset  # subset of {"date", "branch", "department", "doctor"}
    build: Callable
    date_hint: str = ""


REPORTS: dict[str, ReportDef] = {}


def report(
    key, title, group, description, *, permissions, filters=("date", "branch"), date_hint=""
):
    def decorator(fn):
        REPORTS[key] = ReportDef(
            key, title, group, description, tuple(permissions), frozenset(filters), fn, date_hint
        )
        return fn

    return decorator


def can_view(request, rdef: ReportDef) -> bool:
    return any(c in request.sd_perms for c in rdef.permissions)


def visible_reports(request) -> list[tuple[str, str, list[ReportDef]]]:
    groups = []
    for key, label in GROUPS:
        items = [r for r in REPORTS.values() if r.group == key and can_view(request, r)]
        if items:
            groups.append((key, label, items))
    return groups


def allowed_branches(request, permissions: tuple[str, ...]) -> list:
    """Branches the user can open with one of these permissions (checked per branch)."""
    from apps.accounts.services import effective_permissions

    cache = request.__dict__.setdefault("_sd_branch_perms", {})
    allowed = []
    for b in request.branches:
        if b == request.branch:
            held = request.sd_perms
        else:
            if b.pk not in cache:
                cache[b.pk] = effective_permissions(request.user, b)
            held = cache[b.pk]
        if any(c in held for c in permissions):
            allowed.append(b)
    return allowed


@dataclass
class Filters:
    organization: object
    start: date
    end: date
    branches: list
    branch_choice: str = ""
    department: object = None
    doctor: object = None
    adjusted: str = ""  # message when the requested range was shortened

    @property
    def start_dt(self) -> datetime:
        return timezone.make_aware(datetime.combine(self.start, time.min))

    @property
    def end_dt(self) -> datetime:
        """Exclusive upper bound: midnight after the last day."""
        return timezone.make_aware(datetime.combine(self.end + timedelta(days=1), time.min))

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    @property
    def branch_label(self) -> str:
        if len(self.branches) == 1:
            return self.branches[0].name
        return f"All branches ({len(self.branches)})"

    def describe(self, rdef: ReportDef) -> str:
        parts = []
        if "date" in rdef.filters:
            parts.append(f"{self.start:%d %b %Y} – {self.end:%d %b %Y}")
        else:
            parts.append(f"As of {timezone.localtime():%d %b %Y %H:%M}")
        parts.append(self.branch_label)
        if self.department is not None:
            parts.append(self.department.name)
        if self.doctor is not None:
            parts.append(str(self.doctor))
        return " · ".join(parts)


def _parse_date(value, default):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def parse_filters(request, rdef: ReportDef) -> Filters:
    """Read GET filters. Unknown or out-of-scope ids are refused, never silently widened."""
    from apps.core.scoping import scoped
    from apps.masterdata.models import Department, DoctorProfile

    today = timezone.localdate()
    start = _parse_date(request.GET.get("from"), today.replace(day=1))
    end = _parse_date(request.GET.get("to"), today)
    if end < start:
        start, end = end, start
    adjusted = ""
    if (end - start).days >= MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS - 1)
        adjusted = f"Reports cover at most {MAX_DAYS} days; showing from {start:%d %b %Y}."

    allowed = allowed_branches(request, rdef.permissions)
    choice = request.GET.get("branch", "") if "branch" in rdef.filters else ""
    if choice == "all":
        branches = allowed
    elif choice.isdigit():
        branches = [b for b in allowed if b.pk == int(choice)]
        if not branches:
            raise PermissionDenied
    else:
        choice = ""
        branches = [request.branch] if request.branch in allowed else allowed[:1]
    if not branches:
        raise PermissionDenied

    department = doctor = None
    dep_id = request.GET.get("department", "")
    if "department" in rdef.filters and dep_id.isdigit():
        department = scoped(Department, request).filter(pk=dep_id).first()
    doc_id = request.GET.get("doctor", "")
    if "doctor" in rdef.filters and doc_id.isdigit():
        doctor = scoped(DoctorProfile, request).select_related("user").filter(pk=doc_id).first()
    return Filters(
        organization=request.organization,
        start=start,
        end=end,
        branches=branches,
        branch_choice=choice,
        department=department,
        doctor=doctor,
        adjusted=adjusted,
    )


def run(rdef: ReportDef, filters: Filters) -> ReportResult:
    return rdef.build(filters)


def local_date(dt) -> date | None:
    return timezone.localtime(dt).date() if dt else None


def pct(part, whole) -> float:
    return round(float(part) * 100 / float(whole), 1) if whole else 0.0


def sum_rows(rows: list[dict], keys) -> dict:
    return {k: sum((r.get(k) or 0) for r in rows) for k in keys}


def chart_from(rows, label_key, value_key, limit=12):
    data = [
        {"label": str(r[label_key]), "value": r[value_key] or 0}
        for r in rows
        if (r.get(value_key) or 0) > 0
    ]
    data.sort(key=lambda d: -d["value"])
    return data[:limit] or None
