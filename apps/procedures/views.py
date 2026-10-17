"""Procedures desk, patient procedure detail, and ordering from the consultation workspace."""

from datetime import date

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, paginate, scoped
from apps.encounters import services as encounter_services
from apps.encounters.models import Encounter
from apps.patients.models import Patient

from . import services
from .forms import CancelForm, CompleteForm, ProcedureOrderForm
from .models import PatientProcedure

VIEW, ORDER, PERFORM = "procedure.view", "procedure.order", "procedure.perform"


def _procedures(request):
    return scoped(PatientProcedure, request).select_related(
        "patient", "performed_by__user", "invoice", "service"
    )


def _crumbs(*extra):
    return [{"label": "Procedures", "url": reverse("procedures:procedure-list")}, *extra]


def _summary(proc) -> str:
    s = f"{proc.number} {proc.name} × {proc.quantity} @ {proc.unit_price}"
    if proc.price_changed:
        s += f" (list {proc.list_price}: {proc.price_change_reason})"
    return s


def _place_order(request, form, *, patient, encounter=None):
    c = form.cleaned_data
    proc = services.order(
        request=request,
        patient=patient,
        service=c["service"],
        performed_by=c["performed_by"],
        encounter=encounter,
        quantity=c["quantity"],
        unit_price=c["unit_price"],
        price_reason=c["price_reason"],
        notes=c["notes"],
    )
    audit.record(request, "procedure.order", proc, summary=_summary(proc))
    if proc.price_changed:
        audit.record(request, "procedure.price_change", proc, summary=_summary(proc))
    return proc


@permission_required(VIEW)
def procedure_list(request):
    qs = _procedures(request)
    status = request.GET.get("status", "")
    if status in PatientProcedure.Status.values:
        qs = qs.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(number__icontains=q)
            | Q(name__icontains=q)
            | Q(patient__full_name__icontains=q)
            | Q(patient__mrn__icontains=q)
        )
    day = request.GET.get("date", "")
    try:
        qs = qs.filter(created_at__date=date.fromisoformat(day)) if day else qs
    except ValueError:
        day = ""
    return render(
        request,
        "procedures/list.html",
        {
            "page_obj": paginate(request, qs),
            "status": status,
            "q": q,
            "day": day,
            "statuses": PatientProcedure.Status.choices,
            "can_order": has_perm(request, ORDER),
        },
    )


@permission_required(ORDER)
def procedure_create(request):
    initial = {}
    if request.GET.get("patient", "").isdigit():
        initial["patient"] = request.GET["patient"]
    form = ProcedureOrderForm(request.POST or None, initial=initial, request=request)
    if request.method == "POST" and form.is_valid():
        try:
            proc = _place_order(request, form, patient=form.cleaned_data["patient"])
        except services.ProcedureError as err:
            form.add_error(None, err.message)
        else:
            messages.success(
                request,
                f"{proc.number} added. "
                + (f"Invoice {proc.invoice.number} is ready to pay." if proc.invoice else ""),
            )
            return redirect("procedures:procedure-detail", pk=proc.pk)
    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    return render(
        request,
        "procedures/create.html",
        {
            "form": form,
            "patient": patient,
            "can_override": has_perm(request, services.PRICE_OVERRIDE),
            "breadcrumbs": _crumbs({"label": "Add procedure"}),
        },
    )


@require_POST
@permission_required(ORDER)
def encounter_order(request, pk):
    """Doctor adds a procedure from the consultation workspace."""
    encounter = get_scoped_or_404(Encounter, request, pk=pk)
    back = f"{reverse('encounters:detail', args=[pk])}#sec-procedures"
    if not (encounter.is_draft and encounter_services.can_write(request, encounter)):
        messages.error(request, "Only the treating doctor can add procedures to an open visit.")
        return redirect(back)
    form = ProcedureOrderForm(request.POST, request=request, with_patient=False)
    if not form.is_valid():
        errors = [e for errs in form.errors.values() for e in errs]
        messages.error(request, " ".join(errors) or "Check the procedure.")
        return redirect(back)
    try:
        proc = _place_order(request, form, patient=encounter.patient, encounter=encounter)
    except services.ProcedureError as err:
        messages.error(request, err.message)
    else:
        messages.success(request, f"{proc.number} {proc.name} added to the bill.")
    return redirect(back)


@permission_required(VIEW)
def procedure_detail(request, pk):
    proc = get_scoped_or_404(_procedures(request), request, pk=pk)
    return render(
        request,
        "procedures/detail.html",
        {
            "proc": proc,
            "complete_form": CompleteForm(),
            "cancel_form": CancelForm(),
            "can_perform": proc.is_open and has_perm(request, PERFORM),
            "can_cancel": proc.is_open and has_perm(request, ORDER),
            "can_bill": has_perm(request, "billing.view"),
            "breadcrumbs": _crumbs({"label": proc.number}),
        },
    )


@require_POST
@permission_required(PERFORM)
def procedure_complete(request, pk):
    proc = get_scoped_or_404(PatientProcedure, request, pk=pk)
    form = CompleteForm(request.POST)
    if form.is_valid():
        try:
            services.complete(proc, request=request, findings=form.cleaned_data["findings"])
        except services.ProcedureError as err:
            messages.error(request, err.message)
        else:
            audit.record(request, "procedure.complete", proc, summary=proc.number)
            messages.success(request, f"{proc.number} marked done.")
    return redirect("procedures:procedure-detail", pk=pk)


@require_POST
@permission_required(ORDER)
def procedure_cancel(request, pk):
    proc = get_scoped_or_404(PatientProcedure, request, pk=pk)
    form = CancelForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A reason is required to cancel a procedure.")
        return redirect("procedures:procedure-detail", pk=pk)
    try:
        services.cancel(proc, request=request, reason=form.cleaned_data["reason"])
    except services.ProcedureError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "procedure.cancel",
            proc,
            summary=f"{proc.number}: {form.cleaned_data['reason']}",
        )
        messages.success(request, f"{proc.number} cancelled.")
    return redirect("procedures:procedure-detail", pk=pk)


def encounter_panel_context(request, encounter, writable) -> dict:
    """Context for templates/procedures/_encounter_panel.html."""
    can_order = writable and has_perm(request, ORDER)
    ctx = {
        "procedures": encounter.procedures.select_related("performed_by__user", "invoice"),
        "procedure_can_order": can_order,
        "procedure_can_override": has_perm(request, services.PRICE_OVERRIDE),
    }
    if can_order:
        ctx["procedure_form"] = ProcedureOrderForm(
            request=request, with_patient=False, initial={"performed_by": encounter.doctor_id}
        )
    return ctx
