"""Medicine master and prescriptions (PRD §6.3).

Medicine is the shared medicine master: doctors prescribe from it, and pharmacy products
(Phase 12) point at it, so a prescription line can be matched to stock.

A prescription belongs to one consultation. It is a draft while the consultation is a draft,
and is signed together with the note. Each signed EncounterVersion keeps a copy of the lines,
so an amendment after reopening never loses what was signed. Item text (name, strength, form)
is copied from the medicine when added, so later master edits never change old prescriptions.
"""

import math
import re
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import BranchScopedModel, OrgScopedModel


class Medicine(OrgScopedModel):
    class Form(models.TextChoices):
        TABLET = "tablet", "Tablet"
        CAPSULE = "capsule", "Capsule"
        SYRUP = "syrup", "Syrup / suspension"
        INJECTION = "injection", "Injection"
        DROPS = "drops", "Drops"
        CREAM = "cream", "Cream / ointment"
        INHALER = "inhaler", "Inhaler"
        SACHET = "sachet", "Sachet"
        SUPPOSITORY = "suppository", "Suppository"
        OTHER = "other", "Other"

    class Route(models.TextChoices):
        ORAL = "oral", "Oral"
        IV = "iv", "Intravenous (IV)"
        IM = "im", "Intramuscular (IM)"
        SC = "sc", "Subcutaneous (SC)"
        TOPICAL = "topical", "Topical"
        INHALED = "inhaled", "Inhaled"
        EYE = "eye", "Eye"
        EAR = "ear", "Ear"
        NASAL = "nasal", "Nasal"
        RECTAL = "rectal", "Rectal"
        SUBLINGUAL = "sublingual", "Sublingual"
        OTHER = "other", "Other"

    # Countable forms: the quantity can be suggested from dose × frequency × duration.
    COUNTABLE_FORMS = {Form.TABLET, Form.CAPSULE, Form.SACHET, Form.SUPPOSITORY}

    brand_name = models.CharField(max_length=120)
    generic_name = models.CharField(max_length=160)
    strength = models.CharField(max_length=60, blank=True, help_text="e.g. 500 mg, 250 mg/5 ml")
    form = models.CharField(max_length=12, choices=Form.choices, default=Form.TABLET)
    route = models.CharField(max_length=12, choices=Route.choices, default=Route.ORAL)
    manufacturer = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    search_text = models.CharField(max_length=300, editable=False, blank=True, db_index=True)

    class Meta:
        ordering = ["brand_name", "strength"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "brand_name", "strength", "form"],
                name="uniq_medicine_brand_strength_form",
            )
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        self.search_text = f"{self.brand_name} {self.generic_name} {self.strength}".lower()
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        return " ".join(p for p in [self.brand_name, self.strength] if p)


class Prescription(BranchScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SIGNED = "signed", "Signed"

    number = models.CharField(max_length=20)
    encounter = models.OneToOneField(
        "encounters.Encounter", on_delete=models.PROTECT, related_name="prescription"
    )
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="prescriptions"
    )
    doctor = models.ForeignKey(
        "masterdata.DoctorProfile", on_delete=models.PROTECT, related_name="prescriptions"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    advice = models.TextField(blank=True, help_text="General advice printed under the medicines.")
    signed_at = models.DateTimeField(null=True, blank=True)
    sent_to_pharmacy_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Dispense(models.TextChoices):
        PENDING = "pending", "Not dispensed"
        PARTIAL = "partial", "Partly dispensed"
        DISPENSED = "dispensed", "Dispensed"
        CLOSED = "closed", "Closed (rest not dispensed)"

    # Kept up to date by the pharmacy POS (apps/pharmacy/pos.py) after each sale or return.
    dispense_status = models.CharField(
        max_length=10, choices=Dispense.choices, default=Dispense.PENDING
    )
    dispense_note = models.CharField(
        max_length=200, blank=True, help_text="Why the rest was not dispensed (when closed)."
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_prescription_number"
            )
        ]

    def __str__(self):
        return self.number

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    def snapshot(self) -> list[dict]:
        return [i.snapshot() for i in self.items.all()]


