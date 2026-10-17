"""Admin audit viewer (Phase 23): read-only list with filters, an entry drawer and CSV export.

There is no add, edit or delete screen for audit events anywhere in the app; the model and the
database refuse changes too. Viewing needs `admin.audit.view`, and only the signed-in user's
organization is ever shown.
"""

import csv
import ipaddress
from datetime import date, datetime, time, timedelta

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.decorators import permission_required
from apps.accounts.models import User
from apps.organizations.models import Branch

from . import catalog, services
from .models import AuditLog

PERM = "admin.audit.view"
EXPORT_LIMIT = 50_000


def _date(value, default):
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _filtered(request):
    """Audit events for this organization narrowed by the GET filters."""
    today = timezone.localdate()
    g = request.GET
    start = _date(g.get("from"), today - timedelta(days=6))
    end = _date(g.get("to"), today)
    if end < start:
        start, end = end, start
    qs = AuditLog.objects.filter(
        organization=request.organization,
        created_at__gte=timezone.make_aware(datetime.combine(start, time.min)),
        created_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min)),
    )
    actor = g.get("actor", "")
    if actor.isdigit():
        qs = qs.filter(actor_id=actor)
    entity = g.get("entity", "")
    if entity:
        qs = qs.filter(entity=entity)
    record = g.get("record", "").strip()
    if record:
        qs = qs.filter(record_id=record)
    action = g.get("action", "").strip()
    if action:
        qs = qs.filter(Q(action=action) | Q(action__startswith=f"{action}."))
    category = g.get("category", "")
    if category in catalog.CATEGORIES:
        cond = Q()
        for prefix in catalog.CATEGORIES[category][1]:
            cond |= (
                Q(action__startswith=prefix) if prefix.endswith((".", "_")) else Q(action=prefix)
            )
        qs = qs.filter(cond)
    branch = g.get("branch", "")
    if branch.isdigit():
        qs = qs.filter(branch_id=branch)
    q = g.get("q", "").strip()
    if q:
        cond = Q(summary__icontains=q) | Q(record_id=q)
        try:  # PostgreSQL's inet column rejects text that is not an address
            ipaddress.ip_address(q)
            cond |= Q(ip_address=q)
        except ValueError:
            pass
        qs = qs.filter(cond)
    filters = {
        "start": start,
        "end": end,
        "actor": actor,
        "entity": entity,
        "record": record,
        "action": action,
        "category": category,
        "branch": branch,
        "q": q,
    }
    return qs.select_related("actor", "branch"), filters


def _actions_in_use(organization):
    codes = (
        AuditLog.objects.filter(organization=organization)
        .order_by()
        .values_list("action", flat=True)
        .distinct()
    )
    modules = sorted({c.split(".", 1)[0] for c in codes})
    return [(m, catalog.module_of(m)) for m in modules], sorted(codes)


@require_GET
@permission_required(PERM)
def log(request):
    qs, filters = _filtered(request)
    if request.GET.get("format") == "csv":
        return _export(request, qs, filters)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    modules, actions = _actions_in_use(request.organization)
    entities = (
        AuditLog.objects.filter(organization=request.organization)
        .exclude(entity="")
        .order_by()
        .values_list("entity", flat=True)
        .distinct()
    )
    return render(
        request,
        "audit/log.html",
        {
            "page_obj": page,
            "entries": [(e, catalog.label(e.action), catalog.module_of(e.action)) for e in page],
            "f": filters,
            "staff": User.objects.filter(organization=request.organization).order_by(
                "first_name", "last_name", "username"
            ),
            "modules": modules,
            "actions": [(a, catalog.label(a)) for a in actions],
            "entities": sorted(entities),
            "categories": [(k, v[0]) for k, v in catalog.CATEGORIES.items()],
            "branches": Branch.objects.filter(organization=request.organization),
            "query": request.GET.urlencode(),
            "settings_tab": "audit",
        },
    )


@require_GET
@permission_required(PERM)
def entry(request, pk):
    e = get_object_or_404(
        AuditLog.objects.select_related("actor", "branch"), pk=pk, organization=request.organization
    )
    return render(
        request,
        "audit/_entry.html",
        {
            "e": e,
            "label": catalog.label(e.action),
            "module": catalog.module_of(e.action),
            "changes": _change_rows(e.changes),
        },
    )


def _change_rows(changes):
    rows = []
    for key, value in (changes or {}).items():
        if isinstance(value, list) and len(value) == 2:
            rows.append({"field": key, "old": value[0], "new": value[1], "pair": True})
        else:
            rows.append({"field": key, "value": value, "pair": False})
    return rows


def _export(request, qs, filters):
    services.record(
        request,
        "audit.export",
        summary=f"audit {filters['start']}–{filters['end']}"[:255],
        changes={k: v for k, v in filters.items() if v and k not in ("start", "end")},
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="audit-{filters["start"]:%Y%m%d}-{filters["end"]:%Y%m%d}.csv"'
    )
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(
        [
            "Time",
            "Staff",
            "Username",
            "Branch",
            "Module",
            "Action",
            "Code",
            "Record",
            "Record ID",
            "Summary",
            "Changes",
            "IP address",
        ]
    )
    for e in qs[:EXPORT_LIMIT].iterator():
        writer.writerow(
            [
                f"{timezone.localtime(e.created_at):%Y-%m-%d %H:%M:%S}",
                e.actor.get_full_name() if e.actor_id else "",
                e.actor.username if e.actor_id else "",
                e.branch.name if e.branch_id else "",
                catalog.module_of(e.action),
                catalog.label(e.action),
                e.action,
                e.entity,
                e.record_id,
                e.summary,
                e.changes or "",
                e.ip_address or "",
            ]
        )
    return response
