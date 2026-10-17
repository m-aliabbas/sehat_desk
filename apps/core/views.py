import logging

from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required

from .navigation import NAV_ITEMS, PLACEHOLDER_KEYS, item_is_visible

logger = logging.getLogger(__name__)


def server_error(request: HttpRequest) -> HttpResponse:
    """500 page with the request ID, so staff can quote it. Renders without context
    processors, which may be what failed."""
    from django.template import loader

    html = loader.get_template("500.html").render(
        {"request_id": getattr(request, "request_id", "")}
    )
    return HttpResponse(html, status=500)


# --- Health (no auth, no patient data) ------------------------------------------


@login_not_required
@require_GET
def health_live(request: HttpRequest) -> JsonResponse:
    """Process is up. Does not touch the database."""
    return JsonResponse({"status": "ok"})


@login_not_required
@require_GET
def health_ready(request: HttpRequest) -> JsonResponse:
    """Process can serve requests, including a database round trip."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.error("readiness check failed: database unreachable")
        return JsonResponse({"status": "unavailable", "database": "down"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


# --- App shell ------------------------------------------------------------------


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """Headline numbers for the current branch. Each block shows only with its permission."""
    from django.utils import timezone

    from apps.accounts.decorators import has_perm
    from apps.appointments.models import Appointment
    from apps.core.features import enabled_features
    from apps.core.scoping import scoped
    from apps.masterdata.models import DoctorSchedule
    from apps.patients.models import Patient

    today = timezone.localdate()
    ctx = {"today": today}
    features = enabled_features(request.organization)
    if has_perm(request, "patient.view"):
        patients = scoped(Patient, request).filter(is_active=True)
        ctx["patient_total"] = patients.count()
        ctx["patients_today"] = patients.filter(created_at__date=today).count()
    if has_perm(request, "appointment.view"):
        appts = scoped(Appointment, request).filter(date=today)
        ctx["appt_total"] = appts.active().count()
        ctx["appt_completed"] = appts.filter(status=Appointment.Status.COMPLETED).count()
        ctx["appt_waiting"] = appts.filter(status=Appointment.Status.CHECKED_IN).count()
        ctx["recent_appointments"] = appts.select_related("patient", "doctor__user").order_by(
            "start_time"
        )[:8]
        ctx["doctors_today"] = (
            DoctorSchedule.objects.filter(
                branch=request.branch,
                weekday=today.weekday(),
                is_active=True,
                doctor__is_active=True,
            )
            .values("doctor")
            .distinct()
            .count()
        )
    if has_perm(request, "billing.view"):
        from apps.billing.models import Invoice
        from apps.billing.services import day_totals

        ctx["collection"] = day_totals(request.branch, today)
        ctx["outstanding_count"] = (
            scoped(Invoice, request).filter(status__in=Invoice.OUTSTANDING_STATUSES).count()
        )
    if features["pharmacy"] and has_perm(request, "pharmacy.inventory.manage"):
        from apps.pharmacy import stock

        ctx["inventory_alerts"] = {
            "low": stock.low_stock(request.branch, request.organization).count(),
            "near": stock.near_expiry(request.branch).count(),
            "expired": stock.expired(request.branch).count(),
        }
    if features["lab"] and any(
        has_perm(request, c)
        for c in ("lab.sample.collect", "lab.result.enter", "lab.result.verify")
    ):
        from apps.lab.models import LabOrder

        orders = scoped(LabOrder, request)
        ctx["lab_open"] = orders.exclude(status__in=["released", "cancelled"]).count()
        ctx["lab_critical"] = (
            orders.filter(has_critical=True)
            .exclude(status="cancelled")
            .filter(
                items__results__is_current=True,
                items__results__flag__in=["critical_low", "critical_high"],
                items__results__critical_acknowledged_at__isnull=True,
            )
            .distinct()
            .count()
        )
    ops, fin = has_perm(request, "reports.view"), has_perm(request, "reports.financial.view")
    if ops or fin:
        from apps.reports.kpis import admin_kpis

        ctx["kpi"] = admin_kpis(request, financial=fin)
    return render(request, "core/dashboard.html", ctx)


@require_GET
def module_placeholder(request: HttpRequest, module: str) -> HttpResponse:
    """Landing page for modules that are not built yet, so navigation never dead-ends."""
    if module not in PLACEHOLDER_KEYS:
        raise Http404
    item = next(i for i in NAV_ITEMS if i.key == module)
    if not item_is_visible(item, request):
        raise PermissionDenied
    return render(request, "core/module_placeholder.html", {"module": item})


# --- Design system gallery (screen 12) ----------------------------------------------

# Fictional rows for the gallery only. Never stored, never real patients.
_DEMO_NAMES = [
    "Ayesha Khan",
    "Ahmed Raza",
    "Fatima Noor",
    "Bilal Hussain",
    "Sana Javed",
    "Usman Tariq",
    "Hira Aslam",
    "Zain Ali",
    "Maryam Iqbal",
    "Omar Farooq",
]
_DEMO_STATUSES = ["scheduled", "in_consultation", "completed", "cancelled", "no_show"]
DEMO_ROWS = [
    {
        "mrn": f"DEMO-{1000 + i}",
        "name": _DEMO_NAMES[i % len(_DEMO_NAMES)],
        "time": f"{9 + (i % 8):02d}:{(i * 15) % 60:02d}",
        "status": _DEMO_STATUSES[i % len(_DEMO_STATUSES)],
    }
    for i in range(37)
]


@require_GET
@permission_required("admin.organization.manage")
def design_system(request: HttpRequest) -> HttpResponse:
    return render(request, "core/design_system.html")


@require_GET
@permission_required("admin.organization.manage")
def design_system_table(request: HttpRequest) -> HttpResponse:
    """HTMX fragment that shows each list state: loaded, empty, error, with pagination."""
    state = request.GET.get("state", "loaded")
    if state == "error":
        return render(request, "core/partials/demo_table.html", {"state": "error"})
    rows = [] if state == "empty" else DEMO_ROWS
    page = Paginator(rows, 8).get_page(request.GET.get("page"))
    return render(request, "core/partials/demo_table.html", {"state": state, "page_obj": page})


@require_GET
@permission_required("admin.organization.manage")
def design_system_modal(request: HttpRequest) -> HttpResponse:
    if request.GET.get("kind") == "drawer":
        return render(request, "core/partials/demo_drawer.html")
    return render(request, "core/partials/demo_modal.html")


@require_POST
@permission_required("admin.organization.manage")
def design_system_action(request: HttpRequest) -> HttpResponse:
    """Demo endpoint for the confirmation dialog. Changes nothing; returns a toast trigger."""
    response = HttpResponse(status=204)
    response["HX-Trigger"] = '{"toast": {"level": "success", "message": "Action confirmed."}}'
    return response


# --- Monitoring (Phase 0) -------------------------------------------------------------

CLIENT_ERROR_LIMIT_PER_MINUTE = 20
CLIENT_ERROR_MAX_BYTES = 4096


def _same_origin(request) -> bool:
    """sendBeacon cannot add the CSRF header, so the browser's Origin must be this site."""
    from urllib.parse import urlsplit

    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(origin) and urlsplit(origin).netloc == request.get_host()


