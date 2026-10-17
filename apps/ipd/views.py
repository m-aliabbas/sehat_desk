"""IPD screens: bed board, inpatient list, admission, inpatient chart, transfer, discharge.

Clinical text is never written to the audit log; entries name the action and record numbers.
"""

import uuid
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, paginate, scoped
from apps.patients.models import Patient

from . import services
from .forms import (
    AddBedsForm,
    AdmissionForm,
    BedStatusForm,
    ChargeForm,
    DepositForm,
    DischargeForm,
    MedicationOrderForm,
    ReasonForm,
    RoomForm,
    RoundForm,
    TransferForm,
    WardForm,
    medicines_from_orders,
    parse_discharge_medicines,
)
from .models import Admission, Bed, DoctorRound, MedicationOrder, Room, Ward

IPD = ("ipd.manage", "nursing.notes")
FACILITY = "ipd.facility.manage"

CHART_TABS = [
    ("overview", "Overview"),
    ("rounds", "Doctor rounds"),
    ("nursing", "Nursing"),
    ("vitals", "Vitals"),
    ("medication", "Medication chart"),
    ("orders", "Lab & prescriptions"),
    ("billing", "Billing"),
    ("transfers", "Bed history"),
    ("documents", "Documents"),
]


def _crumbs(*extra):
    return [{"label": "IPD & wards", "url": reverse("ipd:board")}, *extra]


def _get_admission(request, pk):
    return get_scoped_or_404(
        Admission.objects.select_related(
            "patient", "admitting_doctor__user", "department", "current_bed__room__ward", "invoice"
        ),
        request,
        pk=pk,
    )


def _chart(admission, tab=None):
    url = reverse("ipd:admission-detail", args=[admission.pk])
    return f"{url}?tab={tab}" if tab else url


def _error(request, err):
    messages.error(request, getattr(err, "message", None) or " ".join(err.messages))


# --- Bed board and lists -----------------------------------------------------------------------


@require_GET
@permission_required(*IPD, FACILITY, any_of=True)
def board(request):
    """Bed board (design screen 7): every ward's beds with their status and patient.

    Administrators who only set up wards and beds (no patient access) land on Setup."""
    if not any(has_perm(request, c) for c in IPD):
        return redirect("ipd:setup")
    ward_filter = request.GET.get("ward", "")
    status_filter = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    overview = services.ward_overview(request.branch)
    if ward_filter.isdigit():
        overview = [(w, beds) for w, beds in overview if w.pk == int(ward_filter)]
    sections = []
    for ward, beds in overview:
        stats = services.occupancy(Bed.objects.filter(pk__in=[b.pk for b in beds]))
        shown = beds
        if status_filter:
            shown = [b for b in shown if b.status == status_filter]
        if q:
            ql = q.lower()
            shown = [
                b
                for b in shown
                if ql in b.code.lower()
                or (
                    b.admission
                    and (
                        ql in b.admission.patient.full_name.lower()
                        or ql in b.admission.patient.mrn.lower()
                    )
                )
            ]
        sections.append({"ward": ward, "beds": shown, "stats": stats})
    all_beds = Bed.objects.filter(
        branch=request.branch, is_active=True, room__is_active=True, room__ward__is_active=True
    )
    return render(
        request,
        "ipd/board.html",
        {
            "sections": sections,
            "stats": services.occupancy(all_beds),
            "wards": Ward.objects.filter(branch=request.branch, is_active=True),
            "statuses": Bed.Status.choices,
            "filters": {"ward": ward_filter, "status": status_filter, "q": q},
            "discharges_today": services.discharges_today(request.branch),
            "planned": scoped(Admission, request)
            .filter(status=Admission.Status.DISCHARGE_PLANNED)
            .count(),
            "status_form": BedStatusForm(),
            "can_manage": has_perm(request, "ipd.manage"),
            "can_setup": has_perm(request, FACILITY),
        },
    )


