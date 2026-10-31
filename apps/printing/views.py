import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, scoped

from . import services
from .models import Paper, PrintJob, PrintStation

MANAGE = "admin.organization.manage"


# --- Browser: the Print button (static/js/print_router.js) ------------------------------------
@require_POST
def job_create(request):
    try:
        data = json.loads(request.body)
        job = services.enqueue(request, data["paper"], data["html"])
    except RequestDataTooBig:
        return JsonResponse({"queued": False})
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=400)
    if job is None:
        return JsonResponse({"queued": False})
    return JsonResponse({"queued": True, "id": job.pk, "printer": job.printer})


@require_GET
def job_status(request, pk):
    job = get_scoped_or_404(PrintJob.objects.filter(created_by=request.user), request, pk=pk)
    return JsonResponse({"status": job.status, "error": job.error, "printer": job.printer})


# --- Helper on each PC: signs in with its token, not a user session ---------------------------
def _agent(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.headers.get("Authorization", "")
        station = services.station_for_token(header.removeprefix("Bearer ").strip())
        if station is None:
            return JsonResponse({"error": "not connected"}, status=401)
        return view(request, station, *args, **kwargs)

    return login_not_required(csrf_exempt(wrapper))


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _sizes(raw) -> dict | None:
    """{printer: [[width_mm, height_mm]]}. A helper's report, so nothing odd is kept."""
    if not isinstance(raw, dict):
        return None
    clean = {}
    for name, sizes in list(raw.items())[:100]:
        if isinstance(sizes, list):
            clean[str(name)[:200]] = [
                [int(s[0]), int(s[1])]
                for s in sizes[:200]
                if isinstance(s, list)
                and len(s) == 2
                and all(isinstance(v, int | float) and 0 < v < 100000 for v in s)
            ]
    return clean


@login_not_required
@csrf_exempt
@require_POST
def agent_join(request):
    """Open only while an admin allows new PCs in Settings → Printers, and only from the LAN."""
    joined = services.join(
        str(_body(request).get("name", ""))[:80], request.META.get("REMOTE_ADDR")
    )
    if joined is None:
        return JsonResponse({"error": "New PCs are not allowed right now."}, status=403)
    station, token = joined
    return JsonResponse({"token": token, "name": station.name})


@_agent
@require_POST
def agent_hello(request, station):
    data = _body(request)
    printers = data.get("printers")
    if not isinstance(printers, list):
        printers = None
    else:
        printers = [str(p)[:200] for p in printers][:100]
    services.touch(station, request.META.get("REMOTE_ADDR"), printers, _sizes(data.get("sizes")))
    return JsonResponse({"name": station.name})


@_agent
@require_GET
def agent_next(request, station):
    services.touch(station, request.META.get("REMOTE_ADDR"))
    found = services.next_job(station)
    if found is None:
        return JsonResponse({"job": None})
    job, html = found
    return JsonResponse(
        {"job": {"id": job.pk, "paper": job.paper, "printer": job.printer, "html": html}}
    )


@_agent
@require_POST
def agent_finish(request, station, pk):
    data = _body(request)
    done = services.finish_job(
        station, pk, ok=bool(data.get("ok")), error=str(data.get("error", ""))
    )
    return JsonResponse({"saved": done})


# --- Settings → Printers -----------------------------------------------------------------------
@permission_required(MANAGE)
def stations(request):
    if request.method == "POST":
        is_open = request.POST.get("enrollment") == "open"
        services.set_enrollment(request.organization, is_open=is_open)
        audit.record(
            request,
            "printing.enrollment_open" if is_open else "printing.enrollment_close",
            summary="New PCs allowed" if is_open else "New PCs stopped",
        )
        return redirect("printing:stations")
    rows = list(scoped(PrintStation, request))
    for row in rows:
        row.online = services.is_online(row)
        row.routes = [
            {
                "key": key,
                "label": label,
                "value": row.printer_for(key),
                "manual": key in row.manual_routes,
                "saved": getattr(row, f"route_{key}"),
                "choose": services.needs_choice(row, key),
            }
            for key, label in Paper.choices
        ]
        row.needs_choice = any(r["choose"] for r in row.routes)
    return render(
        request,
        "printing/stations.html",
        {
            "stations": rows,
            "open_until": services.enrollment_open_until(request.organization),
            "settings_tab": "printers",
        },
    )


@require_POST
@permission_required(MANAGE)
def station_routes(request, pk):
    station = get_scoped_or_404(PrintStation, request, pk=pk)
    station.name = request.POST.get("name", "").strip()[:80] or station.name
    for key in Paper.values:
        value = request.POST.get(f"route_{key}", services.AUTO)
        if value == services.AUTO:
            if key in station.manual_routes:
                station.manual_routes.remove(key)
                setattr(station, f"route_{key}", "")
        elif value == "" or value in station.printers:
            setattr(station, f"route_{key}", value)
            if key not in station.manual_routes:
                station.manual_routes.append(key)
    services.assign_routes(station)
    station.save()
    audit.record(request, "printing.station_routes", station, summary=station.name)
    messages.success(request, f"Printers saved for {station.name}.")
    test = request.POST.get("test")
    if test in Paper.values and station.printer_for(test):
        services.enqueue_test(station, test, request.user)
        messages.success(request, f"Test page sent to {station.printer_for(test)}.")
    return redirect("printing:stations")


@require_POST
@permission_required(MANAGE)
def station_delete(request, pk):
    station = get_scoped_or_404(PrintStation, request, pk=pk)
    audit.record(request, "printing.station_delete", station, summary=station.name)
    station.delete()
    messages.success(request, "PC removed.")
    return redirect("printing:stations")
