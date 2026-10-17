"""Appointments: day/week calendar, today's list, booking, and status actions."""

from datetime import date, datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.forms import StyledFormMixin
from apps.core.scoping import get_scoped_or_404, scoped
from apps.masterdata.models import Department, DoctorProfile, DoctorSchedule
from apps.patients.models import Patient

from . import services
from .models import Appointment

Status = Appointment.Status
VIEW, MANAGE = "appointment.view", "appointment.manage"


def _parse_date(value, default=None):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default or timezone.localdate()


def _doctors(request):
    return (
        scoped(DoctorProfile, request).filter(is_active=True).select_related("user", "department")
    )


def _appointments(request):
    return scoped(Appointment, request).select_related("patient", "doctor__user", "department")


def _crumbs(*extra):
    return [{"label": "Appointments", "url": reverse("appointments:calendar")}, *extra]


# --- Calendar ------------------------------------------------------------------------------


@require_GET
@permission_required(VIEW)
def calendar(request):
    view = request.GET.get("view", "day")
    day = _parse_date(request.GET.get("date"))
    dept_id = request.GET.get("department", "")
    doctor_id = request.GET.get("doctor", "")

    doctors = _doctors(request)
    if dept_id.isdigit():
        doctors = doctors.filter(department_id=dept_id)
    if doctor_id.isdigit():
        doctors = doctors.filter(pk=doctor_id)

    ctx = {
        "view": view,
        "day": day,
        "prev_day": day - timedelta(days=1 if view == "day" else 7),
        "next_day": day + timedelta(days=1 if view == "day" else 7),
        "today": timezone.localdate(),
        "departments": scoped(Department, request).filter(is_active=True),
        "all_doctors": _doctors(request),
        "dept_id": dept_id,
        "doctor_id": doctor_id,
        "can_manage": has_perm(request, MANAGE),
    }
    if view == "week":
        ctx.update(_week_context(request, doctors, day))
    else:
        ctx.update(_day_context(request, doctors, day))
    return render(request, "appointments/calendar.html", ctx)


def _day_context(request, doctors, day):
    """Columns = doctors working (or booked) that day; rows = time steps."""
    schedules = {
        s.doctor_id: s
        for s in DoctorSchedule.objects.filter(
            branch=request.branch, weekday=day.weekday(), is_active=True, doctor__in=doctors
        )
    }
    appts = list(_appointments(request).filter(date=day, doctor__in=doctors).order_by("start_time"))
    booked_doctor_ids = {a.doctor_id for a in appts}
    columns = [d for d in doctors if d.pk in schedules or d.pk in booked_doctor_ids]

    # Rows are the real slot start times of the doctors shown (plus any off-slot bookings such as
    # walk-ins), so every "+ Book" cell offers a time the booking service accepts.
    slot_times = {
        d.pk: set(schedules[d.pk].slot_starts(d.slot_minutes, day)) if d.pk in schedules else set()
        for d in columns
    }
    times = sorted(set().union(*slot_times.values(), {a.start_time for a in appts}))
    rows = []
    for i, row_start in enumerate(times):
        row_end = times[i + 1] if i + 1 < len(times) else time.max
        cells = []
        for d in columns:
            in_row = [
                a for a in appts if a.doctor_id == d.pk and row_start <= a.start_time < row_end
            ]
            cells.append(
                {"doctor": d, "appointments": in_row, "open": row_start in slot_times[d.pk]}
            )
        rows.append({"time": row_start, "cells": cells})
    step = min([d.slot_minutes for d in columns] + [30])
    counts = {s: sum(1 for a in appts if a.status == s) for s in Status.values}
    return {
        "columns": columns,
        "rows": rows,
        "step": step,
        "appointments_count": len(
            [a for a in appts if a.status not in Appointment.RELEASED_STATUSES]
        ),
        "counts": counts,
        "on_leave": [d for d in columns if services.on_leave(d, day)],
    }


def _week_context(request, doctors, day):
    monday = day - timedelta(days=day.weekday())
    days = [monday + timedelta(days=i) for i in range(7)]
    appts = (
        _appointments(request).active().filter(date__range=(days[0], days[-1]), doctor__in=doctors)
    )
    working = set(
        DoctorSchedule.objects.filter(
            branch=request.branch, is_active=True, doctor__in=doctors
        ).values_list("doctor_id", "weekday")
    )
    grid = []
    for d in doctors:
        cells = []
        for dd in days:
            n = sum(1 for a in appts if a.doctor_id == d.pk and a.date == dd)
            cells.append({"date": dd, "count": n, "works": (d.pk, dd.weekday()) in working})
        if any(c["works"] or c["count"] for c in cells):
            grid.append({"doctor": d, "cells": cells})
    return {"week_days": days, "week_grid": grid}