@require_GET
@permission_required(*IPD, any_of=True)
def admission_list(request):
    """Inpatient dashboard: current (or past) admissions in this branch."""
    state = request.GET.get("state", "current")
    q = request.GET.get("q", "").strip()
    qs = scoped(Admission, request).select_related(
        "patient", "admitting_doctor__user", "department", "current_bed__room__ward"
    )
    if state == "current":
        qs = qs.filter(status__in=Admission.ACTIVE_STATUSES)
    elif state == "discharged":
        qs = qs.filter(status=Admission.Status.DISCHARGED)
    if request.GET.get("ward", "").isdigit():
        qs = qs.filter(current_bed__room__ward_id=request.GET["ward"])
    if q:
        qs = qs.filter(
            Q(patient__full_name__icontains=q)
            | Q(patient__mrn__icontains=q)
            | Q(number__icontains=q)
        )
    return render(
        request,
        "ipd/admission_list.html",
        {
            "admissions": (page := paginate(request, qs.order_by("-admitted_at"))),
            "page_obj": page,
            "state": state,
            "q": q,
            "wards": Ward.objects.filter(branch=request.branch, is_active=True),
            "ward": request.GET.get("ward", ""),
            "breadcrumbs": _crumbs({"label": "Inpatients"}),
        },
    )


# --- Admission ---------------------------------------------------------------------------------


@permission_required("ipd.manage")
def admit(request):
    """Admission form (design screen 24). Without ?patient=, search for the patient first."""
    source = request.POST if request.method == "POST" else request.GET
    patient = None
    if source.get("patient", "").isdigit():
        patient = get_scoped_or_404(Patient, request, pk=source["patient"])
    if patient is None:
        q = request.GET.get("q", "").strip()
        results = []
        if len(q) >= 2:
            results = (
                scoped(Patient, request)
                .filter(is_active=True)
                .filter(Q(full_name__icontains=q) | Q(mrn__icontains=q) | Q(phone__icontains=q))
                .order_by("full_name")[:20]
            )
        return render(
            request,
            "ipd/admit_pick.html",
            {"q": q, "results": results, "breadcrumbs": _crumbs({"label": "Admit patient"})},
        )
    open_stay = services.current_admission(patient, request.organization)
    if open_stay:
        messages.info(request, f"{patient.full_name} is already admitted ({open_stay.number}).")
        if open_stay.branch_id == request.branch.pk:
            return redirect("ipd:admission-detail", pk=open_stay.pk)
        return redirect("patients:detail", pk=patient.pk)
    initial = {"admission_reason": "", "attendant_name": patient.emergency_contact_name,
               "attendant_relation": patient.emergency_contact_relation,
               "attendant_phone": patient.emergency_contact_phone}  # fmt: skip
    if request.GET.get("bed", "").isdigit():
        initial["bed"] = request.GET["bed"]
    form = AdmissionForm(request.POST or None, request=request, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            admission = services.admit(
                request=request,
                patient=patient,
                doctor=c["doctor"],
                department=c["department"],
                bed=c["bed"],
                reason=c["admission_reason"],
                diagnosis=c["admission_diagnosis"],
                notes=c["notes"],
                attendant={
                    "name": c["attendant_name"],
                    "relation": c["attendant_relation"],
                    "phone": c["attendant_phone"],
                },
            )
            deposit = None
            if form.can_take_deposit and c.get("deposit_amount"):
                deposit = services.record_deposit(
                    admission,
                    request=request,
                    method=c["deposit_method"],
                    amount=c["deposit_amount"],
                    reference=c["deposit_reference"],
                )
        except services.IpdError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "ipd.admit",
                admission,
                summary=f"{admission.number} {patient.mrn} bed {c['bed'].code}",
            )
            if deposit:
                audit.record(
                    request, "ipd.deposit", admission, summary=f"{deposit.number} {deposit.amount}"
                )
            messages.success(
                request, f"{patient.full_name} admitted to {c['bed'].code} ({admission.number})."
            )
            return redirect("ipd:admission-detail", pk=admission.pk)
    return render(
        request,
        "ipd/admit.html",
        {
            "form": form,
            "patient": patient,
            "allergies": patient.allergies.filter(entered_in_error=False),
            "breadcrumbs": _crumbs({"label": "Admit patient"}),
        },
    )


