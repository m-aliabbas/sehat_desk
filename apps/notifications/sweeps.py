"""Time-based notifications: things that become true as the clock moves, not when a record
is saved (an appointment starting soon, stock running low, a dose overdue).

run(branch) checks everything for one branch. The middleware calls it at most every
SEHATDESK_NOTIFICATION_SWEEP_SECONDS per branch while staff use the app, and the
`notifications_sweep` command can run it from a scheduler. Dedupe keys make repeats harmless.
"""

import logging
from datetime import datetime, timedelta

from django.urls import reverse
from django.utils import timezone

from .models import Event, Level
from .services import notify

logger = logging.getLogger(__name__)

UPCOMING_MINUTES = 60


def upcoming_appointments(branch, now):
    from apps.appointments.models import Appointment

    local = timezone.localtime(now)
    soon = (local + timedelta(minutes=UPCOMING_MINUTES)).time()
    if soon < local.time():  # the hour wraps past midnight; tomorrow's are not "upcoming"
        soon = datetime.max.time()
    appts = Appointment.objects.filter(
        branch=branch,
        date=local.date(),
        start_time__gte=local.time(),
        start_time__lte=soon,
        status__in=[Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED],
    ).select_related("doctor__user", "patient", "organization")
    for a in appts:
        notify(
            a.organization,
            Event.APPOINTMENT_UPCOMING,
            f"Upcoming at {a.start_time:%H:%M} · {a.number}",
            body=f"MRN {a.patient.mrn} · {a.get_visit_type_display()}",
            url=reverse("appointments:detail", args=[a.pk]),
            branch=branch,
            users=[a.doctor.user],
            dedupe_key=f"appt-upcoming-{a.pk}",
        )


def pharmacy_stock(branch, now):
    from apps.pharmacy import stock

    day = timezone.localdate(now)
    org = branch.organization
    low = stock.low_stock(branch, org).count()
    if low:
        notify(
            org,
            Event.LOW_STOCK,
            f"{low} product{'s' if low != 1 else ''} at or below reorder level",
            body="Reorder before they run out.",
            url=reverse("pharmacy:dashboard"),
            branch=branch,
            level=Level.WARNING,
            permission="pharmacy.inventory.manage",
            dedupe_key=f"low-stock-{branch.pk}-{day}",
        )
    near, gone = stock.near_expiry(branch).count(), stock.expired(branch).count()
    if near or gone:
        parts = []
        if gone:
            parts.append(f"{gone} expired batch{'es' if gone != 1 else ''} still in stock")
        if near:
            parts.append(f"{near} expiring within 90 days")
        notify(
            org,
            Event.EXPIRY,
            "Medicine expiry alert",
            body="; ".join(parts) + ".",
            url=reverse("pharmacy:dashboard"),
            branch=branch,
            level=Level.WARNING if gone else Level.INFO,
            permission="pharmacy.inventory.manage",
            dedupe_key=f"expiry-{branch.pk}-{day}",
        )


def workflow_reminders(branch, now):
    """Outstanding work: unsigned consultations, unverified lab results, overdue ward tasks,
    unacknowledged handovers."""
    from apps.encounters.models import Encounter
    from apps.ipd.models import Admission
    from apps.lab.models import LabOrder
    from apps.nursing.models import HandoverNote
    from apps.nursing.services import due_tasks

    org = branch.organization
    day = timezone.localdate(now)
    start_of_day = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)

    drafts = (
        Encounter.objects.filter(
            branch=branch, status=Encounter.Status.DRAFT, started_at__lt=start_of_day
        )
        .select_related("doctor__user")
        .order_by("doctor")
    )
    by_doctor = {}
    for e in drafts:
        by_doctor.setdefault(e.doctor, 0)
        by_doctor[e.doctor] += 1
    for doctor, n in by_doctor.items():
        notify(
            org,
            Event.REMINDER,
            f"{n} unsigned consultation{'s' if n != 1 else ''} from earlier days",
            body="Complete and sign them so the record is final.",
            url=reverse("encounters:workspace"),
            branch=branch,
            users=[doctor.user],
            dedupe_key=f"draft-encounters-{doctor.pk}-{day}",
        )

    waiting = LabOrder.objects.filter(
        branch=branch,
        status=LabOrder.Status.RESULT_ENTERED,
        updated_at__lt=now - timedelta(hours=2),
    ).count()
    if waiting:
        notify(
            org,
            Event.REMINDER,
            f"{waiting} lab order{'s' if waiting != 1 else ''} waiting for verification",
            body="Results were entered more than 2 hours ago.",
            url=reverse("lab:worklist"),
            branch=branch,
            permission="lab.result.verify",
            dedupe_key=f"lab-verify-{branch.pk}-{day}-{timezone.localtime(now).hour // 4}",
        )

    admissions = list(
        Admission.objects.filter(
            branch=branch, status__in=Admission.ACTIVE_STATUSES
        ).select_related("patient", "current_bed", "vitals_schedule")
    )
    overdue = [t for t in due_tasks(admissions, now=now) if t["state"] == "overdue"]
    if overdue:
        n = len(overdue)
        hour = timezone.localtime(now).strftime("%Y%m%d%H")
        notify(
            org,
            Event.REMINDER,
            f"{n} overdue ward task{'s' if n != 1 else ''}",
            body="Medicine doses or observations are more than an hour late.",
            url=reverse("nursing:workspace"),
            branch=branch,
            level=Level.WARNING,
            permission="nursing.notes",
            dedupe_key=f"ward-overdue-{branch.pk}-{hour}",
        )

    for note in HandoverNote.objects.filter(
        ward__branch=branch, acknowledged_at__isnull=True, created_at__lt=now - timedelta(hours=1)
    ).select_related("ward"):
        notify(
            org,
            Event.REMINDER,
            f"Handover for {note.ward.name} not acknowledged",
            body=f"{note.get_shift_display()} shift, {note.shift_date:%d %b}.",
            url=f"{reverse('nursing:handover')}?ward={note.ward_id}",
            branch=branch,
            permission="nursing.notes",
            exclude=note.given_by,
            dedupe_key=f"handover-{note.pk}",
        )


SWEEPS = [upcoming_appointments, pharmacy_stock, workflow_reminders]


def run(branch, now=None) -> None:
    now = now or timezone.now()
    for sweep in SWEEPS:
        try:
            sweep(branch, now)
        except Exception:  # noqa: BLE001 - one failing check must not stop the others
            logger.exception("notification sweep %s failed branch=%s", sweep.__name__, branch.pk)
