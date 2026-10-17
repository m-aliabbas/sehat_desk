"""Nursing operations and the questions the nursing workspace asks ("what is due now?").

Writes check nursing.notes (or ipd.manage for assignments) and refuse closed admissions.
Views write the audit entry against the admission after each successful call.
"""

from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.ipd.models import Admission, MedicationOrder

from .models import (
    HandoverNote,
    IntakeOutput,
    MedicationAdministration,
    NurseAssignment,
    NursingNote,
    VitalsSchedule,
    current_shift,
)

NURSING = "nursing.notes"
DUE_WINDOW = timedelta(hours=1)  # a dose is "due" from 1 h before to 1 h after its time


class NursingError(ValidationError):
    pass


def _need(request, code, action):
    if code not in request.sd_perms:
        raise NursingError(f"You are not allowed to {action}.")


def _require_active(admission):
    if not admission.is_active:
        raise NursingError(f"{admission.number} is {admission.get_status_display().lower()}.")


def _mark_error(obj, *, request, reason):
    if not reason.strip():
        raise NursingError("Give the reason the entry is wrong.")
    obj = type(obj).objects.select_for_update(of=("self",)).get(pk=obj.pk)
    if obj.entered_in_error:
        raise NursingError("This entry is already marked as entered in error.")
    obj.entered_in_error = True
    obj.error_reason = reason.strip()[:200]
    obj.error_marked_by = request.user
    obj.error_marked_at = timezone.now()
    obj.save(
        update_fields=[
            "entered_in_error",
            "error_reason",
            "error_marked_by",
            "error_marked_at",
            "updated_at",
        ]
    )
    return obj


@transaction.atomic
def mark_error(obj, *, request, reason: str):
    _need(request, NURSING, "correct nursing records")
    return _mark_error(obj, request=request, reason=reason)


# --- Assignments -------------------------------------------------------------------------------


@transaction.atomic
def assign(admission, *, request, nurse) -> NurseAssignment:
    _need(request, "ipd.manage", "assign nurses")
    _require_active(admission)
    if nurse.organization_id != admission.organization_id or not nurse.is_active:
        raise NursingError("Choose an active staff member of this hospital.")
    try:
        with transaction.atomic():
            return NurseAssignment.objects.create(
                admission=admission, nurse=nurse, assigned_by=request.user
            )
    except IntegrityError as err:
        raise NursingError(f"{nurse} is already assigned to this patient.") from err


@transaction.atomic
def unassign(assignment, *, request) -> NurseAssignment:
    _need(request, "ipd.manage", "change nurse assignments")
    assignment.ended_at = timezone.now()
    assignment.save(update_fields=["ended_at", "updated_at"])
    return assignment


def assigned_admissions(user, branch):
    return Admission.objects.filter(
        branch=branch,
        status__in=Admission.ACTIVE_STATUSES,
        nurse_assignments__nurse=user,
        nurse_assignments__ended_at__isnull=True,
    ).distinct()


# --- Notes, schedule, intake/output ------------------------------------------------------------


@transaction.atomic
def add_note(admission, *, request, kind: str, note: str) -> NursingNote:
    _need(request, NURSING, "write nursing notes")
    _require_active(admission)
    if not note.strip():
        raise NursingError("Write the note.")
    if kind not in NursingNote.Kind.values:
        kind = NursingNote.Kind.GENERAL
    return NursingNote.objects.create(
        admission=admission, kind=kind, note=note.strip(), recorded_by=request.user
    )


@transaction.atomic
def set_vitals_schedule(admission, *, request, interval_hours: int) -> VitalsSchedule:
    _need(request, NURSING, "set observation schedules")
    _require_active(admission)
    if interval_hours not in dict(VitalsSchedule.INTERVALS):
        raise NursingError("Choose a valid interval.")
    schedule, _ = VitalsSchedule.objects.update_or_create(
        admission=admission, defaults={"interval_hours": interval_hours, "set_by": request.user}
    )
    return schedule


