import logging

from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required

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
def license_page(request: HttpRequest) -> HttpResponse:
    """Where every page leads when an installed copy has no licence. Open only without one."""
    from . import licensing

    license_status = licensing.refresh() if request.method == "POST" else licensing.status()
    if license_status["active"]:
        return redirect("/")
    return render(request, "core/license.html", {"license": license_status})


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


def _work_screen(request, features) -> str | None:
    """Pharmacy and reception staff start where they work; everyone else gets the dashboard."""
    from apps.patients.services import own_doctor_only

    if own_doctor_only(request):
        return "queue:doctor"
    if has_perm(request, "admin.organization.manage") or has_perm(request, "consultation.view"):
        return None
    if features["pharmacy"] and has_perm(request, "pharmacy.sale"):
        return "pharmacy:pos"
    if has_perm(request, "queue.manage") and has_perm(request, "appointment.manage"):
        return "queue:desk"
    return None


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

    features = enabled_features(request.organization)
    home = _work_screen(request, features)
    if home and (home == "queue:doctor" or request.GET.get("view") != "overview"):
        return redirect(home)  # a doctor never gets the hospital-wide numbers
    today = timezone.localdate()
    ctx = {"today": today}
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
    return render(request, "core/system.html", ctx)


@require_GET
@permission_required("admin.organization.manage")
def backup_settings(request: HttpRequest) -> HttpResponse:
    """Settings → Backup and restore: Google Drive, backups on this server, restore."""
    from django.conf import settings as dj

    from apps.core import backup, backup_jobs

    backups = backup.list_backups()[:10]
    latest = backups[0] if backups else None
    hours = dj.SEHATDESK_BACKUP_HOURS or 24
    ctx = {
        "settings_tab": "backups",
        "backups": backups,
        "backup_dir": backup.backup_dir(),
        "backup_hours": dj.SEHATDESK_BACKUP_HOURS,
        "backup_encrypted": bool(dj.SEHATDESK_BACKUP_KEY),
        "backup_overdue": latest is None
        or (timezone.now() - latest.created).total_seconds() > 2 * hours * 3600,
        "job": backup_jobs.status(),
        **_gdrive_context(),
    }
    return render(request, "core/backups.html", ctx)


def _update_page(request: HttpRequest, release=None) -> HttpResponse:
    from apps.core import updates

    current = updates.current_version()
    ctx = {
        "settings_tab": "updates",
        "current_version": current,
        "release": release,
        "update_available": bool(release) and updates.is_newer(release["version"], current),
        "installer": updates.downloaded_installer(),
        "can_install": updates.can_install_here(),
        "job": updates.status(),
    }
    return render(request, "core/updates.html", ctx)


@require_GET
@permission_required("admin.organization.manage")
def update_settings(request: HttpRequest) -> HttpResponse:
    """Settings → Updates: installed version, the update found by the last check, the download."""
    return _update_page(request, request.session.get("update_release"))


@require_POST
@permission_required("admin.organization.manage")
def update_check(request: HttpRequest) -> HttpResponse:
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import updates

    request.session.pop("update_release", None)
    try:
        release = updates.latest_release()
    except updates.UpdateError as err:
        messages.error(request, str(err))
    else:
        request.session["update_release"] = release
        audit.record(request, "update.check", summary=f"Latest version {release['version']}")
    return redirect("core:updates")


@require_POST
@permission_required("admin.organization.manage")
def update_download(request: HttpRequest) -> HttpResponse:
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import updates

    # Asks GitHub again, so a tampered session can never choose what is downloaded.
    try:
        release = updates.latest_release()
        if not updates.is_newer(release["version"], updates.current_version()):
            raise updates.UpdateError("You already have the latest version.")
        updates.start_download(release)
    except (updates.UpdateError, updates.JobBusy) as err:
        messages.error(request, str(err))
    else:
        audit.record(request, "update.download", summary=f"Version {release['version']}")
    return redirect("core:updates")


@require_POST
@permission_required("admin.organization.manage")
def update_install(request: HttpRequest) -> HttpResponse:
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import updates

    installer = updates.downloaded_installer()
    if installer is None:
        messages.error(request, "Download the update first.")
        return redirect("core:updates")
    try:
        updates.launch_installer(installer)
    except (updates.UpdateError, OSError) as err:
        messages.error(request, str(err))
    else:
        audit.record(request, "update.install", summary=installer.name)
        messages.success(request, "The installer has opened on the computer that runs SehatDesk.")
    return redirect("core:updates")


@require_GET
@permission_required("admin.organization.manage")
def update_file(request: HttpRequest) -> HttpResponse:
    from apps.core import updates

    installer = updates.downloaded_installer()
    if installer is None:
        raise Http404
    return FileResponse(installer.open("rb"), as_attachment=True, filename=installer.name)


def _gdrive_context() -> dict:
    from apps.core import gdrive

    return {"gdrive": gdrive.status()}


