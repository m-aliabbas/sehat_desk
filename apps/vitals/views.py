"""Vitals station (nurse / reception), the entry form, corrections and the patient trend view."""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, scoped
from apps.encounters.models import Encounter
from apps.patients.models import Patient
from apps.queue.models import QueueToken

from . import services
from .forms import MarkErrorForm, VitalsForm
from .models import VitalSign

RECORD = "vitals.record"


def _safe_next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
        return nxt
    return fallback


@require_GET
@permission_required(RECORD)
def station(request):
    """Today's open tokens in this branch, with the vitals already taken for each patient."""
    tokens = list(
        scoped(QueueToken, request)
        .filter(date=timezone.localdate(), status__in=QueueToken.OPEN_STATUSES)
        .select_related("patient", "doctor__user", "department")
        .order_by("priority_rank", "queued_at")
    )
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    taken = {}
    for v in (
        VitalSign.objects.filter(
            organization=request.organization,
            patient_id__in=[t.patient_id for t in tokens],
            recorded_at__gte=start,
            entered_in_error=False,
        )
        .select_related("patient")
        .order_by("recorded_at")
    ):
        taken[v.patient_id] = v  # latest wins
    rows = []
    for t in tokens:
        v = taken.get(t.patient_id)
        rows.append({"token": t, "vitals": v, "summary": services.summary(v) if v else []})
    pending = sum(1 for r in rows if r["vitals"] is None)
    return render(
        request,
        "vitals/station.html",
        {"rows": rows, "pending": pending, "done": len(rows) - pending},
    )


@permission_required(RECORD)
def record(request):
    """Vitals entry. ?patient= (or ?token= / ?encounter=) picks the patient; ?next= returns."""
    token = encounter = None
    source = request.POST if request.method == "POST" else request.GET
    if source.get("token", "").isdigit():
        token = get_scoped_or_404(QueueToken, request, pk=source["token"])
        patient = token.patient
    elif source.get("encounter", "").isdigit():
        encounter = get_scoped_or_404(Encounter, request, pk=source["encounter"])
        patient = encounter.patient
    else:
        patient = get_scoped_or_404(Patient, request, pk=source.get("patient") or 0)
    fallback = reverse("vitals:station") if token else reverse("patients:detail", args=[patient.pk])
    back = _safe_next(request, fallback)

    form = VitalsForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            vitals = services.record(
                request=request,
                patient=patient,
                data=form.vitals_data(),
                token=token,
                encounter=encounter,
            )
        except services.VitalsError as err:
            form.add_error(None, err.message)
        else:
            flags = services.assess(vitals, patient)
            audit.record(
                request,
                "vitals.record",
                vitals,
                summary=f"{patient.mrn} " + ", ".join(sorted(k for k in flags if k != "bp")),
            )
            critical = [k for k, level in flags.items() if level == "critical"]
            if critical:
                messages.warning(
                    request,
                    f"Vitals saved for {patient.full_name}. Critical values: tell the doctor now.",
                )
            else:
                messages.success(request, f"Vitals saved for {patient.full_name}.")
            return redirect(back)
    previous = services.latest(patient, request.organization)
    return render(
        request,
        "vitals/record.html",
        {
            "form": form,
            "patient": patient,
            "token": token,
            "encounter": encounter,
            "next": back,
            "previous": previous,
            "previous_summary": services.summary(previous, patient) if previous else [],
            "child": services.is_child(patient),
            "breadcrumbs": [
                {"label": "Vitals station", "url": reverse("vitals:station")}
                if has_perm(request, RECORD)
                else {"label": "Patients", "url": reverse("patients:list")},
                {"label": f"{patient.full_name} · record vitals"},
            ],
        },
    )


@require_POST
@permission_required(RECORD)
def mark_error(request, pk):
    vitals = get_scoped_or_404(VitalSign, request, all_branches=True, pk=pk)
    form = MarkErrorForm(request.POST)
    if form.is_valid():
        try:
            services.mark_error(vitals, request=request, reason=form.cleaned_data["reason"])
        except services.VitalsError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "vitals.entered_in_error",
                vitals,
                changes={"entered_in_error": [False, True]},
                summary=form.cleaned_data["reason"][:200],
            )
            messages.success(request, "Reading marked as entered in error. It stays on record.")
    else:
        messages.error(request, "Give the reason the reading is wrong.")
    return redirect(
        _safe_next(request, reverse("patients:detail", args=[vitals.patient_id]) + "?tab=vitals")
    )