@require_GET
@permission_required(*IPD, any_of=True)
def admission_detail(request, pk):
    """Inpatient chart. Tabs load only the data they show."""
    admission = _get_admission(request, pk)
    tab = request.GET.get("tab", "overview")
    if tab not in dict(CHART_TABS):
        tab = "overview"
    patient = admission.patient
    audit.record(request, "ipd.chart.view", admission, summary=f"{admission.number} {tab}")
    ctx = {
        "admission": admission,
        "patient": patient,
        "tab": tab,
        "tabs": CHART_TABS,
        "allergies": patient.allergies.filter(entered_in_error=False),
        "can_manage": has_perm(request, "ipd.manage"),
        "can_nurse": has_perm(request, "nursing.notes"),
        "can_round": has_perm(request, "consultation.create")
        and getattr(request.user, "doctor_profile", None) is not None,
        "can_order": has_perm(request, "prescription.create"),
        "can_collect": has_perm(request, "billing.payment.collect"),
        "now": timezone.now(),
        "breadcrumbs": _crumbs(
            {"label": "Inpatients", "url": reverse("ipd:admission-list")},
            {"label": f"{admission.number} · {patient.full_name}"},
        ),
    }
    from apps.nursing import services as nursing
    from apps.vitals import services as vitals

    latest = nursing.last_vitals(admission)
    ctx["latest_vitals"] = latest
    ctx["latest_vitals_summary"] = vitals.summary(latest, patient) if latest else []
    ctx["vitals_due"] = nursing.vitals_due_at(admission, latest)
    if tab == "overview":
        ctx["active_orders"] = admission.medication_orders.filter(
            status=MedicationOrder.Status.ACTIVE
        )
        ctx["recent_rounds"] = admission.rounds.filter(entered_in_error=False).select_related(
            "doctor__user"
        )[:3]
        ctx["recent_notes"] = admission.nursing_notes.filter(entered_in_error=False).select_related(
            "recorded_by"
        )[:3]
        ctx["balance"] = nursing.fluid_balance(admission)
        ctx["nurses"] = admission.nurse_assignments.filter(ended_at__isnull=True).select_related(
            "nurse"
        )
    elif tab == "rounds":
        ctx["rounds"] = admission.rounds.select_related("doctor__user", "error_marked_by")
        ctx["round_form"] = RoundForm()
    elif tab == "nursing":
        from apps.accounts.models import User
        from apps.nursing.forms import IntakeOutputForm, NursingNoteForm, VitalsScheduleForm

        ctx["notes"] = admission.nursing_notes.select_related("recorded_by", "error_marked_by")
        ctx["io_rows"] = admission.intake_output.select_related("recorded_by")[:50]
        ctx["balance"] = nursing.fluid_balance(admission)
        ctx["note_form"] = NursingNoteForm()
        ctx["io_form"] = IntakeOutputForm()
        schedule = getattr(admission, "vitals_schedule", None)
        ctx["schedule_form"] = VitalsScheduleForm(
            initial={"interval_hours": schedule.interval_hours if schedule else 6}
        )
        ctx["schedule"] = schedule
        ctx["nurses"] = admission.nurse_assignments.filter(ended_at__isnull=True).select_related(
            "nurse"
        )
        ctx["staff"] = (
            User.objects.filter(organization=request.organization, is_active=True)
            .filter(
                assignments__role__permissions__code="nursing.notes",
            )
            .distinct()
            .order_by("first_name")
        )
        ctx["trail"] = audit_trail(admission)
    elif tab == "vitals":
        from apps.vitals.models import VitalSign

        readings = VitalSign.objects.filter(
            organization=request.organization,
            patient=patient,
            recorded_at__gte=admission.admitted_at,
        )
        if admission.stay_end:
            readings = readings.filter(recorded_at__lte=admission.stay_end)
        ctx["vital_rows"] = [
            (v, vitals.summary(v, patient))
            for v in readings.select_related("recorded_by").order_by("-recorded_at")[:100]
        ]
        ctx["vital_trends"] = _stay_trends(admission, readings)
    elif tab == "medication":
        ctx["orders"] = admission.medication_orders.select_related("ordered_by", "stopped_by")
        ctx["order_form"] = MedicationOrderForm(organization=request.organization)
        ctx["medicine_names"] = _medicine_names(request)
        day = _day(request)
        ctx["mar_day"] = day
        ctx["mar_prev"] = day - timedelta(days=1)
        ctx["mar_next"] = day + timedelta(days=1)
        ctx["mar"] = nursing.mar_for_day(admission, day)
    elif tab == "orders":
        from apps.lab.models import LabOrder
        from apps.prescriptions.models import Prescription

        lab = LabOrder.objects.filter(
            organization=request.organization,
            patient=patient,
            created_at__gte=admission.admitted_at,
        )
        rx = Prescription.objects.filter(
            organization=request.organization,
            patient=patient,
            created_at__gte=admission.admitted_at,
        )
        if admission.stay_end:
            lab = lab.filter(created_at__lte=admission.stay_end)
            rx = rx.filter(created_at__lte=admission.stay_end)
        ctx["lab_orders"] = lab.select_related("doctor__user").prefetch_related("items__test")
        ctx["prescriptions"] = rx.select_related("doctor__user").prefetch_related("items")
        ctx["can_order_lab"] = has_perm(request, "lab.order.create")
    elif tab == "billing":
        ctx["money"] = services.billing_summary(admission)
        ctx["items"] = admission.invoice.items.all() if admission.invoice_id else []
        ctx["charge_form"] = ChargeForm(organization=request.organization)
        ctx["deposit_form"] = DepositForm(
            organization=request.organization, initial={"idempotency_key": uuid.uuid4().hex}
        )
        ctx["can_view_billing"] = has_perm(request, "billing.view")
    elif tab == "transfers":
        ctx["transfers"] = admission.transfers.select_related(
            "from_bed__room__ward", "to_bed__room__ward", "transferred_by"
        )
        ctx["occupancies"] = admission.occupancies.select_related("bed__room__ward")
    elif tab == "documents":
        from apps.documents import services as documents

        ctx.update(documents.panel_context(request, patient, admission=admission))
    return render(request, "ipd/chart.html", ctx)