@require_GET
@permission_required(VIEW)
def appointment_list(request):
    """Today's (or a chosen day's) appointments as a list, with quick actions."""
    day = _parse_date(request.GET.get("date"))
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    qs = _appointments(request).filter(date=day).order_by("start_time")
    if status in Status.values:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(patient__name_search__contains=q.lower()) | qs.filter(
            patient__mrn__icontains=q
        )
    return render(
        request,
        "appointments/list.html",
        {
            "appointments": qs,
            "day": day,
            "status": status,
            "q": q,
            "statuses": Status.choices,
            "today": timezone.localdate(),
            "can_manage": has_perm(request, MANAGE),
            "breadcrumbs": _crumbs({"label": "List"}),
        },
    )


# --- Booking -----------------------------------------------------------------------------------


class BookingForm(StyledFormMixin, forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), widget=forms.HiddenInput)
    department = forms.ModelChoiceField(queryset=Department.objects.none(), required=False)
    doctor = forms.ModelChoiceField(queryset=DoctorProfile.objects.none())
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(widget=forms.HiddenInput, error_messages={"required": "Choose a slot."})
    visit_type = forms.ChoiceField(choices=Appointment.VisitType.choices)
    source = forms.ChoiceField(choices=Appointment.Source.choices, label="Booked via")
    reason = forms.CharField(max_length=200, required=False, label="Reason for visit")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["patient"].queryset = scoped(Patient, request).filter(is_active=True)
        self.fields["department"].queryset = scoped(Department, request).filter(is_active=True)
        self.fields["doctor"].queryset = _doctors(request)
        self.fields["visit_type"].widget.attrs["x-model"] = "visitType"


@permission_required(MANAGE)
def book(request):
    initial = {"date": timezone.localdate(), "visit_type": Appointment.VisitType.NEW}
    for key in ("patient", "doctor", "date", "time", "department"):
        if request.GET.get(key):
            initial[key] = request.GET[key]
    form = BookingForm(request.POST or None, initial=initial, request=request)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            appt = services.book(
                request=request,
                patient=c["patient"],
                doctor=c["doctor"],
                on_date=c["date"],
                start=c["time"],
                visit_type=c["visit_type"],
                source=c["source"],
                reason=c["reason"],
                notes=c["notes"],
            )
        except services.BookingError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "appointment.create",
                appt,
                summary=f"{appt.number} {appt.date} {appt.start_time:%H:%M}",
            )
            msg = (
                f"Booked {appt.number}: {appt.patient.full_name} with {appt.doctor} "
                f"at {appt.start_time:%H:%M}."
            )
            if appt.is_overbooked:
                msg += " Overbooked: the slot already had a booking."
            messages.success(request, msg)
            return redirect("appointments:detail", pk=appt.pk)

    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    return render(
        request,
        "appointments/book.html",
        {"form": form, "patient": patient, "breadcrumbs": _crumbs({"label": "Book"})},
    )


@require_GET
@permission_required(VIEW)
def slots(request):
    """htmx: slot buttons for a doctor and date, plus the suggested visit type and fee."""
    doctor = _doctors(request).filter(pk=request.GET.get("doctor") or 0).first()
    day = _parse_date(request.GET.get("date"))
    exclude = request.GET.get("exclude") or None
    ctx = {"doctor": doctor, "day": day, "selected": request.GET.get("time", "")[:5]}
    if doctor:
        ctx["slots"] = services.available_slots(doctor, request.branch, day, exclude_pk=exclude)
        ctx["on_leave"] = services.on_leave(doctor, day)
        ctx["schedule"] = services.schedule_for(doctor, request.branch, day)
        patient = scoped(Patient, request).filter(pk=request.GET.get("patient") or 0).first()
        if patient:
            vt = services.suggest_visit_type(doctor, patient, day)
            ctx["suggested_visit_type"] = vt
            ctx["suggested_fee"] = services.default_fee(doctor, patient, vt)
    return render(request, "appointments/_slots.html", ctx)


@require_GET
@permission_required(VIEW)
def doctor_options(request):
    """htmx: doctor <option>s filtered by department."""
    doctors = _doctors(request)
    dept = request.GET.get("department", "")
    if dept.isdigit():
        doctors = doctors.filter(department_id=dept)
    return render(request, "appointments/_doctor_options.html", {"doctors": doctors})


