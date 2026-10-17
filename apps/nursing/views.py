"""Nursing workspace (design screen 21) and the nursing actions on the inpatient chart.

Each action is audited against the admission (entity "Admission"), which makes the chart's
nursing audit trail. Clinical text never goes into the audit entry.
"""

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.accounts.models import User
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, scoped
from apps.ipd.models import Admission, MedicationOrder, Ward

from . import services
from .forms import (
    DoseForm,
    HandoverForm,
    IntakeOutputForm,
    NursingNoteForm,
    ReasonForm,
    VitalsScheduleForm,
)
from .models import (
    HandoverNote,
    IntakeOutput,
    MedicationAdministration,
    NurseAssignment,
    NursingNote,
    Shift,
    current_shift,
)

NURSING = "nursing.notes"


def _admission(request, pk):
    return get_scoped_or_404(Admission.objects.select_related("patient"), request, pk=pk)


def _chart(admission, tab):
    return f"{reverse('ipd:admission-detail', args=[admission.pk])}?tab={tab}"


def _back(request, fallback):
    nxt = request.POST.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else fallback


@require_GET
@permission_required(NURSING)
def workspace(request):
    wards = Ward.objects.filter(branch=request.branch, is_active=True)
    ward_id = request.GET.get("ward", "")
    mine = services.assigned_admissions(request.user, request.branch)
    show = request.GET.get("show") or ("mine" if mine.exists() else "ward")
    if show == "mine":
        admissions = mine
    else:
        admissions = scoped(Admission, request).filter(status__in=Admission.ACTIVE_STATUSES)
        if ward_id.isdigit():
            admissions = admissions.filter(current_bed__room__ward_id=ward_id)
    admissions = list(
        admissions.select_related("patient", "current_bed__room__ward", "vitals_schedule").order_by(
            "current_bed__room__ward__name", "current_bed__code"
        )
    )
    now = timezone.now()
    tasks = services.due_tasks(admissions, now=now)
    shift, shift_date = current_shift(now)
    done = MedicationAdministration.objects.filter(
        order__admission__in=admissions,
        recorded_by=request.user,
        entered_in_error=False,
        administered_at__gte=now - timedelta(hours=8),
    ).count()
    ward_ids = {a.current_bed.room.ward_id for a in admissions if a.current_bed_id}
    handovers = HandoverNote.objects.filter(
        ward__branch=request.branch, acknowledged_at__isnull=True
    ).select_related("ward", "given_by")
    if ward_ids:
        handovers = handovers.filter(ward_id__in=ward_ids)
    rows = []
    for adm in admissions:
        last = services.last_vitals(adm)
        rows.append(
            {
                "admission": adm,
                "last_vitals": last,
                "vitals_due": services.vitals_due_at(adm, last),
                "tasks": sum(1 for t in tasks if t["admission"].pk == adm.pk),
            }
        )
    return render(
        request,
        "nursing/workspace.html",
        {
            "rows": rows,
            "tasks": tasks,
            "done": done,
            "handovers": handovers[:20],
            "handover_count": handovers.count(),
            "wards": wards,
            "ward": ward_id,
            "show": show,
            "has_mine": mine.exists(),
            "shift": Shift(shift).label,
            "shift_date": shift_date,
            "now": now,
        },
    )


@require_POST
@permission_required(NURSING)
def note_add(request, pk):
    admission = _admission(request, pk)
    form = NursingNoteForm(request.POST)
    if form.is_valid():
        try:
            note = services.add_note(admission, request=request, **form.cleaned_data)
        except services.NursingError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request, "nursing.note.add", admission, summary=f"note_id={note.pk} {note.kind}"
            )
            messages.success(request, "Nursing note saved.")
    else:
        messages.error(request, "Write the note.")
    return redirect(_back(request, _chart(admission, "nursing")))


@require_POST
@permission_required(NURSING)
def io_add(request, pk):
    admission = _admission(request, pk)
    form = IntakeOutputForm(request.POST)
    if form.is_valid():
        try:
            row = services.record_intake_output(admission, request=request, **form.cleaned_data)
        except services.NursingError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "nursing.io.add",
                admission,
                summary=f"{row.kind} {row.category} {row.volume_ml} ml",
            )
            messages.success(request, f"{row.get_kind_display()} of {row.volume_ml} ml recorded.")
    else:
        messages.error(request, "Enter the type and the volume.")
    return redirect(_back(request, _chart(admission, "nursing")))


@require_POST
@permission_required(NURSING)
def schedule_set(request, pk):
    admission = _admission(request, pk)
    form = VitalsScheduleForm(request.POST)
    if form.is_valid():
        try:
            schedule = services.set_vitals_schedule(
                admission, request=request, interval_hours=form.cleaned_data["interval_hours"]
            )
        except services.NursingError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "nursing.vitals_schedule",
                admission,
                changes={"interval_hours": [None, schedule.interval_hours]},
            )
            messages.success(
                request, f"Observations due {schedule.get_interval_hours_display().lower()}."
            )
    return redirect(_back(request, _chart(admission, "nursing")))