def _day(request):
    from datetime import date

    try:
        return date.fromisoformat(request.GET.get("day", ""))
    except ValueError:
        return timezone.localdate()


def _medicine_names(request):
    from apps.prescriptions.models import Medicine

    return [
        m.display_name
        for m in Medicine.objects.filter(organization=request.organization, is_active=True)[:500]
    ]


def _stay_trends(admission, readings):
    from apps.vitals import services as vitals

    rows = list(readings.filter(entered_in_error=False).order_by("recorded_at")[:200])
    result = []
    for field, label, unit in vitals.TREND_MEASURES:
        points = [
            (v.recorded_at, float(getattr(v, field)),
             vitals.assess(v, admission.patient).get(field, vitals.NORMAL))
            for v in rows
            if getattr(v, field) is not None
        ][-24:]  # fmt: skip
        if points:
            result.append({"field": field, "label": label, "unit": unit, "points": points,
                           "latest": points[-1]})  # fmt: skip
    return result


def audit_trail(admission, limit=100):
    from apps.audit.models import AuditLog

    return (
        AuditLog.objects.filter(
            organization=admission.organization, entity="Admission", record_id=str(admission.pk)
        )
        .exclude(action="ipd.chart.view")
        .select_related("actor")[:limit]
    )


@require_GET
@permission_required(*IPD, any_of=True)
def admission_print(request, pk):
    admission = _get_admission(request, pk)
    audit.record(request, "ipd.admission.print", admission, summary=admission.number)
    return render(
        request,
        "ipd/admission_print.html",
        {
            "admission": admission,
            "patient": admission.patient,
            "deposits": admission.deposits.select_related("method"),
            "allergies": admission.patient.allergies.filter(entered_in_error=False),
        },
    )


