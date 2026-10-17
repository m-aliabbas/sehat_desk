"""Reports hub, report pages with filters, and exports (Phase 20)."""

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.scoping import scoped
from apps.masterdata.models import Department, DoctorProfile

from . import engine, exports

HUB_PERMS = ("reports.view", "reports.financial.view", "pharmacy.inventory.manage")
PAGE_SIZE = 100


def _crumbs(*extra):
    return [{"label": "Reports", "url": reverse("reports:hub")}, *extra]


@require_GET
@permission_required(*HUB_PERMS, any_of=True)
def hub(request):
    return render(
        request,
        "reports/hub.html",
        {"groups": engine.visible_reports(request), "breadcrumbs": _crumbs()},
    )


def _presets(today):
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    return [
        ("Today", today, today),
        ("Last 7 days", today - timedelta(days=6), today),
        ("This month", month_start, today),
        ("Last month", last_month_end.replace(day=1), last_month_end),
        ("Last 30 days", today - timedelta(days=29), today),
    ]


@require_GET
def report_view(request, key):
    rdef = engine.REPORTS.get(key)
    if rdef is None:
        raise Http404
    if not engine.can_view(request, rdef):
        raise PermissionDenied
    filters = engine.parse_filters(request, rdef)
    result = engine.run(rdef, filters)
    context = {
        "report": rdef,
        "f": filters,
        "result": result,
        "scope": filters.describe(rdef),
        "organization": request.organization,
        "generated_at": timezone.localtime(),
    }

    fmt = request.GET.get("format", "")
    if fmt in exports.FORMATS:
        audit.record(
            request,
            "report.export",
            summary=f"{rdef.key} {fmt} · {context['scope']}"[:255],
        )
        if fmt == "csv":
            return exports.to_csv(rdef, filters, result)
        if fmt == "xlsx":
            return exports.to_xlsx(rdef, filters, result, request.organization)
        return exports.to_pdf(request, rdef, filters, result, context)

    if filters.adjusted:
        messages.warning(request, filters.adjusted)
    page = Paginator(result.rows, PAGE_SIZE).get_page(request.GET.get("page"))
    branches = engine.allowed_branches(request, rdef.permissions)
    return render(
        request,
        "reports/report.html",
        {
            **context,
            "page_obj": page,
            "rows": page.object_list,
            "branches": branches,
            "departments": scoped(Department, request).filter(is_active=True)
            if "department" in rdef.filters
            else [],
            "doctors": scoped(DoctorProfile, request).select_related("user")
            if "doctor" in rdef.filters
            else [],
            "presets": _presets(timezone.localdate()) if "date" in rdef.filters else [],
            "export_query": request.GET.urlencode(),
            "breadcrumbs": _crumbs({"label": rdef.title}),
        },
    )