def last_vitals(admission):
    from apps.vitals.models import VitalSign

    return (
        VitalSign.objects.filter(
            organization=admission.organization,
            patient=admission.patient,
            entered_in_error=False,
            recorded_at__gte=admission.admitted_at,
        )
        .order_by("-recorded_at")
        .first()
    )


def vitals_due_at(admission, last=None):
    """When the next observations are due, or None without a schedule."""
    schedule = getattr(admission, "vitals_schedule", None)
    if schedule is None:
        return None
    last = last if last is not None else last_vitals(admission)
    start = last.recorded_at if last else admission.admitted_at
    return start + timedelta(hours=schedule.interval_hours)


@transaction.atomic
def record_intake_output(admission, *, request, kind, category, volume_ml, notes=""):
    _need(request, NURSING, "record intake and output")
    _require_active(admission)
    allowed = IntakeOutput.INTAKE_CATEGORIES if kind == "intake" else IntakeOutput.OUTPUT_CATEGORIES
    if kind not in IntakeOutput.Kind.values or category not in allowed:
        raise NursingError("Choose a matching type and category.")
    if not volume_ml or not 1 <= int(volume_ml) <= 10000:
        raise NursingError("Enter a volume between 1 and 10,000 ml.")
    return IntakeOutput.objects.create(
        admission=admission,
        kind=kind,
        category=category,
        volume_ml=int(volume_ml),
        notes=(notes or "").strip()[:200],
        recorded_by=request.user,
    )


def fluid_balance(admission, *, hours=24, now=None) -> dict:
    now = now or timezone.now()
    rows = admission.intake_output.filter(
        entered_in_error=False, recorded_at__gte=now - timedelta(hours=hours)
    )
    totals = {r["kind"]: r["s"] for r in rows.values("kind").annotate(s=Sum("volume_ml"))}
    intake, output = totals.get("intake", 0), totals.get("output", 0)
    return {"intake": intake, "output": output, "balance": intake - output, "hours": hours}


# --- Medication administration record ----------------------------------------------------------


def scheduled_times(order: MedicationOrder, day) -> list[datetime]:
    """Dose times for one order on one local day, inside the order's active period."""
    hours = MedicationOrder.SCHEDULE.get(order.frequency, [])
    if not hours:
        return []
    if (
        order.frequency == "WEEKLY"
        and day.weekday() != timezone.localtime(order.starts_at).weekday()
    ):
        return []
    start = order.starts_at
    end = order.stopped_at
    if order.ends_on and day > order.ends_on:
        return []
    times = []
    for h in hours:
        at = timezone.make_aware(datetime.combine(day, time(h)))
        # A dose due within the hour before the order starts is still given (first dose).
        if at < start - DUE_WINDOW or (end is not None and at > end):
            continue
        times.append(at)
    return times


def mar_for_day(admission, day, *, now=None) -> list[dict]:
    """One row per order: its scheduled doses that day with status, plus PRN / one-off doses."""
    now = now or timezone.now()
    day_start = timezone.make_aware(datetime.combine(day, time.min))
    day_end = day_start + timedelta(days=1)
    orders = list(
        admission.medication_orders.filter(starts_at__lt=day_end).exclude(stopped_at__lt=day_start)
    )
    charted = {}
    adhoc = {}
    for a in (
        MedicationAdministration.objects.filter(order__in=orders)
        .filter(
            Q(scheduled_for__gte=day_start, scheduled_for__lt=day_end)
            | Q(
                scheduled_for__isnull=True,
                administered_at__gte=day_start,
                administered_at__lt=day_end,
            )
        )
        .select_related("recorded_by", "error_marked_by")
        .order_by("administered_at")
    ):
        if a.scheduled_for is not None and not a.entered_in_error:
            charted[(a.order_id, a.scheduled_for)] = a
        elif a.scheduled_for is None:
            adhoc.setdefault(a.order_id, []).append(a)
    rows = []
    for order in orders:
        doses = []
        for at in scheduled_times(order, day):
            entry = charted.get((order.pk, at))
            if entry:
                state = entry.status
            elif at < now - DUE_WINDOW:
                state = "overdue"
            elif at <= now + DUE_WINDOW:
                state = "due"
            else:
                state = "upcoming"
            doses.append({"at": at, "entry": entry, "state": state})
        rows.append({"order": order, "doses": doses, "adhoc": adhoc.get(order.pk, [])})
    return rows


