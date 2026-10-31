"""Stations and the print queue. Job html is patient data: never log it, keep it only until sent."""

import hashlib
import ipaddress
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from .detect import candidates
from .models import Paper, PrintEnrollment, PrintJob, PrintStation

ONLINE_WINDOW = timedelta(minutes=2)
ENROLLMENT_LIFE = timedelta(hours=24)
AUTO = "__auto__"  # the "detect automatically" choice in Settings → Printers
# A helper that comes back later must not print something staff gave up on and reprinted.
COLLECT_WINDOW = timedelta(seconds=30)
KEEP_JOB_ROWS = timedelta(days=7)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def enrollment_open_until(organization):
    enrollment = PrintEnrollment.objects.filter(
        organization=organization, open_until__gt=timezone.now()
    ).first()
    return enrollment.open_until if enrollment else None


def set_enrollment(organization, *, is_open: bool) -> None:
    PrintEnrollment.objects.update_or_create(
        organization=organization,
        defaults={"open_until": timezone.now() + ENROLLMENT_LIFE if is_open else None},
    )


def _free_name(organization, name: str) -> str:
    base = name.strip()[:70] or "PC"
    taken = set(
        PrintStation.objects.filter(organization=organization).values_list("name", flat=True)
    )
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base} ({number})", number + 1
    return candidate


@transaction.atomic
def join(name: str, ip: str | None) -> tuple[PrintStation, str] | None:
    """A helper connects itself: only from a private address, while one hospital has it open."""
    try:
        if not ipaddress.ip_address(ip or "").is_private:
            return None
    except ValueError:
        return None
    # Two hospitals open at once on one server: there is no telling whose PC this is.
    enrollments = list(PrintEnrollment.objects.filter(open_until__gt=timezone.now())[:2])
    if len(enrollments) != 1:
        return None
    organization = enrollments[0].organization
    token = secrets.token_urlsafe(32)
    station = PrintStation.objects.create(
        organization=organization,
        name=_free_name(organization, name),
        token_hash=_hash(token),
        last_ip=ip,
        last_seen_at=timezone.now(),
    )
    return station, token


def station_for_token(token: str) -> PrintStation | None:
    if not token:
        return None
    return PrintStation.objects.filter(token_hash=_hash(token)).first()


def assign_routes(station: PrintStation) -> None:
    """Fill each paper an admin has not set by hand. Sets the fields; the caller saves."""
    found = candidates(station.printers, station.printer_sizes)
    for paper in list(station.manual_routes):
        chosen = getattr(station, f"route_{paper}", "")
        if chosen and chosen not in station.printers:  # that printer was removed from the PC
            station.manual_routes.remove(paper)
    for paper in Paper.values:
        if paper in station.manual_routes:
            continue
        current = getattr(station, f"route_{paper}")
        options = found[paper]
        if current in options:
            continue  # a new printer must not move a paper that already works
        setattr(station, f"route_{paper}", options[0] if len(options) == 1 else "")


def needs_choice(station: PrintStation, paper: str) -> bool:
    if paper in station.manual_routes or station.printer_for(paper):
        return False
    return len(candidates(station.printers, station.printer_sizes)[paper]) > 1


def touch(
    station: PrintStation,
    ip: str,
    printers: list[str] | None = None,
    sizes: dict | None = None,
) -> None:
    now = timezone.now()
    fields = {}
    stale = not station.last_seen_at or now - station.last_seen_at > timedelta(seconds=15)
    if stale or station.last_ip != ip:
        fields.update(last_seen_at=now, last_ip=ip)
    changed = printers is not None and printers != station.printers
    changed = changed or (sizes is not None and sizes != station.printer_sizes)
    if changed:
        station.printers = station.printers if printers is None else printers
        station.printer_sizes = station.printer_sizes if sizes is None else sizes
        assign_routes(station)
        fields.update(
            printers=station.printers,
            printer_sizes=station.printer_sizes,
            manual_routes=station.manual_routes,
            **{f"route_{paper}": getattr(station, f"route_{paper}") for paper in Paper.values},
        )
    if fields:
        PrintStation.objects.filter(pk=station.pk).update(**fields)


def is_online(station: PrintStation) -> bool:
    seen = station.last_seen_at
    return bool(seen and timezone.now() - seen < ONLINE_WINDOW)


def station_for_request(request) -> PrintStation | None:
    """The helper on the same PC as this browser, if it is running."""
    return (
        PrintStation.objects.filter(
            organization=request.organization,
            last_ip=request.META.get("REMOTE_ADDR"),
            last_seen_at__gte=timezone.now() - ONLINE_WINDOW,
        )
        .exclude(token_hash="")
        .order_by("-last_seen_at")
        .first()
    )


def enqueue(request, paper: str, html: str) -> PrintJob | None:
    """None when this PC has no helper or no printer for this paper: the print window opens."""
    if paper not in Paper.values:
        raise ValueError("Unknown paper.")
    station = station_for_request(request)
    printer = station.printer_for(paper) if station else ""
    if not printer:
        return None
    return PrintJob.objects.create(
        organization=request.organization,
        station=station,
        paper=paper,
        printer=printer,
        html=html,
        created_by=request.user,
    )


def enqueue_test(station: PrintStation, paper: str, user) -> PrintJob:
    html = (
        "<!doctype html><html><head></head><body style='font:bold 13px Arial;text-align:center'>"
        f"<p>SehatDesk test print</p><p>{Paper(paper).label}</p><p>{escape(station.name)}</p>"
        "</body></html>"
    )
    return PrintJob.objects.create(
        organization=station.organization,
        station=station,
        paper=paper,
        printer=station.printer_for(paper),
        html=html,
        created_by=user,
    )


@transaction.atomic
def next_job(station: PrintStation) -> tuple[PrintJob, str] | None:
    """Hand over the oldest waiting page, wiping it from the database in the same step."""
    now = timezone.now()
    jobs = PrintJob.objects.filter(station=station)
    jobs.filter(status=PrintJob.Status.PENDING, created_at__lt=now - COLLECT_WINDOW).update(
        status=PrintJob.Status.EXPIRED, html=""
    )
    jobs.filter(created_at__lt=now - KEEP_JOB_ROWS).delete()
    job = jobs.filter(status=PrintJob.Status.PENDING).order_by("created_at", "pk").first()
    if job is None:
        return None
    html = job.html
    claimed = jobs.filter(pk=job.pk, status=PrintJob.Status.PENDING).update(
        status=PrintJob.Status.SENDING, html=""
    )
    return (job, html) if claimed else None


def finish_job(station: PrintStation, job_id: int, *, ok: bool, error: str = "") -> bool:
    return bool(
        PrintJob.objects.filter(station=station, pk=job_id, status=PrintJob.Status.SENDING).update(
            status=PrintJob.Status.DONE if ok else PrintJob.Status.FAILED,
            error="" if ok else error[:200],
        )
    )