@require_GET
@permission_required("admin.organization.manage")
def gdrive_panel(request: HttpRequest) -> HttpResponse:
    """The Google Drive card alone: polled while a sign-in or upload is running."""
    return render(request, "core/_gdrive.html", _gdrive_context())


@require_GET
@permission_required("admin.organization.manage")
def gdrive_restore_list(request: HttpRequest) -> HttpResponse:
    """Backups in Drive with a Restore button; loaded on demand because it calls Google."""
    from apps.core import gdrive

    try:
        ctx = {"files": gdrive.list_backups()}
    except gdrive.GoogleDriveError as err:
        ctx = {"error": str(err)}
    return render(request, "core/_gdrive_restore_list.html", ctx)


def _start_restore(request: HttpRequest, start, label: str) -> HttpResponse:
    """Every restore form asks the admin to type RESTORE, then runs in the background."""
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import backup, backup_jobs

    if request.POST.get("confirm", "").strip() != "RESTORE":
        messages.error(request, "Type RESTORE to confirm. Nothing was changed.")
        return redirect("core:backups")
    try:
        audit.record(request, "system.restore", summary=label)
        start()
    except (backup.BackupError, backup_jobs.JobBusy) as err:
        messages.error(request, str(err))
    else:
        messages.info(request, "Restore started in the background.")
    return redirect("core:backups")


@require_POST
@permission_required("admin.organization.manage")
def restore_upload(request: HttpRequest) -> HttpResponse:
    """Restore from a chosen .sqlite3 file or backup file."""
    from django.contrib import messages

    from apps.core import backup, backup_jobs

    upload = request.FILES.get("file")
    if upload is None:
        messages.error(request, "Choose a file to restore.")
        return redirect("core:backups")
    if request.POST.get("confirm", "").strip() != "RESTORE":
        messages.error(request, "Type RESTORE to confirm. Nothing was changed.")
        return redirect("core:backups")
    try:
        path = backup_jobs.save_upload(upload)
    except backup.BackupError as err:
        messages.error(request, str(err))
        return redirect("core:backups")
    return _start_restore(
        request, lambda: backup_jobs.start_restore(path, delete_after=True), path.name
    )


@require_POST
@permission_required("admin.organization.manage")
def restore_local(request: HttpRequest) -> HttpResponse:
    """Restore a file listed in the server's backup folder."""
    from apps.core import backup_jobs

    name = request.POST.get("name", "")
    return _start_restore(
        request, lambda: backup_jobs.start_restore(backup_jobs.local_backup(name)), name
    )


@require_POST
@permission_required("admin.organization.manage")
def restore_drive(request: HttpRequest) -> HttpResponse:
    """Download a backup from Google Drive and restore it."""
    from apps.core import backup_jobs

    file_id = request.POST.get("file_id", "")
    return _start_restore(
        request, lambda: backup_jobs.start_restore_from_drive(file_id), "Google Drive backup"
    )


@require_POST
@permission_required("admin.organization.manage")
def gdrive_connect(request: HttpRequest) -> HttpResponse:
    """Opens Google's sign-in page on the server computer; the card polls for the result."""
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import gdrive

    gdrive.start_connect()
    audit.record(request, "system.gdrive_connect", summary="Google Drive sign-in started")
    messages.info(request, "Finish the Google sign-in in the browser on the server computer.")
    return redirect("core:backups")


@require_POST
@permission_required("admin.organization.manage")
def gdrive_disconnect(request: HttpRequest) -> HttpResponse:
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import gdrive

    gdrive.disconnect()
    audit.record(request, "system.gdrive_disconnect", summary="Google Drive disconnected")
    messages.success(request, "Google Drive disconnected. Backups already there were kept.")
    return redirect("core:backups")


@require_POST
@permission_required("admin.organization.manage")
def gdrive_backup_now(request: HttpRequest) -> HttpResponse:
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import gdrive

    gdrive.start_backup()
    audit.record(request, "system.gdrive_backup", summary="Google Drive backup started")
    messages.info(request, "Google Drive backup started.")
    return redirect("core:backups")


@require_POST
@permission_required("admin.organization.manage")
def backup_now(request: HttpRequest) -> HttpResponse:
    """Write a backup to the server's folder in the background (never sent to the browser)."""
    from django.contrib import messages

    from apps.audit import services as audit
    from apps.core import backup_jobs

    try:
        backup_jobs.start_backup()
    except backup_jobs.JobBusy as err:
        messages.error(request, str(err))
    else:
        audit.record(request, "system.backup", summary="Backup started")
        messages.info(request, "Backup started in the background.")
    return redirect("core:backups")


@require_GET
@permission_required("admin.organization.manage")
def menu(request):
    """All menu page: every screen grouped by category, plus the module switches."""
    from apps.organizations.forms import ModulesForm
    from apps.organizations.models import preferences_for

    from .menu_hub import build_hub

    return render(
        request,
        "core/menu.html",
        {
            "groups": build_hub(request),
            "modules_form": ModulesForm(instance=preferences_for(request.organization)),
        },
    )
