"""Doctor workspace (dashboard) and the OPD consultation workspace."""

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.appointments.models import Appointment
from apps.audit import services as audit
from apps.core.forms import StyledFormMixin
from apps.core.scoping import get_scoped_or_404, scoped
from apps.lab import services as lab_services
from apps.patients.models import Patient
from apps.queue import services as queue_services
from apps.queue.models import QueueToken

from . import services
from .models import NOTE_FIELD_NAMES, NOTE_FIELDS, Encounter

Status = Encounter.Status
VIEW, WRITE = "consultation.view", "consultation.create"

SECTIONS = [
    ("history", "History"),
    ("exam", "Examination"),
    ("assessment", "Assessment"),
    ("plan", "Plan & follow-up"),
]


def _encounters(request):
    return scoped(Encounter, request).select_related(
        "patient", "doctor__user", "department", "token", "appointment"
    )


def _crumbs(*extra):
    return [{"label": "Doctor workspace", "url": reverse("encounters:workspace")}, *extra]


# --- Doctor workspace --------------------------------------------------------------------------


@require_GET
@permission_required(VIEW)
def workspace(request):
    """Doctor home (design screen 93): today's clinic, current patient, follow-ups, drafts."""
    doctor = services.doctor_for(request)
    ctx = {"doctor": doctor}
    if doctor is not None:
        today = timezone.localdate()
        queue = queue_services.queue_for(request.branch, doctor=doctor)
        current = queue_services.current_for(request.branch, doctor)
        mine = _encounters(request).filter(doctor=doctor)
        ctx.update(
            {
                "current": current,
                "current_encounter": Encounter.objects.filter(token=current).first()
                if current
                else None,
                "waiting": queue_services.waiting(queue)[:8],
                "waiting_count": queue_services.waiting(queue).count(),
                "completed_today": mine.filter(
                    status=Status.COMPLETED, completed_at__date=today
                ).count(),
                "drafts": mine.filter(status=Status.DRAFT).order_by("-updated_at"),
                "follow_ups": services.follow_ups(doctor),
                "appointments": scoped(Appointment, request)
                .filter(doctor=doctor, date=today)
                .exclude(status__in=Appointment.RELEASED_STATUSES)
                .select_related("patient")
                .order_by("start_time"),
                "can_write": has_perm(request, WRITE),
                "today": today,
                "critical_results": lab_services.unacknowledged_critical(
                    request.organization, doctor=doctor
                )[:10],
            }
        )
    return render(request, "encounters/workspace.html", ctx)


@require_POST
@permission_required(WRITE)
def start(request):
    """Start or continue a consultation from a token, an appointment, or a patient record."""
    token = appointment = None
    if request.POST.get("token", "").isdigit():
        token = get_scoped_or_404(QueueToken, request, pk=request.POST["token"])
        patient = token.patient
    elif request.POST.get("appointment", "").isdigit():
        appointment = get_scoped_or_404(Appointment, request, pk=request.POST["appointment"])
        patient = appointment.patient
    else:
        patient = get_scoped_or_404(Patient, request, pk=request.POST.get("patient") or 0)
    try:
        encounter, created = services.start(
            request=request, patient=patient, token=token, appointment=appointment
        )
    except services.EncounterError as err:
        messages.error(request, err.message)
        back = request.POST.get("back", "")
        if not url_has_allowed_host_and_scheme(back, {request.get_host()}):
            back = reverse("encounters:workspace")
        return redirect(back)
    if created:
        audit.record(
            request,
            "encounter.start",
            encounter,
            summary=f"{encounter.number} {patient.mrn}"
            + (f" token {token.label}" if token else ""),
        )
    return redirect("encounters:detail", pk=encounter.pk)


# --- Consultation workspace --------------------------------------------------------------------


class NoteForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Encounter
        fields = NOTE_FIELD_NAMES
        widgets = {
            f: forms.Textarea(attrs={"rows": 3})
            for f in NOTE_FIELD_NAMES
            if f not in ("follow_up_date", "referral_to")
        } | {"follow_up_date": forms.DateInput(attrs={"type": "date"})}
        labels = {f: label for f, label, _ in NOTE_FIELDS}