# --- Detail and actions --------------------------------------------------------------------------


@require_GET
@permission_required(VIEW)
def detail(request, pk):
    appt = get_scoped_or_404(_appointments(request), request, pk=pk)
    return render(
        request,
        "appointments/detail.html",
        {
            "appt": appt,
            "can_manage": has_perm(request, MANAGE),
            "today": timezone.localdate(),
            "breadcrumbs": _crumbs({"label": appt.number}),
            "can_bill": has_perm(request, "billing.payment.collect"),
            "can_view_bills": has_perm(request, "billing.view"),
            "invoices": appt.invoices.exclude(status="cancelled")
            if has_perm(request, "billing.view")
            else [],
        },
    )


@require_GET
@permission_required(VIEW)
def slip(request, pk):
    """A5 appointment slip for the patient (Phase 24)."""
    appt = get_scoped_or_404(_appointments(request), request, pk=pk)
    return render(request, "appointments/slip.html", {"appt": appt})


def _action(request, pk, label, fn, **kwargs):
    appt = get_scoped_or_404(Appointment, request, pk=pk)
    before = appt.status
    try:
        fn(appt, **kwargs)
    except services.BookingError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request, f"appointment.{label}", appt, changes={"status": [before, appt.status]}
        )
        messages.success(request, f"{appt.number} is now {appt.get_status_display().lower()}.")
    return redirect(request.POST.get("next") or reverse("appointments:detail", args=[pk]))


@require_POST
@permission_required(MANAGE)
def confirm(request, pk):
    return _action(request, pk, "confirm", services.confirm)


@require_POST
@permission_required(MANAGE)
def check_in(request, pk):
    """Arrival: check in and issue a queue token in one step (PRD §5.3)."""
    from apps.queue import services as queue_services

    appt = get_scoped_or_404(Appointment, request, pk=pk)
    before = appt.status
    try:
        if appt.date != timezone.localdate():
            raise services.BookingError("Only today's appointments can be checked in.")
        token = queue_services.issue_token(
            request=request,
            patient=appt.patient,
            department=appt.department,
            doctor=appt.doctor,
            appointment=appt,
            priority=request.POST.get("priority", "normal"),
        )
    except (services.BookingError, queue_services.QueueError) as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request, "appointment.check_in", appt, changes={"status": [before, appt.status]}
        )
        audit.record(request, "queue.issue", token, summary=token.label)
        messages.success(request, f"{appt.patient.full_name} checked in. Token {token.label}.")
    return redirect(request.POST.get("next") or reverse("appointments:detail", args=[pk]))


@require_POST
@permission_required(MANAGE)
def no_show(request, pk):
    return _action(request, pk, "no_show", services.mark_no_show)


@require_POST
@permission_required(MANAGE)
def cancel(request, pk):
    return _action(
        request,
        pk,
        "cancel",
        services.cancel,
        user=request.user,
        reason=request.POST.get("reason", ""),
    )


@permission_required(MANAGE)
def reschedule(request, pk):
    appt = get_scoped_or_404(_appointments(request), request, pk=pk)
    error = None
    if request.method == "POST":
        day = _parse_date(request.POST.get("date"))
        doctor = _doctors(request).filter(pk=request.POST.get("doctor") or 0).first() or appt.doctor
        try:
            start = datetime.strptime(request.POST.get("time", ""), "%H:%M").time()
        except ValueError:
            error = "Choose a new slot."
        else:
            old = f"{appt.date} {appt.start_time:%H:%M} {appt.doctor}"
            try:
                services.reschedule(appt, on_date=day, start=start, doctor=doctor)
            except services.BookingError as err:
                error = err.message
            else:
                audit.record(
                    request,
                    "appointment.reschedule",
                    appt,
                    changes={"slot": [old, f"{appt.date} {appt.start_time:%H:%M} {appt.doctor}"]},
                )
                messages.success(
                    request, f"{appt.number} moved to {appt.date:%d %b} at {appt.start_time:%H:%M}."
                )
                return redirect("appointments:detail", pk=pk)
    return render(
        request,
        "appointments/reschedule.html",
        {
            "appt": appt,
            "doctors": _doctors(request),
            "error": error,
            "default_date": max(appt.date, timezone.localdate()),
            "breadcrumbs": _crumbs(
                {"label": appt.number, "url": reverse("appointments:detail", args=[pk])},
                {"label": "Reschedule"},
            ),
        },
    )
