"""Appointments: day/week calendar, today's list, booking, and status actions."""

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.billing import services as billing_services
from apps.billing.models import Invoice, PaymentMethod
from apps.core.forms import StyledFormMixin
from apps.core.scoping import get_scoped_or_404, scoped
from apps.masterdata.models import Department, DoctorProfile, DoctorSchedule
from apps.organizations import field_rules
from apps.organizations.models import preferences_for
from apps.patients import services as patient_services
from apps.patients.forms import PatientForm
from apps.patients.models import Patient
from apps.patients.services import own_doctor_only
from apps.printing import direct as printing_direct
from apps.queue import services as queue_services
from apps.vitals import services as vitals_services
from apps.vitals.forms import VitalsForm

from . import services, visits
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
    qs = scoped(Appointment, request).select_related("patient", "doctor__user", "department")
    if own := own_doctor_only(request):
        qs = qs.filter(doctor=own)
    return qs


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
    if own := own_doctor_only(request):
        doctors = doctors.filter(pk=own.pk)
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
    schedules = {}
    for s in DoctorSchedule.objects.filter(
        branch=request.branch, weekday=day.weekday(), is_active=True, doctor__in=doctors
    ):
        schedules.setdefault(s.doctor_id, []).append(s)
    appts = list(_appointments(request).filter(date=day, doctor__in=doctors).order_by("start_time"))
    booked_doctor_ids = {a.doctor_id for a in appts}
    columns = [d for d in doctors if d.pk in schedules or d.pk in booked_doctor_ids]

    # Rows are the real slot start times of the doctors shown (plus any off-slot bookings such as
    # walk-ins), so every "+ Book" cell offers a time the booking service accepts.
    slot_times = {
        d.pk: {t for s in schedules.get(d.pk, []) for t in s.slot_starts(d.slot_minutes, day)}
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


# --- Front-desk visit (one screen: patient, doctor, time, vitals, fee, papers) --------------------


class VisitForm(StyledFormMixin, forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), required=False)
    doctor = forms.ModelChoiceField(
        queryset=DoctorProfile.objects.none(), error_messages={"required": "Choose a doctor."}
    )
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    time = forms.TimeField(required=False, input_formats=["%H:%M"])
    shift = forms.ChoiceField(choices=[("", "Any"), *DoctorSchedule.Shift.choices], required=False)
    reason = forms.CharField(max_length=200, required=False, label="Reason for visit")
    source = forms.ChoiceField(choices=Appointment.Source.choices, required=False)
    collect = forms.BooleanField(required=False)
    method = forms.ModelChoiceField(queryset=PaymentMethod.objects.none(), required=False)
    reference = forms.CharField(max_length=80, required=False)
    key = forms.CharField(max_length=40, required=False)
    discount = forms.DecimalField(min_value=0, max_digits=12, decimal_places=2, required=False)
    discount_reason = forms.CharField(max_length=200, required=False)
    cash_paid = forms.DecimalField(min_value=0, max_digits=12, decimal_places=2, required=False)
    emergency = forms.BooleanField(required=False)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["patient"].queryset = scoped(Patient, request).filter(is_active=True)
        self.fields["doctor"].queryset = _doctors(request)
        self.fields["reason"].required = field_rules.is_required(
            request.organization, "visit.reason"
        )
        self.fields["method"].queryset = billing_services.active_methods(request.organization)


def _doctor_rows(request):
    """What the doctor picker filters in the browser: a hospital has tens of doctors, not more."""
    today = timezone.localdate()
    hours = {}
    for s in DoctorSchedule.objects.filter(
        branch=request.branch, weekday=today.weekday(), is_active=True
    ).order_by("start_time"):
        hours.setdefault(s.doctor_id, []).append(f"{s.start_time:%H:%M}–{s.end_time:%H:%M}")
    return [
        {
            "id": d.pk,
            "name": d.display_name,
            "dept": d.department.name,
            "fee": str(int(d.consultation_fee)),
            "today": ", ".join(hours.get(d.pk, [])) or "No clinic today",
        }
        for d in _doctors(request)
    ]


def _has_vitals(request) -> bool:
    return any(
        v.strip()
        for k, v in request.POST.items()
        if k.startswith("v-") and k not in ("v-temperature_unit", "v-glucose_context")
    )


def _quick_patient_form(request, data=None):
    return PatientForm(data, organization=request.organization, quick=True, prefix="new")