def _get_encounter(request, pk):
    return get_scoped_or_404(_encounters(request), request, pk=pk)


def _workspace_context(request, encounter, form=None):
    patient = encounter.patient
    previous = (
        Encounter.objects.filter(
            organization=request.organization, patient=patient, status=Status.COMPLETED
        )
        .exclude(pk=encounter.pk)
        .select_related("doctor__user", "branch")
        .order_by("-started_at")
    )
    writable = encounter.is_draft and services.can_write(request, encounter)
    form = form or NoteForm(instance=encounter)
    if not writable:
        for field in form.fields.values():
            field.disabled = True
    sections = [
        {"key": key, "label": label, "fields": [form[f] for f, _, s in NOTE_FIELDS if s == key]}
        for key, label in SECTIONS
    ]
    from apps.prescriptions.views import panel_context
    from apps.vitals import services as vitals_services

    visit_vitals = list(
        vitals_services.valid_for(patient, request.organization)
        .filter(
            Q(encounter=encounter)
            | Q(recorded_at__date=timezone.localtime(encounter.started_at).date())
        )
        .select_related("recorded_by")[:5]
    )
    from apps.lab.models import LabOrder
    from apps.lab.views import _catalogue

    lab_can_order = writable and has_perm(request, "lab.order.create")
    lab = {
        "lab_orders": LabOrder.objects.filter(encounter=encounter).prefetch_related(
            "items__results"
        ),
        "lab_can_order": lab_can_order,
        "selected_tests": [],
        "selected_panels": [],
        **(_catalogue(request) if lab_can_order else {}),
    }
    from apps.documents.services import panel_context as document_panel

    return {
        **lab,
        **document_panel(request, patient, encounter=encounter),
        **diagnosis_context(request, encounter),
        **panel_context(request, encounter),
        "encounter": encounter,
        "patient": patient,
        "visit_vitals": [(v, vitals_services.summary(v, patient)) for v in visit_vitals],
        "last_vitals": None
        if visit_vitals
        else vitals_services.latest(patient, request.organization),
        "can_record_vitals": has_perm(request, "vitals.record") and encounter.is_draft,
        "form": form,
        "sections": sections,
        "writable": writable,
        "can_sign": writable and has_perm(request, "consultation.sign"),
        "can_reopen": not encounter.is_draft
        and has_perm(request, "consultation.reopen")
        and services.can_write(request, encounter),
        "allergies": patient.allergies.filter(entered_in_error=False),
        "alerts": patient.alerts.filter(is_active=True),
        "last_visit": previous.first(),
        "previous": previous[:5],
        "versions": encounter.versions.select_related("signed_by"),
        "breadcrumbs": _crumbs({"label": f"{encounter.number} · {patient.full_name}"}),
    }


def _note_data(form):
    return {f: form.cleaned_data[f] for f in NOTE_FIELD_NAMES if f in form.cleaned_data}


def _revision(request):
    value = request.POST.get("revision", "")
    return int(value) if value.isdigit() else None


@permission_required(VIEW)
def detail(request, pk):
    """Consultation workspace. GET shows the note; POST saves the draft (Save draft button)."""
    encounter = _get_encounter(request, pk)
    if request.method == "GET":
        audit.record(request, "encounter.view", encounter, summary=encounter.number)
        return render(request, "encounters/detail.html", _workspace_context(request, encounter))
    form = NoteForm(request.POST, instance=Encounter.objects.get(pk=encounter.pk))
    if not form.is_valid():
        messages.error(request, "Check the highlighted fields.")
        return render(
            request, "encounters/detail.html", _workspace_context(request, encounter, form)
        )
    try:
        services.save_note(
            encounter, request=request, data=_note_data(form), revision=_revision(request)
        )
    except services.EncounterError as err:
        messages.error(request, err.message)
    else:
        messages.success(request, "Draft saved.")
    if request.POST.get("exit"):
        return redirect("encounters:workspace")
    return redirect("encounters:detail", pk=pk)