class PrescriptionItem(TimeStampedModel):
    class Frequency(models.TextChoices):
        OD = "OD", "Once daily"
        BD = "BD", "Twice daily"
        TDS = "TDS", "Three times daily"
        QID = "QID", "Four times daily"
        Q6H = "Q6H", "Every 6 hours"
        Q8H = "Q8H", "Every 8 hours"
        HS = "HS", "At bedtime"
        SOS = "SOS", "When needed"
        STAT = "STAT", "Once, now"
        WEEKLY = "WEEKLY", "Once a week"

    PER_DAY = {"OD": 1, "BD": 2, "TDS": 3, "QID": 4, "Q6H": 4, "Q8H": 3, "HS": 1,
               "WEEKLY": Decimal(1) / 7}  # fmt: skip

    class DurationUnit(models.TextChoices):
        DAYS = "days", "Days"
        WEEKS = "weeks", "Weeks"
        MONTHS = "months", "Months"

    UNIT_DAYS = {"days": 1, "weeks": 7, "months": 30}

    class Food(models.TextChoices):
        ANY = "", "—"
        BEFORE = "before", "Before food"
        AFTER = "after", "After food"
        WITH = "with", "With food"
        EMPTY = "empty", "Empty stomach"

    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="items")
    medicine = models.ForeignKey(
        Medicine, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    medicine_name = models.CharField(max_length=200)
    generic_name = models.CharField(max_length=160, blank=True)
    form = models.CharField(max_length=12, choices=Medicine.Form.choices, blank=True)
    dose = models.CharField(max_length=40, help_text="e.g. 1 tablet, 5 ml, 2 puffs")
    route = models.CharField(max_length=12, choices=Medicine.Route.choices, default="oral")
    frequency = models.CharField(max_length=8, choices=Frequency.choices, default=Frequency.BD)
    duration_value = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    duration_unit = models.CharField(
        max_length=8, choices=DurationUnit.choices, default=DurationUnit.DAYS
    )
    quantity = models.PositiveIntegerField(null=True, blank=True)
    food = models.CharField(max_length=8, choices=Food.choices, blank=True, default="")
    instructions = models.CharField(max_length=200, blank=True)
    allergy_override_reason = models.CharField(
        max_length=200, blank=True, help_text="Why it is prescribed despite a recorded allergy."
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]

    def __str__(self):
        return self.medicine_name

    @property
    def duration_label(self) -> str:
        if not self.duration_value:
            return ""
        unit = self.get_duration_unit_display().lower()
        return f"{self.duration_value} {unit[:-1] if self.duration_value == 1 else unit}"

    @property
    def directions(self) -> str:
        parts = [
            self.dose,
            self.get_frequency_display().lower(),
            self.get_food_display() if self.food else "",
            f"for {self.duration_label}" if self.duration_label else "",
        ]
        return " · ".join(p for p in parts if p)

    def snapshot(self) -> dict:
        return {
            "medicine": self.medicine_name,
            "generic": self.generic_name,
            "dose": self.dose,
            "route": self.route,
            "frequency": self.frequency,
            "duration": self.duration_label,
            "quantity": self.quantity,
            "food": self.food,
            "instructions": self.instructions,
            "allergy_override": self.allergy_override_reason,
        }

    def copy_fields(self) -> dict:
        """Fields a favourite or a reused prescription copies."""
        return {
            "medicine_id": self.medicine_id,
            "medicine_name": self.medicine_name,
            "generic_name": self.generic_name,
            "form": self.form,
            "dose": self.dose,
            "route": self.route,
            "frequency": self.frequency,
            "duration_value": self.duration_value,
            "duration_unit": self.duration_unit,
            "quantity": self.quantity,
            "food": self.food,
            "instructions": self.instructions,
        }


def suggest_quantity(form, dose, frequency, duration_value, duration_unit):
    """Units to dispense for countable forms (tablets, capsules ...), else None.

    "1", "2 tabs", "1/2" and "0.5" are understood as the number of units per dose.
    """
    if form not in Medicine.COUNTABLE_FORMS or not duration_value:
        return None
    per_day = PrescriptionItem.PER_DAY.get(frequency)
    match = re.match(r"\s*(\d+(?:\.\d+)?)(?:\s*/\s*(\d+))?", dose or "")
    if per_day is None or not match:
        return None
    units = Decimal(match.group(1))
    if match.group(2):
        units = units / Decimal(match.group(2))
    days = duration_value * PrescriptionItem.UNIT_DAYS.get(duration_unit, 1)
    return math.ceil(units * Decimal(per_day) * days) or None


class FavoritePrescription(OrgScopedModel):
    """A doctor's saved set of prescription lines, applied in one click."""

    doctor = models.ForeignKey(
        "masterdata.DoctorProfile", on_delete=models.CASCADE, related_name="favorite_prescriptions"
    )
    name = models.CharField(max_length=80)
    items = models.JSONField(default=list)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["doctor", "name"], name="uniq_favorite_rx_name")
        ]

    def __str__(self):
        return self.name
