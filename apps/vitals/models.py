"""Vital signs (PRD §6.2). One row is one set of readings taken at one time.

Readings are clinical records: they are never edited or deleted. A wrong entry is marked
"entered in error" with a reason (like allergies) and a corrected set is recorded instead.
Temperature is stored in °C and weight/height in kg/cm; the form also accepts °F.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.scoping import BranchScopedModel


def _range(low, high):
    return [MinValueValidator(Decimal(str(low))), MaxValueValidator(Decimal(str(high)))]


class VitalSign(BranchScopedModel):
    class GlucoseContext(models.TextChoices):
        RANDOM = "random", "Random"
        FASTING = "fasting", "Fasting"
        POST_MEAL = "post_meal", "2 h after meal"

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="vitals")
    encounter = models.ForeignKey(
        "encounters.Encounter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="vitals",
    )
    token = models.ForeignKey(
        "queue.QueueToken", on_delete=models.PROTECT, null=True, blank=True, related_name="vitals"
    )
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    # Plausibility limits only stop typing mistakes; clinical flags come from services.assess().
    temperature_c = models.DecimalField(
        "Temperature (°C)", max_digits=4, decimal_places=1, null=True, blank=True,
        validators=_range(30, 45),
    )  # fmt: skip
    bp_systolic = models.PositiveSmallIntegerField(
        "BP systolic", null=True, blank=True, validators=_range(40, 300)
    )
    bp_diastolic = models.PositiveSmallIntegerField(
        "BP diastolic", null=True, blank=True, validators=_range(20, 200)
    )
    pulse = models.PositiveSmallIntegerField(
        "Pulse (/min)", null=True, blank=True, validators=_range(20, 250)
    )
    respiratory_rate = models.PositiveSmallIntegerField(
        "Respiratory rate (/min)", null=True, blank=True, validators=_range(4, 80)
    )
    spo2 = models.PositiveSmallIntegerField(
        "SpO₂ (%)", null=True, blank=True, validators=_range(50, 100)
    )
    weight_kg = models.DecimalField(
        "Weight (kg)", max_digits=5, decimal_places=1, null=True, blank=True,
        validators=_range("0.3", 400),
    )  # fmt: skip
    height_cm = models.DecimalField(
        "Height (cm)", max_digits=4, decimal_places=1, null=True, blank=True,
        validators=_range(30, 250),
    )  # fmt: skip
    bmi = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True, editable=False)
    blood_glucose = models.PositiveSmallIntegerField(
        "Blood glucose (mg/dL)", null=True, blank=True, validators=_range(10, 1000)
    )
    glucose_context = models.CharField(
        max_length=10, choices=GlucoseContext.choices, default=GlucoseContext.RANDOM, blank=True
    )
    notes = models.CharField(
        max_length=300, blank=True, help_text="e.g. pain score, position, patient distressed."
    )

    entered_in_error = models.BooleanField(default=False)
    error_reason = models.CharField(max_length=200, blank=True)
    error_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    error_marked_at = models.DateTimeField(null=True, blank=True)

    MEASURES = [
        "temperature_c", "bp_systolic", "bp_diastolic", "pulse", "respiratory_rate", "spo2",
        "weight_kg", "height_cm", "blood_glucose",
    ]  # fmt: skip

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["patient", "-recorded_at"])]

    def __str__(self):
        return f"Vitals {self.patient_id} {self.recorded_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        self.bmi = compute_bmi(self.weight_kg, self.height_cm)
        super().save(*args, **kwargs)

    @property
    def temperature_f(self):
        if self.temperature_c is None:
            return None
        return (self.temperature_c * Decimal("1.8") + 32).quantize(Decimal("0.1"))

    @property
    def bp(self) -> str:
        if self.bp_systolic and self.bp_diastolic:
            return f"{self.bp_systolic}/{self.bp_diastolic}"
        return ""

    def has_readings(self) -> bool:
        return any(getattr(self, f) is not None for f in self.MEASURES)


def compute_bmi(weight_kg, height_cm):
    """BMI = kg / m², one decimal. None unless both are known."""
    if not weight_kg or not height_cm:
        return None
    metres = Decimal(height_cm) / 100
    return (Decimal(weight_kg) / (metres * metres)).quantize(Decimal("0.1"), ROUND_HALF_UP)