@require_POST
@permission_required(WRITE)
def autosave(request, pk):
    """htmx autosave while typing. Replies with the save status and the new revision."""
    encounter = _get_encounter(request, pk)
    form = NoteForm(request.POST, instance=Encounter.objects.get(pk=encounter.pk))
    ctx = {"encounter": encounter, "error": ""}
    if not form.is_valid():
        ctx["error"] = "Not saved: check the highlighted fields."
    else:
        try:
            encounter = services.save_note(
                encounter, request=request, data=_note_data(form), revision=_revision(request)
            )
        except services.EncounterError as err:
            ctx["error"] = err.message
        ctx["encounter"] = encounter
    return render(request, "encounters/_save_status.html", ctx)


@require_POST
@permission_required("consultation.sign")
def complete(request, pk):
    """Save what is on screen, then sign and complete the consultation."""
    encounter = _get_encounter(request, pk)
    form = NoteForm(request.POST, instance=Encounter.objects.get(pk=encounter.pk))
    if not form.is_valid():
        messages.error(request, "Check the highlighted fields.")
        return render(
            request, "encounters/detail.html", _workspace_context(request, encounter, form)
        )
    try:
        services.save_note(
            encounter, request=request, data=_note_data(form), revision=_revision(request)
        )
        version = services.complete(encounter, request=request)
    except services.EncounterError as err:
        messages.error(request, err.message)
        return redirect("encounters:detail", pk=pk)
    encounter.refresh_from_db()
    audit.record(
        request,
        "encounter.complete",
        encounter,
        changes={
            "status": ["draft", "completed"],
            "version": [version.version - 1, version.version],
        },
        summary=f"{encounter.number} v{version.version} sections: "
        + ", ".join(encounter.filled_fields()),
    )
    messages.success(
        request, f"{encounter.number} completed and signed (version {version.version})."
    )
    return redirect("encounters:detail", pk=pk)


class ReopenForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        max_length=300,
        label="Reason for amendment",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Saved with the new version and in the audit log.",
    )


@permission_required("consultation.reopen")
def reopen(request, pk):
    """Amend a completed encounter (design screen 134). Signed versions stay unchanged."""
    encounter = _get_encounter(request, pk)
    if not services.can_write(request, encounter):
        raise PermissionDenied
    form = ReopenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            encounter = services.reopen(
                encounter, request=request, reason=form.cleaned_data["reason"]
            )
        except services.EncounterError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "encounter.reopen",
                encounter,
                changes={"status": ["completed", "draft"]},
                summary=f"{encounter.number}: {encounter.reopen_reason[:200]}",
            )
            messages.success(request, "Amendment started. The signed version is kept.")
            return redirect("encounters:detail", pk=pk)
    return render(
        request,
        "encounters/reopen.html",
        {
            "encounter": encounter,
            "form": form,
            "latest": encounter.versions.first(),
            "breadcrumbs": _crumbs(
                {"label": encounter.number, "url": reverse("encounters:detail", args=[pk])},
                {"label": "Amend"},
            ),
        },
    )


@require_GET
@permission_required(VIEW)
def history(request, pk):
    """Every signed version of the note, newest first, with what changed."""
    encounter = _get_encounter(request, pk)
    versions = list(encounter.versions.select_related("signed_by"))
    labels = [(f, label) for f, label, _ in NOTE_FIELDS]
    rows = []
    for i, v in enumerate(versions):
        prev = versions[i + 1] if i + 1 < len(versions) else None
        rows.append(
            {
                "version": v,
                "changed": v.changed_from(prev),
                "fields": [(label, v.content.get(f, "")) for f, label in labels],
            }
        )
    audit.record(request, "encounter.history_view", encounter, summary=encounter.number)
    return render(
        request,
        "encounters/history.html",
        {
            "encounter": encounter,
            "rows": rows,
            "breadcrumbs": _crumbs(
                {"label": encounter.number, "url": reverse("encounters:detail", args=[pk])},
                {"label": "Versions"},
            ),
        },
    )


# --- Coded diagnoses (Phase 10) ----------------------------------------------------------------


def diagnosis_context(request, encounter, error=""):
    return {
        "encounter": encounter,
        "diagnoses": encounter.diagnoses.select_related("code"),
        "dx_writable": encounter.is_draft and services.can_write(request, encounter),
        "dx_error": error,
    }


