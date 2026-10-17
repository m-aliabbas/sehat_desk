"""Patient directory, registration, profile and demographics."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, scoped

from . import services
from .forms import AllergyForm, IdentifierForm, MarkErrorForm, PatientForm
from .identity import normalize_cnic
from .models import Allergy, Patient, PatientAlert, PatientIdentifier

# Demographic fields recorded in the audit trail when edited (values, not clinical content).
AUDIT_FIELDS = [
    "full_name", "gender", "date_of_birth", "phone", "cnic", "email", "address", "city",
    "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relation",
    "blood_group", "allergy_status", "consent_recorded", "is_active",
]  # fmt: skip

PROFILE_TABS = [
    ("overview", "Overview"),
    ("visits", "Visits"),
    ("vitals", "Vitals"),
    ("prescriptions", "Prescriptions"),
    ("lab", "Lab reports"),
    ("billing", "Billing"),
    ("documents", "Documents"),
    ("timeline", "Timeline"),
]


def _patients(request):
    return scoped(Patient, request)


def _get_patient(request, pk):
    return get_scoped_or_404(Patient, request, pk=pk)


def _crumbs(*extra):
    return [{"label": "Patients", "url": reverse("patients:list")}, *extra]


@require_GET
@permission_required("patient.view")
def patient_list(request):
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active")
    qs = _patients(request)
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    results, suggestions = services.search_patients(qs, q)
    page = Paginator(results.order_by("-created_at"), 20).get_page(request.GET.get("page"))
    ctx = {"page_obj": page, "q": q, "status": status, "suggestions": suggestions}
    if request.htmx and request.htmx.target == "patient-results":
        return render(request, "patients/_results.html", ctx)
    return render(request, "patients/list.html", ctx)


@require_GET
@permission_required("patient.view")
def patient_lookup(request):
    """Small htmx search used by appointment booking and other pickers."""
    q = request.GET.get("q", "").strip()
    results = []
    if len(q) >= 2:
        results, _ = services.search_patients(_patients(request).filter(is_active=True), q)
        results = results[:8]
    return render(
        request,
        "patients/_lookup.html",
        {"results": results, "q": q, "target": request.GET.get("target", "")},
    )


@permission_required("patient.create")
def patient_register(request):
    quick = request.GET.get("mode") == "quick" or request.POST.get("mode") == "quick"
    form = PatientForm(
        request.POST or None,
        request.FILES or None,
        organization=request.organization,
        quick=quick,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            patient = services.register_patient(
                organization=request.organization,
                branch=request.branch,
                user=request.user,
                data=form.model_data(),
            )
            if form.cleaned_data.get("guardian_cnic"):
                services.add_identifier(
                    patient,
                    PatientIdentifier.Kind.GUARDIAN_CNIC,
                    form.cleaned_data["guardian_cnic"],
                )
            audit.record(
                request,
                "patient.create",
                patient,
                summary=f"mrn={patient.mrn}"
                + (" duplicate_override" if form.cleaned_data.get("not_duplicate") else ""),
            )
        messages.success(request, f"{patient.full_name} registered as {patient.mrn}.")
        if request.POST.get("next") == "book" and has_perm(request, "appointment.manage"):
            return redirect(f"{reverse('appointments:book')}?patient={patient.pk}")
        return redirect("patients:detail", pk=patient.pk)
    return render(
        request,
        "patients/register.html",
        {"form": form, "quick": quick, "breadcrumbs": _crumbs({"label": "Register"})},
    )


@require_GET
@permission_required("patient.create", "patient.update", any_of=True)
def duplicate_check(request):
    """Live duplicate panel on the registration form (htmx)."""
    from datetime import date

    dob = None
    try:
        dob = date.fromisoformat(request.GET.get("date_of_birth", ""))
    except ValueError:
        pass
    matches = services.find_duplicates(
        request.organization,
        full_name=request.GET.get("full_name", ""),
        phone=request.GET.get("phone", ""),
        cnic=request.GET.get("cnic", ""),
        date_of_birth=dob,
        exclude_pk=request.GET.get("exclude") or None,
    )
    has_input = any(request.GET.get(k) for k in ("full_name", "phone", "cnic"))
    return render(
        request, "patients/_duplicates.html", {"matches": matches, "has_input": has_input}
    )


@require_GET
@permission_required("patient.view")
def patient_detail(request, pk):
    patient = _get_patient(request, pk)
    tab = request.GET.get("tab", "overview")
    if tab not in dict(PROFILE_TABS):
        tab = "overview"
    # Opening a record is logged (PRD §26 privacy: log access to sensitive records).
    audit.record(request, "patient.view", patient, summary=f"tab={tab}")
    from apps.appointments.models import Appointment

    appointments = (
        Appointment.objects.filter(organization=request.organization, patient=patient)
        .select_related("doctor__user", "department", "branch")
        .order_by("-date", "-start_time")
    )
    ctx = {
        "patient": patient,
        "tab": tab,
        "tabs": PROFILE_TABS,
        "allergies": patient.allergies.select_related("recorded_by"),
        "alerts": patient.alerts.filter(is_active=True),
        "identifiers": patient.identifiers.all(),
        "appointments": appointments[:50],
        "last_visit": appointments.filter(status="completed").first(),
        "upcoming": appointments.filter(status__in=["scheduled", "confirmed"]).last(),
        "can_edit": has_perm(request, "patient.update"),
        "can_book": has_perm(request, "appointment.manage"),
        "allergy_form": AllergyForm(),
        "identifier_form": IdentifierForm(),
        "error_form": MarkErrorForm(),
        "breadcrumbs": _crumbs({"label": patient.full_name}),
    }
    if has_perm(request, "consultation.view") and tab in ("visits", "timeline", "overview"):
        from apps.encounters.models import Encounter

        ctx["encounters"] = (
            Encounter.objects.filter(organization=request.organization, patient=patient)
            .select_related("doctor__user", "branch")
            .order_by("-started_at")
        )
    if has_perm(request, "consultation.view") and tab == "overview":
        from apps.encounters.services import diagnosis_history

        ctx["diagnosis_history"] = diagnosis_history(patient, request.organization)[:10]
    ctx["can_see_rx"] = has_perm(request, "consultation.view") or has_perm(request, "pharmacy.sale")
    if ctx["can_see_rx"] and tab == "prescriptions":
        from apps.prescriptions.models import Prescription

        ctx["prescriptions"] = (
            Prescription.objects.filter(organization=request.organization, patient=patient)
            .exclude(items__isnull=True)
            .distinct()
            .select_related("doctor__user", "branch", "encounter")
            .prefetch_related("items")
            .order_by("-created_at")
        )
    lab_codes = ("lab.order.create", "lab.sample.collect", "lab.result.enter", "lab.result.verify")
    ctx["can_see_lab"] = has_perm(request, "consultation.view") or any(
        has_perm(request, c) for c in lab_codes
    )
    from apps.core.features import enabled_features

    features = enabled_features(request.organization)
    ctx["can_see_lab"] = ctx["can_see_lab"] and features["lab"]
    ctx["can_order_lab"] = features["lab"] and has_perm(request, "lab.order.create")
    if ctx["can_see_lab"] and tab == "lab":
        from apps.lab.models import LabOrder

        ctx["lab_orders"] = (
            LabOrder.objects.filter(organization=request.organization, patient=patient)
            .select_related("doctor__user", "branch")
            .prefetch_related("items__results")
            .order_by("-created_at")
        )
    own_doctor = getattr(request.user, "doctor_profile", None)
    ctx["can_consult"] = (
        has_perm(request, "consultation.create")
        and own_doctor is not None
        and own_doctor.organization_id == request.organization.id
    )
    can_bill_view = has_perm(request, "billing.view")
    ctx["can_bill"] = has_perm(request, "billing.payment.collect")
    if can_bill_view and tab in ("billing", "timeline"):
        from apps.billing.models import Invoice, Payment

        ctx["invoices"] = (
            Invoice.objects.filter(organization=request.organization, patient=patient)
            .select_related("branch")
            .order_by("-created_at")
        )
        ctx["patient_payments"] = (
            Payment.objects.filter(organization=request.organization, invoice__patient=patient)
            .select_related("invoice", "method", "branch")
            .order_by("-created_at")
        )
    ctx["can_see_vitals"] = has_perm(request, "consultation.view") or has_perm(
        request, "vitals.record"
    )
    ctx["can_record_vitals"] = has_perm(request, "vitals.record")
    if ctx["can_see_vitals"] and tab in ("vitals", "overview"):
        from apps.vitals import services as vitals_services

        latest_vitals = vitals_services.latest(patient, request.organization)
        ctx["latest_vitals"] = latest_vitals
        ctx["latest_vitals_summary"] = (
            vitals_services.summary(latest_vitals, patient) if latest_vitals else []
        )
        if tab == "vitals":
            ctx["vital_trends"] = vitals_services.trends(patient, request.organization)
            ctx["vital_rows"] = [
                (v, vitals_services.summary(v, patient))
                for v in patient.vitals.filter(organization=request.organization)
                .select_related("recorded_by", "branch", "error_marked_by")
                .order_by("-recorded_at")[:50]
            ]
            ctx["vitals_child"] = vitals_services.is_child(patient)
    if tab == "documents":
        from apps.documents.services import panel_context as document_panel

        ctx.update(document_panel(request, patient))
    ctx["can_admit"] = features["ipd"] and has_perm(request, "ipd.manage")
    if features["ipd"] and (has_perm(request, "ipd.manage") or has_perm(request, "nursing.notes")):
        from apps.ipd.services import current_admission

        ctx["current_admission"] = current_admission(patient, request.organization)
    if tab == "timeline":
        ctx["timeline"] = _timeline(
            patient,
            appointments,
            ctx.get("invoices", []),
            ctx.get("patient_payments", []),
            ctx.get("encounters", []),
        )
    return render(request, "patients/detail.html", ctx)


def _timeline(patient, appointments, invoices=(), payments=(), encounters=()):
    events = [
        {
            "when": patient.created_at,
            "title": "Registered",
            "detail": f"{patient.mrn} at {patient.registered_branch.name}",
            "kind": "registration",
        }
    ]
    for a in appointments:
        events.append(
            {
                "when": a.created_at,
                "title": f"Appointment · {a.get_status_display()}",
                "detail": f"{a.date:%d %b %Y} {a.start_time:%H:%M} · {a.doctor} · {a.department}",
                "kind": "appointment",
            }
        )
    for al in patient.allergies.all():
        events.append(
            {
                "when": al.created_at,
                "title": f"Allergy recorded · {al.substance}",
                "detail": "Entered in error" if al.entered_in_error else al.get_severity_display(),
                "kind": "allergy",
            }
        )
    for e in encounters:
        events.append(
            {
                "when": e.started_at,
                "title": f"OPD consultation · {e.get_status_display()}",
                "detail": f"{e.number} · {e.doctor} · {e.branch.name}",
                "kind": "appointment",
            }
        )
    for inv in invoices:
        events.append(
            {
                "when": inv.created_at,
                "title": f"Invoice {inv.number} · {inv.get_status_display()}",
                "detail": f"Total {inv.grand_total:,.2f} at {inv.branch.name}",
                "kind": "billing",
            }
        )
    for p in payments:
        events.append(
            {
                "when": p.created_at,
                "title": f"Payment {p.number}",
                "detail": f"{p.amount:,.2f} by {p.method} on {p.invoice.number}",
                "kind": "billing",
            }
        )
    return sorted(events, key=lambda e: e["when"], reverse=True)


@permission_required("patient.update")
def patient_edit(request, pk):
    patient = _get_patient(request, pk)
    before = audit.snapshot(patient, AUDIT_FIELDS)
    form = PatientForm(
        request.POST or None,
        request.FILES or None,
        instance=patient,
        organization=request.organization,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        changes = audit.diff(before, patient, AUDIT_FIELDS)
        if "photo" in form.changed_data:
            changes["photo"] = ["", "updated"]
        if changes:
            audit.record(request, "patient.update", patient, changes=changes)
        messages.success(request, "Patient details saved.")
        return redirect("patients:detail", pk=pk)
    return render(
        request,
        "patients/edit.html",
        {
            "form": form,
            "patient": patient,
            "breadcrumbs": _crumbs(
                {"label": patient.full_name, "url": reverse("patients:detail", args=[pk])},
                {"label": "Edit"},
            ),
        },
    )


@require_GET
@permission_required("patient.view")
def patient_photo(request, pk):
    """Patient photos are PHI: served only through this permission- and org-checked view."""
    patient = _get_patient(request, pk)
    if not patient.photo:
        raise Http404
    response = FileResponse(patient.photo.open("rb"))
    response["Cache-Control"] = "private, max-age=300"
    return response


@require_GET
@permission_required("patient.view")
def registration_slip(request, pk):
    patient = _get_patient(request, pk)
    audit.record(request, "patient.print_slip", patient)
    return render(request, "patients/slip.html", {"patient": patient})


# --- Allergies, alerts and identifiers -------------------------------------------------------

CLINICAL_EDIT = ("patient.update", "consultation.create")


@require_POST
@permission_required(*CLINICAL_EDIT, any_of=True)
def allergy_add(request, pk):
    patient = _get_patient(request, pk)
    form = AllergyForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            allergy = form.save(commit=False)
            allergy.patient = patient
            allergy.recorded_by = request.user
            allergy.save()
            if patient.allergy_status != Patient.AllergyStatus.PRESENT:
                patient.allergy_status = Patient.AllergyStatus.PRESENT
                patient.save(update_fields=["allergy_status", "updated_at"])
            audit.record(
                request, "patient.allergy_add", patient, summary=f"allergy_id={allergy.pk}"
            )
        messages.success(request, f"Allergy to {allergy.substance} recorded.")
    else:
        messages.error(request, "Enter the substance the patient is allergic to.")
    return redirect("patients:detail", pk=pk)


@require_POST
@permission_required(*CLINICAL_EDIT, any_of=True)
def allergy_mark_error(request, pk, allergy_pk):
    patient = _get_patient(request, pk)
    allergy = get_object_or_404(Allergy, pk=allergy_pk, patient=patient, entered_in_error=False)
    form = MarkErrorForm(request.POST)
    if form.is_valid():
        allergy.entered_in_error = True
        allergy.error_reason = form.cleaned_data["reason"]
        allergy.error_marked_by = request.user
        allergy.save()
        audit.record(request, "patient.allergy_error", patient, summary=f"allergy_id={allergy.pk}")
        messages.success(request, "Allergy marked as entered in error. The original entry is kept.")
    else:
        messages.error(request, "Give a reason for marking the allergy as an error.")
    return redirect("patients:detail", pk=pk)


@require_POST
@permission_required("patient.update")
def alert_add(request, pk):
    patient = _get_patient(request, pk)
    message = request.POST.get("message", "").strip()[:160]
    if message:
        PatientAlert.objects.create(patient=patient, message=message, created_by=request.user)
        audit.record(request, "patient.alert_add", patient)
        messages.success(request, "Alert added.")
    return redirect("patients:detail", pk=pk)


@require_POST
@permission_required("patient.update")
def alert_remove(request, pk, alert_pk):
    patient = _get_patient(request, pk)
    PatientAlert.objects.filter(patient=patient, pk=alert_pk).update(is_active=False)
    audit.record(request, "patient.alert_remove", patient)
    return redirect("patients:detail", pk=pk)


@require_POST
@permission_required("patient.update")
def identifier_add(request, pk):
    patient = _get_patient(request, pk)
    form = IdentifierForm(request.POST)
    if form.is_valid():
        kind, value = form.cleaned_data["kind"], form.cleaned_data["value"]
        if kind == PatientIdentifier.Kind.GUARDIAN_CNIC and len(normalize_cnic(value)) != 13:
            messages.error(request, "A CNIC has 13 digits.")
        else:
            services.add_identifier(patient, kind, value)
            audit.record(request, "patient.identifier_add", patient, summary=kind)
            messages.success(request, "Identifier added.")
    return redirect(f"{reverse('patients:detail', args=[pk])}?tab=overview")


@require_POST
@permission_required("patient.update")
def identifier_remove(request, pk, identifier_pk):
    patient = _get_patient(request, pk)
    ident = get_object_or_404(PatientIdentifier, pk=identifier_pk, patient=patient)
    audit.record(request, "patient.identifier_remove", patient, summary=ident.kind)
    ident.delete()
    return redirect("patients:detail", pk=pk)