@require_POST
@permission_required("ipd.manage")
def admission_cancel(request, pk):
    admission = _get_admission(request, pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give the reason for cancelling the admission.")
        return redirect(_chart(admission))
    try:
        services.cancel_admission(admission, request=request, reason=form.cleaned_data["reason"])
    except services.IpdError as err:
        _error(request, err)
        return redirect(_chart(admission))
    audit.record(request, "ipd.admission.cancel", admission, summary=admission.number)
    messages.success(request, f"{admission.number} cancelled. The bed is free again.")
    return redirect("ipd:board")


# --- Transfers ---------------------------------------------------------------------------------


@permission_required("ipd.manage")
def transfer(request, pk):
    """Bed or ward transfer (design screen 25)."""
    admission = _get_admission(request, pk)
    form = TransferForm(request.POST or None, branch=request.branch)
    if request.method == "POST" and form.is_valid():
        try:
            move = services.transfer(
                admission,
                request=request,
                to_bed=form.cleaned_data["to_bed"],
                reason=form.cleaned_data["reason"],
            )
        except services.IpdError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "ipd.transfer",
                admission,
                changes={"bed": [move.from_bed.code, move.to_bed.code]},
                summary=f"{admission.number} {move.get_kind_display()}",
            )
            messages.success(
                request, f"Moved to {move.to_bed.code}. {move.from_bed.code} needs cleaning."
            )
            return redirect(_chart(admission, "transfers"))
    return render(
        request,
        "ipd/transfer.html",
        {
            "admission": admission,
            "form": form,
            "breadcrumbs": _crumbs(
                {"label": admission.number, "url": _chart(admission)}, {"label": "Transfer"}
            ),
        },
    )


# --- Chart entries -----------------------------------------------------------------------------


@require_POST
@permission_required("consultation.create")
def round_add(request, pk):
    admission = _get_admission(request, pk)
    form = RoundForm(request.POST)
    if form.is_valid():
        try:
            note = services.add_round(admission, request=request, **form.cleaned_data)
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(request, "ipd.round.add", admission, summary=f"round_id={note.pk}")
            messages.success(request, "Round note saved.")
    else:
        messages.error(request, "Write the round findings.")
    return redirect(_chart(admission, "rounds"))


@require_POST
@permission_required("consultation.create")
def round_error(request, pk):
    note = get_object_or_404(
        DoctorRound,
        pk=pk,
        admission__organization=request.organization,
        admission__branch=request.branch,
    )
    form = ReasonForm(request.POST)
    if form.is_valid():
        try:
            services.mark_round_error(note, request=request, reason=form.cleaned_data["reason"])
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(request, "ipd.round.error", note.admission, summary=f"round_id={note.pk}")
            messages.success(request, "Round note marked as entered in error. It stays on record.")
    else:
        messages.error(request, "Give the reason the note is wrong.")
    return redirect(_chart(note.admission, "rounds"))


@require_POST
@permission_required("prescription.create")
def medication_add(request, pk):
    admission = _get_admission(request, pk)
    form = MedicationOrderForm(request.POST, organization=request.organization)
    if form.is_valid():
        try:
            order = services.order_medication(admission, request=request, data=form.order_data())
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(request, "ipd.medication.order", admission, summary=f"order_id={order.pk}")
            messages.success(request, f"{order.medicine_name} added to the medication chart.")
    else:
        messages.error(request, "Give the medicine, dose and frequency.")
    return redirect(_chart(admission, "medication"))


@require_POST
@permission_required("prescription.create")
def medication_stop(request, pk):
    order = get_object_or_404(
        MedicationOrder,
        pk=pk,
        admission__organization=request.organization,
        admission__branch=request.branch,
    )
    form = ReasonForm(request.POST)
    if form.is_valid():
        try:
            services.stop_medication(order, request=request, reason=form.cleaned_data["reason"])
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(
                request, "ipd.medication.stop", order.admission, summary=f"order_id={order.pk}"
            )
            messages.success(request, f"{order.medicine_name} stopped.")
    else:
        messages.error(request, "Give the reason for stopping the medicine.")
    return redirect(_chart(order.admission, "medication"))


@require_POST
@permission_required("ipd.manage")
def charge_add(request, pk):
    admission = _get_admission(request, pk)
    form = ChargeForm(request.POST, organization=request.organization)
    if form.is_valid():
        c = form.cleaned_data
        try:
            item = services.add_charge(
                admission,
                request=request,
                service=c["service"],
                description=c["description"],
                unit_price=c["unit_price"],
                quantity=c["quantity"],
                kind=c["kind"],
            )
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(
                request, "ipd.charge.add", admission, summary=f"{item.description} ×{item.quantity}"
            )
            messages.success(request, f"{item.description} added to the bill.")
    else:
        messages.error(request, " ".join(form.non_field_errors()) or "Check the charge details.")
    return redirect(_chart(admission, "billing"))