@permission_required(MANAGE)
def visit_new(request):
    """Reception's main screen. Enter a patient and doctor, and the rest is filled in for them."""
    can_collect = has_perm(request, visits.COLLECT)
    can_vitals = has_perm(request, "vitals.record")
    can_register = has_perm(request, "patient.create")
    today = timezone.localdate()
    post = request.POST or None
    new_mode = request.POST.get("mode") == "new"
    form = VisitForm(
        post,
        initial={"date": today, "patient": request.GET.get("patient", "")},
        request=request,
    )
    patient_form = _quick_patient_form(request, post if new_mode else None)
    patient_form.fields["cnic"].required = field_rules.is_required(
        request.organization, "visit.cnic"
    )
    if request.POST.get("emergency"):  # no time to ask: the details can be added afterwards
        patient_form.fields["phone"].required = False
        patient_form.fields["cnic"].required = False
    want_vitals = bool(post) and can_vitals and _has_vitals(request)
    vitals_form = VitalsForm(post if want_vitals else None, prefix="v")

    if post:
        valid = form.is_valid()
        valid = (patient_form.is_valid() if new_mode else True) and valid
        valid = (vitals_form.is_valid() if want_vitals else True) and valid
        if valid and not new_mode and not form.cleaned_data["patient"]:
            form.add_error(None, "Find the patient, or add a new one.")
            valid = False
        if new_mode and not can_register:
            form.add_error(None, "You are not allowed to register patients.")
            valid = False
        if valid:
            c = form.cleaned_data
            try:
                with transaction.atomic():
                    patient = c["patient"]
                    if new_mode:
                        patient = patient_services.register_patient(
                            organization=request.organization,
                            branch=request.branch,
                            user=request.user,
                            data=patient_form.model_data(),
                        )
                        audit.record(
                            request, "patient.create", patient, summary=f"mrn={patient.mrn}"
                        )
                    visit = visits.register_visit(
                        request=request,
                        patient=patient,
                        doctor=c["doctor"],
                        on_date=c["date"],
                        start=c["time"],
                        shift=c["shift"],
                        reason=c["reason"],
                        source=c["source"] or Appointment.Source.COUNTER,
                        vitals=vitals_form.vitals_data() if want_vitals else None,
                        collect=can_collect and c["collect"],
                        method=c["method"],
                        reference=c["reference"],
                        key=c["key"],
                        cash_paid=c["cash_paid"],
                        discount=c["discount"] or 0,
                        discount_reason=c["discount_reason"],
                        emergency=c["emergency"],
                    )
            except (
                services.BookingError,
                vitals_services.VitalsError,
                billing_services.BillingError,
            ) as err:
                form.add_error(None, err.message)
            except visits.queue_services.QueueError as err:
                form.add_error(None, err.message)
            else:
                appt = visit.appointment
                audit.record(
                    request,
                    "appointment.create",
                    appt,
                    summary=f"{appt.number} {appt.date} {appt.start_time:%H:%M}",
                )
                if visit.token:
                    audit.record(request, "queue.issue", visit.token, summary=visit.token.label)
                if visit.payment:
                    _audit_fee(request, visit.invoice, visit.payment)
                return redirect(f"{reverse('appointments:papers', args=[appt.pk])}?print=1")

    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    return render(
        request,
        "appointments/visit_new.html",
        visit_context(
            request,
            form=form,
            patient_form=patient_form,
            vitals_form=vitals_form,
            new_mode=new_mode,
        ),
    )


def visit_context(request, *, form=None, patient_form=None, vitals_form=None, new_mode=False):
    """What the booking form needs. The reception desk shows it empty."""
    today = timezone.localdate()
    if form is None:
        form = VisitForm(
            initial={"date": today, "patient": request.GET.get("patient", "")}, request=request
        )
    if patient_form is None:
        patient_form = _quick_patient_form(request)
    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    return {
        "form": form,
        "patient_form": patient_form,
        "vitals_form": vitals_form or VitalsForm(prefix="v"),
        "patient": patient,
        "new_mode": new_mode,
        "doctors": _doctor_rows(request),
        "methods": form.fields["method"].queryset,
        "prefs": preferences_for(request.organization),
        "shifts": DoctorSchedule.Shift.choices,
        "sources": Appointment.Source.choices,
        "today": today,
        "tomorrow": today + timedelta(days=1),
        "can_collect": has_perm(request, visits.COLLECT),
        "can_discount": has_perm(request, "billing.discount"),
        "cash_limit": billing_services.settings_for(request.organization).cash_adjustment_limit,
        "can_vitals": has_perm(request, "vitals.record"),
        "can_register": has_perm(request, "patient.create"),
        "idempotency_key": request.POST.get("key") or uuid.uuid4().hex,
    }


