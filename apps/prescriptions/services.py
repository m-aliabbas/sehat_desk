"""Prescription writing. Views call these; every write checks the consultation is the
signed-in doctor's own open draft (encounters.services._require_writable) and the
prescription.create permission."""

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.core.models import next_number
from apps.encounters import services as encounter_services
from apps.encounters.models import Encounter

from .models import FavoritePrescription, Medicine, Prescription, PrescriptionItem, suggest_quantity


class PrescriptionError(ValidationError):
    pass


class AllergyConflict(PrescriptionError):
    """The medicine matches a recorded allergy; the doctor must give a reason to continue."""

    def __init__(self, substances):
        self.substances = substances
        super().__init__(
            "Recorded allergy: " + ", ".join(substances) + ". Give a reason to prescribe anyway."
        )


ITEM_FIELDS = [
    "medicine_name", "generic_name", "form", "dose", "route", "frequency", "duration_value",
    "duration_unit", "quantity", "food", "instructions",
]  # fmt: skip


def _require(request, encounter):
    """Check against the current row, locked, so a just-signed consultation cannot change."""
    encounter = Encounter.objects.select_for_update(of=("self",)).get(pk=encounter.pk)
    try:
        encounter_services._require_writable(request, encounter)
    except encounter_services.EncounterError as err:
        raise PrescriptionError(err.message) from err
    if "prescription.create" not in request.sd_perms:
        raise PrescriptionError("You are not allowed to write prescriptions.")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 4}


def allergy_conflicts(patient, *names: str) -> list[str]:
    """Recorded allergies whose substance matches a word of the medicine's brand or generic
    name (whole word or part of one, 4+ letters): "Sulfa" matches "Sulfamethoxazole".
    A screen for typed allergies only; it does not know drug classes."""
    med_words = set().union(*(_words(n) for n in names if n))
    hits = []
    for allergy in patient.allergies.filter(entered_in_error=False):
        substance_words = _words(allergy.substance)
        if substance_words & med_words or any(
            s in m or m in s for s in substance_words for m in med_words
        ):
            hits.append(allergy.substance)
    return hits


def prescription_for(encounter) -> Prescription | None:
    return Prescription.objects.filter(encounter=encounter).first()


def _get_or_create(encounter, request) -> Prescription:
    rx = prescription_for(encounter)
    if rx is not None:
        return rx
    year = timezone.localdate().year
    return Prescription.objects.create(
        organization=encounter.organization,
        branch=encounter.branch,
        number=f"RX-{year}-{next_number(encounter.organization, f'prescription:{year}'):05d}",
        encounter=encounter,
        patient=encounter.patient,
        doctor=encounter.doctor,
    )


def _next_order(rx) -> int:
    return (rx.items.aggregate(m=Max("sort_order"))["m"] or 0) + 1


@transaction.atomic
def add_item(
    encounter, *, request, medicine=None, data: dict, override_reason=""
) -> PrescriptionItem:
    """Add one line. With a medicine from the master, its names, form and route are copied."""
    _require(request, encounter)
    data = {k: v for k, v in data.items() if k in ITEM_FIELDS}
    if medicine is not None:
        if medicine.organization_id != encounter.organization_id or not medicine.is_active:
            raise PrescriptionError("That medicine is not available.")
        data["medicine_name"] = medicine.display_name
        data["generic_name"] = medicine.generic_name
        data["form"] = medicine.form
        data["route"] = data.get("route") or medicine.route
    if not (data.get("medicine_name") or "").strip():
        raise PrescriptionError("Choose a medicine or type its name.")
    if not (data.get("dose") or "").strip():
        raise PrescriptionError("Enter the dose, e.g. 1 tablet or 5 ml.")
    conflicts = allergy_conflicts(
        encounter.patient, data.get("medicine_name", ""), data.get("generic_name", "")
    )
    if conflicts and not override_reason.strip():
        raise AllergyConflict(conflicts)
    if data.get("quantity") in (None, ""):
        data["quantity"] = suggest_quantity(
            data.get("form"), data.get("dose"), data.get("frequency"),
            data.get("duration_value"), data.get("duration_unit", "days"),
        )  # fmt: skip
    rx = _get_or_create(encounter, request)
    return PrescriptionItem.objects.create(
        prescription=rx,
        medicine=medicine,
        allergy_override_reason=override_reason.strip()[:200] if conflicts else "",
        sort_order=_next_order(rx),
        **{k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()},
    )