@require_POST
@permission_required("billing.payment.collect")
def deposit_add(request, pk):
    admission = _get_admission(request, pk)
    form = DepositForm(request.POST, organization=request.organization)
    if form.is_valid():
        c = form.cleaned_data
        try:
            dep = services.record_deposit(
                admission,
                request=request,
                method=c["method"],
                amount=c["amount"],
                reference=c["reference"],
                idempotency_key=c["idempotency_key"],
            )
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(request, "ipd.deposit", admission, summary=f"{dep.number} {dep.amount}")
            messages.success(request, f"Deposit {dep.number} recorded.")
    else:
        messages.error(request, "Enter the amount and how it was paid.")
    return redirect(_chart(admission, "billing"))


# --- Discharge ---------------------------------------------------------------------------------


@permission_required(*IPD, "consultation.create", "billing.payment.collect", any_of=True)
def discharge(request, pk):
    """Discharge workspace (design screens 26, 119–121): clinical summary, pending services,
    final bill, release. Each POST names its step in `action`."""
    admission = _get_admission(request, pk)
    action = request.POST.get("action", "")
    form = DischargeForm(
        request.POST if action == "save" else None, instance=Admission.objects.get(pk=admission.pk)
    )
    if request.method == "POST":
        try:
            if action == "save":
                if not has_perm(request, "consultation.create"):
                    raise PermissionDenied
                if form.is_valid():
                    data = dict(form.cleaned_data)
                    data["discharge_medicines"] = parse_discharge_medicines(request.POST)
                    services.save_discharge_draft(admission, request=request, data=data)
                    audit.record(
                        request, "ipd.discharge.draft", admission, summary=admission.number
                    )
                    if request.POST.get("plan"):
                        services.plan_discharge(admission, request=request)
                        audit.record(
                            request, "ipd.discharge.plan", admission, summary=admission.number
                        )
                        messages.success(request, "Patient cleared for discharge.")
                    else:
                        messages.success(request, "Discharge summary saved.")
                    return redirect("ipd:discharge", pk=pk)
                messages.error(request, "Check the highlighted fields.")
            elif action == "revert":
                services.revert_discharge_plan(admission, request=request)
                audit.record(request, "ipd.discharge.revert", admission, summary=admission.number)
                messages.success(request, "Discharge plan withdrawn. The stay continues.")
                return redirect("ipd:discharge", pk=pk)
            elif action == "finalize":
                services.finalize_bill(admission, request=request)
                audit.record(request, "ipd.bill.finalize", admission, summary=admission.number)
                messages.success(request, "Final bill issued and deposits applied.")
                return redirect("ipd:discharge", pk=pk)
            elif action == "return_deposit":
                amount = services.return_deposit_balance(admission, request=request)
                audit.record(request, "ipd.deposit.return", admission, summary=f"{amount}")
                messages.success(request, f"{amount:,.2f} of unused deposit returned.")
                return redirect("ipd:discharge", pk=pk)
            elif action == "release":
                services.release(
                    admission, request=request, override_reason=request.POST.get("override", "")
                )
                audit.record(request, "ipd.discharge", admission, summary=admission.number)
                messages.success(
                    request,
                    f"{admission.patient.full_name} discharged. The bed is set to cleaning.",
                )
                return redirect("ipd:summary", pk=pk)
            else:
                messages.error(request, "Unknown step.")
        except services.IpdError as err:
            _error(request, err)
            if action != "save":
                return redirect("ipd:discharge", pk=pk)
    admission.refresh_from_db()
    medicines = admission.discharge_medicines or medicines_from_orders(admission)
    return render(
        request,
        "ipd/discharge.html",
        {
            "admission": admission,
            "patient": admission.patient,
            "form": form,
            "medicines": medicines,
            "check": services.checklist(admission),
            "can_write": has_perm(request, "consultation.create"),
            "can_collect": has_perm(request, "billing.payment.collect"),
            "can_refund": has_perm(request, "billing.refund"),
            "can_release": has_perm(request, "ipd.manage"),
            "can_override": has_perm(request, "ipd.discharge.override"),
            "breadcrumbs": _crumbs(
                {"label": admission.number, "url": _chart(admission)}, {"label": "Discharge"}
            ),
        },
    )