@require_GET
@permission_required(VIEW)
def papers(request, pk):
    """What a visit prints: token and fee copies on the roll; ?part=rx is the A5 prescription."""
    appt = get_scoped_or_404(_appointments(request), request, pk=pk)
    prefs = preferences_for(request.organization)
    can_view_bills = has_perm(request, "billing.view")
    invoice = visits.fee_invoice(appt) if can_view_bills else None
    if invoice is not None and invoice.is_draft:
        invoice = None
    latest = vitals_services.latest_today(appt.patient, request.organization)
    other_bills = []
    if can_view_bills:
        other_bills = (
            scoped(Invoice, request)
            .filter(patient=appt.patient, status__in=Invoice.OUTSTANDING_STATUSES)
            .exclude(pk=invoice.pk if invoice else None)
        )
    due = bool(invoice and invoice.balance_due)
    wants_print = request.GET.get("print") == "1"
    rx_only = request.GET.get("part") == "rx"
    token = getattr(appt, "token", None)
    has_slip = bool(invoice or token)
    emergency = appt.visit_type == Appointment.VisitType.EMERGENCY  # treated first, paid later
    token_printers = printing_direct.printers_for(request.organization)
    return render(
        request,
        "appointments/papers_rx.html" if rx_only else "appointments/papers.html",
        {
            "appt": appt,
            "patient": appt.patient,
            "token": token,
            **(queue_services.position(token) if token else {}),
            "invoice": invoice,
            "items": invoice.items.all() if invoice else [],
            "vitals": vitals_services.summary(latest, appt.patient) if latest else [],
            "allergies": appt.patient.allergies.filter(entered_in_error=False),
            "prefs": prefs,
            "other_bills": other_bills,
            "due": due,
            "can_collect": has_perm(request, visits.COLLECT),
            "can_discount": has_perm(request, "billing.discount"),
            "can_refund": has_perm(request, "billing.refund"),
            "token_printers": token_printers,
            "token_printer": printing_direct.chosen(request),
            "can_set_printers": has_perm(request, "admin.organization.manage"),
            "cash_limit": billing_services.settings_for(request.organization).cash_adjustment_limit,
            "methods": billing_services.active_methods(request.organization),
            "key": uuid.uuid4().hex,
            "auto_print": wants_print
            and (rx_only or (prefs.opd_auto_print and has_slip and (not due or emergency))),
            "billing_settings": billing_services.settings_for(request.organization),
        },
    )


def _audit_fee(request, invoice, payment):
    invoice.refresh_from_db()
    audit.record(request, "billing.payment", invoice, summary=payment.number)
    if invoice.invoice_discount:
        audit.record(
            request,
            "billing.discount",
            invoice,
            summary=f"{invoice.invoice_discount}: {invoice.discount_reason}",
        )
    if invoice.cash_adjustment:
        audit.record(
            request,
            "billing.cash_adjustment",
            invoice,
            summary=f"{invoice.number}: {invoice.cash_adjustment}",
        )


def _money(value):
    try:
        return Decimal(value) if str(value or "").strip() else None
    except ArithmeticError:
        raise billing_services.BillingError("Enter valid amounts.") from None


@require_POST
@permission_required(visits.COLLECT)
def collect(request, pk):
    """Take the doctor's fee from the papers page, then print."""
    appt = get_scoped_or_404(Appointment, request, pk=pk)
    invoice = visits.fee_invoice(appt)
    method = (
        billing_services.active_methods(request.organization)
        .filter(pk=request.POST.get("method") or 0)
        .first()
    )
    if invoice is None or method is None:
        messages.error(request, "Choose how the fee was paid.")
        return redirect("appointments:papers", pk=pk)
    try:
        payment = visits.collect_fee(
            invoice,
            request=request,
            method=method,
            reference=request.POST.get("reference", ""),
            key=request.POST.get("key", ""),
            cash_paid=_money(request.POST.get("cash_paid")),
            discount=_money(request.POST.get("discount")) or 0,
            reason=request.POST.get("discount_reason", ""),
        )
    except (billing_services.BillingError, services.BookingError) as err:
        messages.error(request, err.message)
        return redirect("appointments:papers", pk=pk)
    _audit_fee(request, invoice, payment)
    return redirect(f"{reverse('appointments:papers', args=[pk])}?print=1")


@require_POST
@permission_required(MANAGE)
def arrive(request, pk):
    """A booked patient has arrived: token, fee invoice and straight to the papers."""
    appt = get_scoped_or_404(Appointment, request, pk=pk)
    before = appt.status
    try:
        visit = visits.arrive(appt, request=request)
    except (
        services.BookingError,
        visits.queue_services.QueueError,
        billing_services.BillingError,
    ) as err:
        messages.error(request, err.message)
        return redirect(request.POST.get("next") or reverse("appointments:detail", args=[pk]))
    audit.record(
        request,
        "appointment.check_in",
        appt,
        changes={"status": [before, visit.appointment.status]},
    )
    audit.record(request, "queue.issue", visit.token, summary=visit.token.label)
    return redirect(f"{reverse('appointments:papers', args=[pk])}?print=1")


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
        ctx["use_shifts"] = preferences_for(request.organization).use_doctor_shifts
        shift = request.GET.get("shift", "")
        if shift:
            ctx["slots"] = [s for s in ctx["slots"] if s.shift == shift]
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