@login_not_required
@csrf_exempt
@require_POST
def client_error(request: HttpRequest) -> HttpResponse:
    """Browser script errors from static/js/app.js. Stores no patient data (see monitoring)."""
    import json

    from django.core.cache import cache

    from .monitoring import record_browser_error

    if not _same_origin(request) or len(request.body) > CLIENT_ERROR_MAX_BYTES:
        return HttpResponse(status=400)
    key = f"client-error:{request.META.get('REMOTE_ADDR', '')}"
    cache.add(key, 0, 60)
    if cache.incr(key) > CLIENT_ERROR_LIMIT_PER_MINUTE:
        return HttpResponse(status=429)
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    if isinstance(data, dict):
        record_browser_error(request, data)
    return HttpResponse(status=204)


def _is_loopback(request) -> bool:
    return request.META.get("REMOTE_ADDR") in ("127.0.0.1", "::1")


@login_not_required
@require_GET
def health_metrics(request: HttpRequest) -> HttpResponse:
    """Prometheus text metrics. Open to monitoring on the server itself, otherwise to staff
    holding admin.system.view."""
    from apps.accounts.decorators import has_perm

    from .observability import metrics

    if not _is_loopback(request):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and has_perm(request, "admin.system.view")):
            return HttpResponse(status=404)
    return HttpResponse(metrics.prometheus(), content_type="text/plain; version=0.0.4")


@require_GET
@permission_required("admin.system.view")
def system_health(request: HttpRequest) -> HttpResponse:
    """Settings → System: environment, live metrics, recent errors, backups."""
    from django.conf import settings as dj
    from django.db.models import Q

    from .models import ErrorReport
    from .observability import metrics

    errors = ErrorReport.objects.filter(
        Q(organization=request.organization) | Q(organization__isnull=True)
    ).select_related("user", "branch")
    source = request.GET.get("source", "")
    if source in ErrorReport.Source.values:
        errors = errors.filter(source=source)
    rid = request.GET.get("request_id", "").strip()
    if rid:
        errors = errors.filter(request_id=rid)
    page = Paginator(errors, 25).get_page(request.GET.get("page"))
    snap = metrics.snapshot()
    total = sum(snap["requests"].values())
    server_errors = sum(n for (_m, s), n in snap["requests"].items() if s == "5xx")
    ctx = {
        "settings_tab": "system",
        "page_obj": page,
        "source": source,
        "request_id": rid,
        "env": dj.SEHATDESK_ENV,
        "db_vendor": connection.vendor,
        "data_dir": dj.DATA_DIR,
        "snap": snap,
        "total_requests": total,
        "server_errors": server_errors,
        "avg_ms": (snap["latency_sum"] / snap["latency_count"] * 1000)
        if snap["latency_count"]
        else 0,
        "https": dj.HTTPS,
    }
    from apps.core import backup

    ctx["backups"] = backup.list_backups()[:10]
    ctx["backup_dir"] = backup.backup_dir()
    ctx["backup_hours"] = dj.SEHATDESK_BACKUP_HOURS
    ctx["backup_encrypted"] = bool(dj.SEHATDESK_BACKUP_KEY)
    latest = ctx["backups"][0] if ctx["backups"] else None
    hours = dj.SEHATDESK_BACKUP_HOURS or 24
    ctx["backup_overdue"] = latest is None or (
        (timezone.now() - latest.created).total_seconds() > 2 * hours * 3600
    )
    return render(request, "core/system.html", ctx)


@require_POST
@permission_required("admin.system.view")
def backup_now(request: HttpRequest) -> HttpResponse:
    """Write a backup to the server's backup folder (never downloads it to the browser)."""
    from django.contrib import messages
    from django.shortcuts import redirect

    from apps.audit import services as audit
    from apps.core import backup

    try:
        info = backup.create_backup(key=backup.configured_key())
    except backup.BackupError as err:
        messages.error(request, f"Backup failed: {err}")
    else:
        audit.record(request, "system.backup", summary=info.name)
        messages.success(request, f"Backup written: {info.name}")
    return redirect("core:system")