def _diagnosis_reply(request, encounter, error=""):
    if request.htmx:
        return render(
            request, "encounters/_diagnoses.html", diagnosis_context(request, encounter, error)
        )
    if error:
        messages.error(request, error)
    return redirect(f"{reverse('encounters:detail', args=[encounter.pk])}#sec-diagnoses")


@require_GET
@permission_required(WRITE)
def diagnosis_search(request, pk):
    encounter = _get_encounter(request, pk)
    q = request.GET.get("dx_q", "")
    return render(
        request,
        "encounters/_diagnosis_results.html",
        {
            "encounter": encounter,
            "results": services.search_codes(request.organization, q),
            "q": q.strip(),
        },
    )


@require_POST
@permission_required(WRITE)
def diagnosis_add(request, pk):
    from .models import DiagnosisCode

    encounter = _get_encounter(request, pk)
    code = None
    if request.POST.get("code", "").isdigit():
        code = get_scoped_or_404(DiagnosisCode, request, pk=request.POST["code"])
    try:
        dx = services.add_diagnosis(
            encounter,
            request=request,
            code=code,
            text=request.POST.get("text", ""),
            kind=request.POST.get("kind", "provisional"),
        )
    except services.EncounterError as err:
        return _diagnosis_reply(request, encounter, err.message)
    audit.record(
        request, "encounter.diagnosis_add", encounter, summary=f"{encounter.number} {dx.kind}"
    )
    return _diagnosis_reply(request, encounter)


@require_POST
@permission_required(WRITE)
def diagnosis_update(request, pk, dx_pk):
    encounter = _get_encounter(request, pk)
    dx = encounter.diagnoses.filter(pk=dx_pk).first()
    if dx is None:
        return _diagnosis_reply(request, encounter, "That diagnosis was already removed.")
    try:
        services.update_diagnosis(
            dx,
            request=request,
            kind=request.POST.get("kind") or None,
            primary=bool(request.POST.get("primary")),
        )
    except services.EncounterError as err:
        return _diagnosis_reply(request, encounter, err.message)
    return _diagnosis_reply(request, encounter)


@require_POST
@permission_required(WRITE)
def diagnosis_remove(request, pk, dx_pk):
    encounter = _get_encounter(request, pk)
    dx = encounter.diagnoses.filter(pk=dx_pk).first()
    if dx is not None:
        try:
            services.remove_diagnosis(dx, request=request)
        except services.EncounterError as err:
            return _diagnosis_reply(request, encounter, err.message)
        audit.record(request, "encounter.diagnosis_remove", encounter, summary=encounter.number)
    return _diagnosis_reply(request, encounter)


# --- Settings → Diagnosis codes ----------------------------------------------------------------


@permission_required("admin.masterdata.manage")
def diagnosis_code_list(request):
    from .models import DiagnosisCode

    services.ensure_diagnosis_codes(request.organization)
    if request.method == "POST":
        code = request.POST.get("code", "").strip().upper()[:10]
        title = request.POST.get("title", "").strip()[:200]
        if not code or not title:
            messages.error(request, "Enter both the code and the title.")
        elif scoped(DiagnosisCode, request).filter(code=code).exists():
            messages.error(request, f"{code} is already in the list.")
        else:
            dc = DiagnosisCode.objects.create(
                organization=request.organization, code=code, title=title
            )
            audit.record(request, "diagnosis_code.create", dc, summary=code)
            messages.success(request, f"{code} added.")
        return redirect("encounters:diagnosis-code-list")
    q = request.GET.get("q", "").strip()
    codes = scoped(DiagnosisCode, request)
    if q:
        codes = codes.filter(Q(code__istartswith=q) | Q(title__icontains=q))
    return render(
        request,
        "encounters/diagnosis_codes.html",
        {"codes": codes, "q": q, "settings_tab": "diagnoses"},
    )


@require_POST
@permission_required("admin.masterdata.manage")
def diagnosis_code_toggle(request, pk):
    from .models import DiagnosisCode

    dc = get_scoped_or_404(DiagnosisCode, request, pk=pk)
    dc.is_active = not dc.is_active
    dc.save(update_fields=["is_active", "updated_at"])
    audit.record(request, f"diagnosis_code.{'activate' if dc.is_active else 'deactivate'}", dc)
    return redirect("encounters:diagnosis-code-list")