@require_GET
@permission_required(*IPD, "consultation.view", any_of=True)
def summary(request, pk):
    """Printable discharge summary: the signed version (or ?v=), else the draft, watermarked."""
    admission = _get_admission(request, pk)
    versions = list(admission.summary_versions.select_related("signed_by"))
    chosen = None
    if versions:
        v = request.GET.get("v", "")
        chosen = next((s for s in versions if str(s.version) == v), versions[0])
    content = chosen.content if chosen else services.summary_content(admission)
    audit.record(
        request,
        "ipd.summary.view",
        admission,
        summary=f"{admission.number} v{chosen.version if chosen else 'draft'}",
    )
    return render(
        request,
        "ipd/summary_print.html",
        {
            "admission": admission,
            "patient": admission.patient,
            "content": content,
            "version": chosen,
            "versions": versions,
            "allergies": admission.patient.allergies.filter(entered_in_error=False),
        },
    )


@permission_required("consultation.create")
def amend(request, pk):
    admission = _get_admission(request, pk)
    if admission.status != Admission.Status.DISCHARGED:
        messages.error(request, "Only a discharged stay has a signed summary to amend.")
        return redirect(_chart(admission))
    form = DischargeForm(request.POST or None, instance=Admission.objects.get(pk=admission.pk))
    reason = request.POST.get("reason", "").strip()
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        data["discharge_medicines"] = parse_discharge_medicines(request.POST)
        try:
            version = services.amend_summary(admission, request=request, data=data, reason=reason)
        except services.IpdError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request, "ipd.summary.amend", admission, summary=f"v{version.version}: {reason}"
            )
            messages.success(request, f"Discharge summary version {version.version} signed.")
            return redirect("ipd:summary", pk=pk)
    return render(
        request,
        "ipd/amend.html",
        {
            "admission": admission,
            "form": form,
            "reason": reason,
            "medicines": admission.discharge_medicines,
            "breadcrumbs": _crumbs(
                {"label": admission.number, "url": _chart(admission)},
                {"label": "Amend discharge summary"},
            ),
        },
    )


# --- Beds and facility setup -------------------------------------------------------------------


@require_POST
@permission_required("ipd.manage")
def bed_status(request, pk):
    bed = get_scoped_or_404(Bed, request, pk=pk)
    form = BedStatusForm(request.POST)
    if form.is_valid():
        before = bed.status
        try:
            services.set_bed_status(bed, request=request, **form.cleaned_data)
        except services.IpdError as err:
            _error(request, err)
        else:
            audit.record(
                request,
                "ipd.bed.status",
                bed,
                changes={"status": [before, form.cleaned_data["status"]]},
            )
            messages.success(
                request,
                f"{bed.code} is now {Bed.Status(form.cleaned_data['status']).label.lower()}.",
            )
    else:
        messages.error(request, "Choose a bed status.")
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "ipd:board")


@require_GET
@permission_required(*IPD, any_of=True)
def bed_detail(request, pk):
    bed = get_scoped_or_404(Bed.objects.select_related("room__ward"), request, pk=pk)
    return render(
        request,
        "ipd/bed_detail.html",
        {
            "bed": bed,
            "history": bed.occupancies.select_related("admission__patient").order_by("-started_at")[
                :50
            ],
            "status_form": BedStatusForm(initial={"status": bed.status}),
            "can_manage": has_perm(request, "ipd.manage"),
            "breadcrumbs": _crumbs({"label": f"Bed {bed.code}"}),
        },
    )