@transaction.atomic
def remove_item(item, *, request) -> None:
    _require(request, item.prescription.encounter)
    item.delete()  # draft lines only; signed lines live on in the EncounterVersion copy


@transaction.atomic
def set_advice(encounter, *, request, advice: str) -> Prescription:
    _require(request, encounter)
    rx = _get_or_create(encounter, request)
    rx.advice = advice.strip()[:2000]
    rx.save(update_fields=["advice", "updated_at"])
    return rx


@transaction.atomic
def copy_items(encounter, *, request, items: list[dict]) -> int:
    """Add copied lines (a favourite or an earlier prescription). Allergy matches are skipped,
    so a copy never bypasses the allergy check. Returns the number added."""
    _require(request, encounter)
    rx = _get_or_create(encounter, request)
    added = 0
    for data in items:
        medicine = None
        if data.get("medicine_id"):
            medicine = Medicine.objects.filter(
                pk=data["medicine_id"], organization=encounter.organization, is_active=True
            ).first()
        fields = {k: data.get(k) for k in ITEM_FIELDS if data.get(k) is not None}
        if not fields.get("medicine_name") or not fields.get("dose"):
            continue
        if allergy_conflicts(
            encounter.patient, fields.get("medicine_name", ""), fields.get("generic_name", "")
        ):
            continue
        PrescriptionItem.objects.create(
            prescription=rx, medicine=medicine, sort_order=_next_order(rx), **fields
        )
        added += 1
    return added


@transaction.atomic
def save_favorite(encounter, *, request, name: str) -> FavoritePrescription:
    _require(request, encounter)
    rx = prescription_for(encounter)
    if rx is None or not rx.items.exists():
        raise PrescriptionError("Add medicines before saving a favourite.")
    if not name.strip():
        raise PrescriptionError("Name the favourite, e.g. Adult URTI.")
    fav, _ = FavoritePrescription.objects.update_or_create(
        organization=encounter.organization,
        doctor=encounter.doctor,
        name=name.strip()[:80],
        defaults={"items": [i.copy_fields() for i in rx.items.all()]},
    )
    return fav


# --- Signing (called by encounters.services) ---------------------------------------------------


def sign_for(encounter) -> None:
    rx = prescription_for(encounter)
    if rx is not None and rx.items.exists():
        rx.status = Prescription.Status.SIGNED
        rx.signed_at = timezone.now()
        rx.save(update_fields=["status", "signed_at", "updated_at"])


def reopen_for(encounter) -> None:
    """Back to draft for an amendment. A sent prescription must be sent again once re-signed,
    so the pharmacy never dispenses from a version under amendment."""
    rx = prescription_for(encounter)
    if rx is not None:
        rx.status = Prescription.Status.DRAFT
        rx.sent_to_pharmacy_at = None
        rx.sent_by = None
        rx.save(update_fields=["status", "sent_to_pharmacy_at", "sent_by", "updated_at"])


@transaction.atomic
def send_to_pharmacy(rx, *, request) -> Prescription:
    rx = Prescription.objects.select_for_update(of=("self",)).get(pk=rx.pk)
    if not encounter_services.can_write(request, rx.encounter):
        raise PrescriptionError("Only the prescribing doctor can send this prescription.")
    if rx.status != Prescription.Status.SIGNED:
        raise PrescriptionError("Complete and sign the consultation before sending.")
    if rx.sent_to_pharmacy_at:
        raise PrescriptionError(f"{rx.number} was already sent to the pharmacy.")
    rx.sent_to_pharmacy_at = timezone.now()
    rx.sent_by = request.user
    rx.save(update_fields=["sent_to_pharmacy_at", "sent_by", "updated_at"])
    return rx