@transaction.atomic
def chart_dose(
    order: MedicationOrder,
    *,
    request,
    status: str,
    scheduled_for=None,
    reason: str = "",
    dose_given: str = "",
    administered_at=None,
) -> MedicationAdministration:
    _need(request, NURSING, "chart medicines")
    order = (
        MedicationOrder.objects.select_for_update(of=("self",))
        .select_related("admission")
        .get(pk=order.pk)
    )
    _require_active(order.admission)
    if status not in MedicationAdministration.Status.values:
        raise NursingError("Choose what happened with the dose.")
    if status in MedicationAdministration.NEEDS_REASON and not reason.strip():
        raise NursingError("Give the reason the dose was not given.")
    now = timezone.now()
    if scheduled_for is not None:
        if scheduled_for not in scheduled_times(order, timezone.localtime(scheduled_for).date()):
            raise NursingError("That dose time is not on this medicine's schedule.")
        if scheduled_for > now + DUE_WINDOW:
            raise NursingError("This dose is not due yet.")
    elif order.frequency not in ("SOS", "STAT"):
        raise NursingError("Chart a scheduled dose against its time.")
    if not order.is_active and (scheduled_for is None or scheduled_for > (order.stopped_at or now)):
        raise NursingError("This medicine has been stopped.")
    try:
        with transaction.atomic():
            return MedicationAdministration.objects.create(
                order=order,
                scheduled_for=scheduled_for,
                status=status,
                reason=reason.strip()[:200],
                dose_given=(dose_given or (order.dose if status == "given" else "")).strip()[:40],
                administered_at=administered_at or now,
                recorded_by=request.user,
            )
    except IntegrityError as err:
        raise NursingError("This dose is already charted.") from err


# --- Handover ----------------------------------------------------------------------------------


@transaction.atomic
def give_handover(ward, *, request, summary: str, pending_tasks: str = "") -> HandoverNote:
    _need(request, NURSING, "write handover notes")
    if not summary.strip():
        raise NursingError("Write the handover summary.")
    shift, shift_date = current_shift()
    return HandoverNote.objects.create(
        ward=ward,
        shift=shift,
        shift_date=shift_date,
        summary=summary.strip(),
        pending_tasks=pending_tasks.strip(),
        given_by=request.user,
    )


@transaction.atomic
def acknowledge_handover(note: HandoverNote, *, request) -> HandoverNote:
    _need(request, NURSING, "acknowledge handovers")
    note = HandoverNote.objects.select_for_update(of=("self",)).get(pk=note.pk)
    if note.acknowledged_at:
        raise NursingError("This handover is already acknowledged.")
    if note.given_by_id == request.user.pk:
        raise NursingError("The receiving nurse acknowledges the handover, not the one giving it.")
    note.acknowledged_at = timezone.now()
    note.acknowledged_by = request.user
    note.save(update_fields=["acknowledged_at", "acknowledged_by", "updated_at"])
    return note


# --- Workspace ---------------------------------------------------------------------------------


def due_tasks(admissions, *, now=None) -> list[dict]:
    """Doses and observations due now or overdue, soonest first, for the nursing workspace."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    tasks = []
    for adm in admissions:
        for row in mar_for_day(adm, today, now=now):
            for dose in row["doses"]:
                if dose["state"] in ("due", "overdue"):
                    tasks.append(
                        {
                            "admission": adm,
                            "kind": "dose",
                            "label": f"Give {row['order'].medicine_name} {row['order'].dose}",
                            "due": dose["at"],
                            "state": dose["state"],
                        }
                    )
        due = vitals_due_at(adm)
        if due is not None and due <= now + DUE_WINDOW:
            tasks.append(
                {
                    "admission": adm,
                    "kind": "vitals",
                    "label": "Record observations",
                    "due": due,
                    "state": "overdue" if due < now - DUE_WINDOW else "due",
                }
            )
    return sorted(tasks, key=lambda t: t["due"])
