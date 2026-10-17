"""Prescription builder (inside the consultation), printing, the pharmacy inbox and the
medicine master (Settings → Medicines)."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, scoped
from apps.encounters import services as encounter_services
from apps.encounters.models import Encounter

from . import services
from .forms import MedicineForm, PrescriptionItemForm
from .models import FavoritePrescription, Medicine, Prescription, PrescriptionItem

WRITE = "prescription.create"
MASTER_PERMS = ("admin.masterdata.manage", "pharmacy.inventory.manage")


# --- Builder panel -----------------------------------------------------------------------------


def panel_context(request, encounter, *, form=None, error="", conflict=None):
    rx = services.prescription_for(encounter)
    doctor = encounter_services.doctor_for(request)
    writable = (
        encounter.is_draft
        and encounter_services.can_write(request, encounter)
        and has_perm(request, WRITE)
    )
    previous = (
        Prescription.objects.filter(
            organization=request.organization,
            patient=encounter.patient,
            status=Prescription.Status.SIGNED,
        )
        .exclude(encounter=encounter)
        .select_related("doctor__user")
        .prefetch_related("items")
        .order_by("-signed_at")[:3]
    )
    return {
        "encounter": encounter,
        "patient": encounter.patient,
        "rx": rx,
        "rx_items": list(rx.items.all()) if rx else [],
        "rx_writable": writable,
        "rx_form": form or PrescriptionItemForm(),
        "rx_error": error,
        "rx_conflict": conflict,
        "rx_allergies": encounter.patient.allergies.filter(entered_in_error=False),
        "favorites": FavoritePrescription.objects.filter(doctor=doctor) if doctor else [],
        "previous_rx": previous if writable else [],
        "can_send": rx is not None
        and rx.status == Prescription.Status.SIGNED
        and not rx.sent_to_pharmacy_at
        and encounter_services.can_write(request, encounter),
    }


def _get_encounter(request, pk):
    return get_scoped_or_404(
        Encounter.objects.select_related("patient", "doctor__user"), request, pk=pk
    )


def _reply(request, encounter, **kwargs):
    if request.htmx:
        return render(
            request, "prescriptions/_panel.html", panel_context(request, encounter, **kwargs)
        )
    if kwargs.get("error"):
        messages.error(request, kwargs["error"])
    return redirect(f"{reverse('encounters:detail', args=[encounter.pk])}#sec-prescription")


@require_GET
@permission_required(WRITE)
def medicine_search(request):
    q = request.GET.get("medicine_name", "").strip().lower()
    results = []
    if len(q) >= 2:
        qs = scoped(Medicine, request).filter(is_active=True)
        for word in q.split():
            qs = qs.filter(search_text__contains=word)
        results = qs[:10]
    return render(request, "prescriptions/_medicine_results.html", {"results": results, "q": q})


@require_POST
@permission_required(WRITE)
def item_add(request, pk):
    encounter = _get_encounter(request, pk)
    form = PrescriptionItemForm(request.POST)
    if not form.is_valid():
        return _reply(request, encounter, form=form, error="Check the highlighted fields.")
    medicine = None
    if form.cleaned_data.get("medicine"):
        medicine = get_scoped_or_404(Medicine, request, pk=form.cleaned_data["medicine"])
    try:
        item = services.add_item(
            encounter,
            request=request,
            medicine=medicine,
            data=form.item_data(),
            override_reason=form.cleaned_data.get("override_reason", ""),
        )
    except services.AllergyConflict as err:
        return _reply(request, encounter, form=form, conflict=err.substances)
    except services.PrescriptionError as err:
        return _reply(request, encounter, form=form, error=err.message)
    audit.record(
        request,
        "prescription.item_add",
        item.prescription,
        changes={"item": [None, item.pk]},
        summary=f"{item.prescription.number} line #{item.pk} added",
    )
    if item.allergy_override_reason:
        audit.record(
            request,
            "prescription.allergy_override",
            item.prescription,
            summary=f"{item.prescription.number}: {item.allergy_override_reason[:200]}",
        )
    return _reply(request, encounter)


@require_POST
@permission_required(WRITE)
def item_remove(request, pk, item_pk):
    encounter = _get_encounter(request, pk)
    item = PrescriptionItem.objects.filter(pk=item_pk, prescription__encounter=encounter).first()
    if item is not None:
        rx, item_pk = item.prescription, item.pk
        try:
            services.remove_item(item, request=request)
        except services.PrescriptionError as err:
            return _reply(request, encounter, error=err.message)
        audit.record(
            request,
            "prescription.item_remove",
            rx,
            changes={"item": [item_pk, None]},
            summary=f"{rx.number} line #{item_pk} removed",
        )
    return _reply(request, encounter)


@require_POST
@permission_required(WRITE)
def advice(request, pk):
    encounter = _get_encounter(request, pk)
    try:
        rx = services.set_advice(encounter, request=request, advice=request.POST.get("advice", ""))
    except services.PrescriptionError as err:
        return _reply(request, encounter, error=err.message)
    audit.record(request, "prescription.advice", rx, changes={"fields": ["advice"]},
                 summary=rx.number)  # fmt: skip
    return _reply(request, encounter)


@require_POST
@permission_required(WRITE)
def favorite_save(request, pk):
    encounter = _get_encounter(request, pk)
    try:
        fav = services.save_favorite(encounter, request=request, name=request.POST.get("name", ""))
    except services.PrescriptionError as err:
        return _reply(request, encounter, error=err.message)
    messages.success(request, f"Saved favourite “{fav.name}”.")
    return _reply(request, encounter)


@require_POST
@permission_required(WRITE)
def favorite_apply(request, pk, fav_pk):
    encounter = _get_encounter(request, pk)
    doctor = encounter_services.doctor_for(request)
    fav = FavoritePrescription.objects.filter(pk=fav_pk, doctor=doctor).first()
    if fav is None:
        return _reply(request, encounter, error="That favourite no longer exists.")
    return _copy(request, encounter, fav.items, fav.name)


@require_POST
@permission_required(WRITE)
def favorite_delete(request, fav_pk):
    doctor = encounter_services.doctor_for(request)
    FavoritePrescription.objects.filter(pk=fav_pk, doctor=doctor).delete()
    messages.success(request, "Favourite removed.")
    nxt = request.POST.get("encounter", "")
    if nxt.isdigit():
        return _reply(request, _get_encounter(request, nxt))
    return redirect("encounters:workspace")


@require_POST
@permission_required(WRITE)
def copy_previous(request, pk, rx_pk):
    encounter = _get_encounter(request, pk)
    source = get_scoped_or_404(
        Prescription.objects.filter(patient=encounter.patient), request, all_branches=True, pk=rx_pk
    )
    return _copy(request, encounter, [i.copy_fields() for i in source.items.all()], source.number)


def _copy(request, encounter, items, source_label):
    try:
        added = services.copy_items(encounter, request=request, items=items)
    except services.PrescriptionError as err:
        return _reply(request, encounter, error=err.message)
    if added:
        rx = services.prescription_for(encounter)
        audit.record(
            request,
            "prescription.copy",
            rx,
            changes={"added": added},
            summary=f"{rx.number}: {added} line(s) copied from {source_label}"[:255],
        )
    skipped = len(items) - added
    error = ""
    if skipped:
        error = (
            f"{skipped} line{'s' if skipped != 1 else ''} from {source_label} not copied "
            "(recorded allergy or incomplete). Add them one by one if needed."
        )
    return _reply(request, encounter, error=error)


# --- Signed prescriptions ----------------------------------------------------------------------


@require_POST
@permission_required(WRITE)
def send(request, pk):
    rx = get_scoped_or_404(Prescription, request, pk=pk)
    try:
        services.send_to_pharmacy(rx, request=request)
    except services.PrescriptionError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "prescription.send", rx, summary=rx.number)
        messages.success(request, f"{rx.number} sent to the pharmacy.")
    return redirect(f"{reverse('encounters:detail', args=[rx.encounter_id])}#sec-prescription")


def _can_view_rx(request):
    return has_perm(request, "consultation.view") or has_perm(request, "pharmacy.sale")


@require_GET
def print_rx(request, pk):
    if not _can_view_rx(request):
        raise PermissionDenied
    rx = get_scoped_or_404(
        Prescription.objects.select_related(
            "patient",
            "doctor__user",
            "doctor__department",
            "doctor__specialty",
            "encounter",
            "branch",
        ),
        request,
        all_branches=True,
        pk=pk,
    )
    audit.record(request, "prescription.print", rx, summary=rx.number)
    encounter = rx.encounter
    return render(
        request,
        "prescriptions/print.html",
        {
            "rx": rx,
            "items": rx.items.all(),
            "patient": rx.patient,
            "diagnoses": encounter.diagnoses.select_related("code"),
            "encounter": encounter,
            "allergies": rx.patient.allergies.filter(entered_in_error=False),
            "vitals": _visit_vitals(request, encounter),
        },
    )


def _visit_vitals(request, encounter):
    from apps.vitals import services as vitals_services

    v = (
        vitals_services.valid_for(encounter.patient, request.organization)
        .filter(
            Q(encounter=encounter)
            | Q(recorded_at__date=timezone.localtime(encounter.started_at).date())
        )
        .first()
    )
    return vitals_services.summary(v) if v else []


@require_GET
@permission_required("pharmacy.sale")
def pharmacy_inbox(request):
    """Old address of the pharmacy inbox; dispensing now lives in the pharmacy queue."""
    return redirect("pharmacy:rx-queue")


# --- Medicine master (Settings → Medicines) ----------------------------------------------------


@require_GET
@permission_required(*MASTER_PERMS, any_of=True)
def medicine_list(request):
    q = request.GET.get("q", "").strip()
    medicines = scoped(Medicine, request)
    if q:
        medicines = medicines.filter(search_text__contains=q.lower())
    return render(
        request,
        "prescriptions/medicine_list.html",
        {"medicines": medicines[:300], "q": q, "settings_tab": "medicines"},
    )


@permission_required(*MASTER_PERMS, any_of=True)
def medicine_form(request, pk=None):
    medicine = (
        get_scoped_or_404(Medicine, request, pk=pk)
        if pk
        else Medicine(organization=request.organization)
    )
    creating = pk is None
    form = MedicineForm(request.POST or None, instance=medicine, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        medicine = form.save()
        audit.record(request, "medicine.create" if creating else "medicine.update", medicine)
        messages.success(request, f"{medicine} saved.")
        return redirect("prescriptions:medicine-list")
    return render(
        request,
        "masterdata/simple_form.html",
        {
            "form": form,
            "title": "New medicine" if creating else f"Edit {medicine}",
            "cancel_url": reverse("prescriptions:medicine-list"),
            "settings_tab": "medicines",
            "breadcrumbs": [
                {"label": "Settings", "url": "/settings/"},
                {"label": "Medicines", "url": reverse("prescriptions:medicine-list")},
                {"label": "New" if creating else "Edit"},
            ],
        },
    )


@require_POST
@permission_required(*MASTER_PERMS, any_of=True)
def medicine_toggle(request, pk):
    medicine = get_scoped_or_404(Medicine, request, pk=pk)
    medicine.is_active = not medicine.is_active
    medicine.save(update_fields=["is_active", "updated_at"])
    audit.record(
        request, f"medicine.{'activate' if medicine.is_active else 'deactivate'}", medicine
    )
    messages.success(
        request, f"{medicine} is now {'active' if medicine.is_active else 'inactive'}."
    )
    return redirect("prescriptions:medicine-list")