@permission_required(FACILITY)
def setup(request):
    """Wards, rooms and beds of this branch."""
    org, branch = request.organization, request.branch
    kind = request.POST.get("form", "")
    ward_form = WardForm(request.POST if kind == "ward" else None, organization=org, prefix="ward")
    room_form = RoomForm(request.POST if kind == "room" else None, branch=branch, prefix="room")
    beds_form = AddBedsForm(request.POST if kind == "beds" else None, branch=branch, prefix="beds")
    if request.method == "POST":
        if kind == "ward" and ward_form.is_valid():
            ward = ward_form.save(commit=False)
            ward.organization, ward.branch = org, branch
            if Ward.objects.filter(branch=branch, code=ward.code).exists():
                ward_form.add_error("code", "Another ward in this branch uses this code.")
            else:
                ward.save()
                audit.record(request, "ipd.ward.create", ward, summary=ward.name)
                messages.success(request, f"Ward {ward.name} added.")
                return redirect("ipd:setup")
        elif kind == "room" and room_form.is_valid():
            room = room_form.save(commit=False)
            room.organization, room.branch = org, branch
            if Room.objects.filter(ward=room.ward, name=room.name).exists():
                room_form.add_error("name", "This ward already has a room with this name.")
            else:
                room.save()
                audit.record(request, "ipd.room.create", room, summary=str(room))
                messages.success(request, f"Room {room} added.")
                return redirect("ipd:setup")
        elif kind == "beds" and beds_form.is_valid():
            c = beds_form.cleaned_data
            try:
                beds = services.add_beds(
                    c["room"], prefix=c["prefix"], count=c["count"], start=c["start"]
                )
            except services.IpdError as err:
                beds_form.add_error(None, err.message)
            else:
                audit.record(
                    request,
                    "ipd.beds.create",
                    c["room"],
                    summary=", ".join(b.code for b in beds)[:255],
                )
                messages.success(request, f"{len(beds)} bed(s) added to {c['room']}.")
                return redirect("ipd:setup")
    wards = Ward.objects.filter(branch=branch).prefetch_related("rooms__beds")
    return render(
        request,
        "ipd/setup.html",
        {
            "wards": wards,
            "ward_form": ward_form,
            "room_form": room_form,
            "beds_form": beds_form,
            "breadcrumbs": _crumbs({"label": "Wards, rooms & beds"}),
        },
    )


@permission_required(FACILITY)
def ward_edit(request, pk):
    ward = get_scoped_or_404(Ward, request, pk=pk)
    form = WardForm(request.POST or None, instance=ward, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        clash = Ward.objects.filter(branch=ward.branch, code=form.cleaned_data["code"]).exclude(
            pk=ward.pk
        )
        if clash.exists():
            form.add_error("code", "Another ward in this branch uses this code.")
        elif (
            not form.cleaned_data["is_active"]
            and Admission.objects.filter(
                current_bed__room__ward=ward, status__in=Admission.ACTIVE_STATUSES
            ).exists()
        ):
            form.add_error("is_active", "Patients are admitted in this ward. Transfer them first.")
        else:
            form.save()
            audit.record(request, "ipd.ward.update", ward, summary=ward.name)
            messages.success(request, "Ward saved.")
            return redirect("ipd:setup")
    return render(
        request,
        "ipd/facility_form.html",
        {"form": form, "title": f"Edit ward · {ward.name}",
         "breadcrumbs": _crumbs({"label": "Wards, rooms & beds", "url": reverse("ipd:setup")},
                                {"label": ward.name})},
    )  # fmt: skip


@permission_required(FACILITY)
def room_edit(request, pk):
    room = get_scoped_or_404(Room.objects.select_related("ward"), request, pk=pk)
    form = RoomForm(request.POST or None, instance=room, branch=request.branch)
    if request.method == "POST" and form.is_valid():
        if (
            not form.cleaned_data["is_active"]
            and room.beds.filter(status=Bed.Status.OCCUPIED).exists()
        ):
            form.add_error("is_active", "Patients are in this room. Transfer them first.")
        else:
            form.save()
            audit.record(request, "ipd.room.update", room, summary=str(room))
            messages.success(request, "Room saved. New admissions use the new bed charge.")
            return redirect("ipd:setup")
    return render(
        request,
        "ipd/facility_form.html",
        {"form": form, "title": f"Edit room · {room}",
         "breadcrumbs": _crumbs({"label": "Wards, rooms & beds", "url": reverse("ipd:setup")},
                                {"label": str(room)})},
    )  # fmt: skip


@require_POST
@permission_required(FACILITY)
def bed_toggle(request, pk):
    bed = get_scoped_or_404(Bed, request, pk=pk)
    if bed.is_active and bed.status == Bed.Status.OCCUPIED:
        messages.error(request, f"{bed.code} is occupied. Transfer the patient first.")
    else:
        bed.is_active = not bed.is_active
        bed.save(update_fields=["is_active", "updated_at"])
        audit.record(
            request,
            "ipd.bed.toggle",
            bed,
            changes={"is_active": [not bed.is_active, bed.is_active]},
        )
        messages.success(
            request, f"{bed.code} {'back in use' if bed.is_active else 'taken out of use'}."
        )
    return redirect("ipd:setup")