@require_POST
@permission_required("ipd.manage")
def assign(request, pk):
    admission = _admission(request, pk)
    nurse = User.objects.filter(
        organization=request.organization, pk=request.POST.get("nurse") or 0
    ).first()
    if nurse is None:
        messages.error(request, "Choose a nurse.")
    else:
        try:
            services.assign(admission, request=request, nurse=nurse)
        except services.NursingError as err:
            messages.error(request, err.message)
        else:
            audit.record(request, "nursing.assign", admission, summary=f"nurse_id={nurse.pk}")
            messages.success(request, f"{nurse.get_full_name() or nurse} assigned.")
    return redirect(_back(request, _chart(admission, "nursing")))


@require_POST
@permission_required("ipd.manage")
def unassign(request, pk):
    assignment = get_object_or_404(
        NurseAssignment,
        pk=pk,
        ended_at__isnull=True,
        admission__organization=request.organization,
        admission__branch=request.branch,
    )
    services.unassign(assignment, request=request)
    audit.record(
        request, "nursing.unassign", assignment.admission, summary=f"nurse_id={assignment.nurse_id}"
    )
    messages.success(request, "Assignment ended.")
    return redirect(_back(request, _chart(assignment.admission, "nursing")))


@require_POST
@permission_required(NURSING)
def dose_chart(request, pk):
    order = get_object_or_404(
        MedicationOrder.objects.select_related("admission"),
        pk=pk,
        admission__organization=request.organization,
        admission__branch=request.branch,
    )
    form = DoseForm(request.POST)
    if form.is_valid():
        c = form.cleaned_data
        try:
            dose = services.chart_dose(
                order,
                request=request,
                status=c["status"],
                scheduled_for=c["scheduled_for"],
                reason=c["reason"],
                dose_given=c["dose_given"],
            )
        except services.NursingError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "nursing.dose",
                order.admission,
                summary=f"order_id={order.pk} {dose.status}"
                + (
                    f" {timezone.localtime(dose.scheduled_for):%H:%M}" if dose.scheduled_for else ""
                ),
            )
            messages.success(
                request, f"{order.medicine_name}: {dose.get_status_display().lower()}."
            )
    else:
        messages.error(request, "Choose what happened with the dose.")
    return redirect(_back(request, _chart(order.admission, "medication")))


ERROR_KINDS = {
    "note": (NursingNote, "admission", "nursing"),
    "io": (IntakeOutput, "admission", "nursing"),
    "dose": (MedicationAdministration, "order__admission", "medication"),
}


@require_POST
@permission_required(NURSING)
def mark_error(request, kind, pk):
    if kind not in ERROR_KINDS:
        return redirect("nursing:workspace")
    model, path, tab = ERROR_KINDS[kind]
    obj = get_object_or_404(
        model,
        pk=pk,
        **{f"{path}__organization": request.organization, f"{path}__branch": request.branch},
    )
    admission = obj.order.admission if kind == "dose" else obj.admission
    form = ReasonForm(request.POST)
    if form.is_valid():
        try:
            services.mark_error(obj, request=request, reason=form.cleaned_data["reason"])
        except services.NursingError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                f"nursing.{kind}.error",
                admission,
                changes={"entered_in_error": [False, True]},
                summary=f"{kind}_id={obj.pk}",
            )
            messages.success(request, "Marked as entered in error. The entry stays on record.")
    else:
        messages.error(request, "Give the reason the entry is wrong.")
    return redirect(_back(request, _chart(admission, tab)))


@permission_required(NURSING)
def handover(request):
    wards = Ward.objects.filter(branch=request.branch, is_active=True)
    ward = None
    if (request.POST.get("ward") or request.GET.get("ward") or "").isdigit():
        ward = get_scoped_or_404(Ward, request, pk=request.POST.get("ward") or request.GET["ward"])
    form = HandoverForm(request.POST or None)
    if request.method == "POST" and ward and form.is_valid():
        try:
            note = services.give_handover(ward, request=request, **form.cleaned_data)
        except services.NursingError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request, "nursing.handover.give", note, summary=f"{ward.name} {note.shift}"
            )
            messages.success(request, "Handover saved. The next shift will acknowledge it.")
            return redirect(f"{reverse('nursing:handover')}?ward={ward.pk}")
    notes = HandoverNote.objects.filter(ward__branch=request.branch).select_related(
        "ward", "given_by", "acknowledged_by"
    )
    if ward:
        notes = notes.filter(ward=ward)
    patients = []
    if ward:
        patients = (
            scoped(Admission, request)
            .filter(status__in=Admission.ACTIVE_STATUSES, current_bed__room__ward=ward)
            .select_related("patient", "current_bed")
            .order_by("current_bed__code")
        )
    shift, shift_date = current_shift()
    return render(
        request,
        "nursing/handover.html",
        {
            "wards": wards,
            "ward": ward,
            "form": form,
            "notes": notes[:50],
            "patients": patients,
            "shift_label": Shift(shift).label,
            "shift_date": shift_date,
            "breadcrumbs": [
                {"label": "Nursing workspace", "url": reverse("nursing:workspace")},
                {"label": "Shift handover"},
            ],
        },
    )


@require_POST
@permission_required(NURSING)
def handover_ack(request, pk):
    note = get_object_or_404(HandoverNote, pk=pk, ward__branch=request.branch,
                             ward__organization=request.organization)  # fmt: skip
    try:
        services.acknowledge_handover(note, request=request)
    except services.NursingError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "nursing.handover.ack", note, summary=note.ward.name)
        messages.success(request, "Handover acknowledged.")
    return redirect(_back(request, f"{reverse('nursing:handover')}?ward={note.ward_id}"))
