"""Recording vitals and flagging abnormal values.

The flag ranges are common adult screening ranges, used only to draw attention (colour and a
word, never colour alone). They are not a diagnosis. For patients under 18, heart rate,
breathing rate and blood pressure are not flagged because normal values depend on age; the
screen says so instead of showing a misleading "normal".
"""

from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import VitalSign


class VitalsError(ValidationError):
    pass


NORMAL, LOW, HIGH, CRITICAL = "normal", "low", "high", "critical"


@dataclass(frozen=True)
class Band:
    """Values below `low` / above `high` are flagged; beyond the critical limits, critical."""

    low: float | None
    high: float | None
    critical_low: float | None = None
    critical_high: float | None = None
    adult_only: bool = False

    def level(self, value) -> str:
        v = float(value)
        if self.critical_low is not None and v < self.critical_low:
            return CRITICAL
        if self.critical_high is not None and v >= self.critical_high:
            return CRITICAL
        if self.low is not None and v < self.low:
            return LOW
        if self.high is not None and v > self.high:
            return HIGH
        return NORMAL


BANDS = {
    "temperature_c": Band(36.0, 37.5, critical_low=35.0, critical_high=40.0),
    "bp_systolic": Band(90, 139, critical_low=80, critical_high=180, adult_only=True),
    "bp_diastolic": Band(60, 89, critical_low=50, critical_high=120, adult_only=True),
    "pulse": Band(60, 100, critical_low=40, critical_high=130, adult_only=True),
    "respiratory_rate": Band(12, 20, critical_low=8, critical_high=30, adult_only=True),
    "spo2": Band(95, None, critical_low=90),
    "bmi": Band(18.5, 24.9, adult_only=True),
}
GLUCOSE_BANDS = {
    VitalSign.GlucoseContext.FASTING: Band(70, 99, critical_low=54, critical_high=400),
    VitalSign.GlucoseContext.POST_MEAL: Band(70, 139, critical_low=54, critical_high=400),
    VitalSign.GlucoseContext.RANDOM: Band(70, 199, critical_low=54, critical_high=400),
}
LEVEL_LABELS = {LOW: "Low", HIGH: "High", CRITICAL: "Critical", NORMAL: "Normal"}


def is_child(patient) -> bool:
    return patient.age is not None and patient.age < 18


def assess(vitals: VitalSign, patient=None) -> dict[str, str]:
    """{measure: level} for every recorded value that has a band ("normal", "low", ...)."""
    patient = patient or vitals.patient
    child = is_child(patient)
    flags = {}
    for field, band in BANDS.items():
        value = getattr(vitals, field)
        if value is None or (band.adult_only and child):
            continue
        flags[field] = band.level(value)
    if vitals.blood_glucose is not None:
        band = GLUCOSE_BANDS.get(vitals.glucose_context or "random", GLUCOSE_BANDS["random"])
        flags["blood_glucose"] = band.level(vitals.blood_glucose)
    if "bp_systolic" in flags or "bp_diastolic" in flags:
        order = [NORMAL, LOW, HIGH, CRITICAL]
        flags["bp"] = max(flags.get("bp_systolic", NORMAL), flags.get("bp_diastolic", NORMAL),
                          key=order.index)  # fmt: skip
    return flags


def summary(vitals: VitalSign, patient=None) -> list[dict]:
    """Display rows: label, value text, level. Used by the doctor summary and the station."""
    flags = assess(vitals, patient)
    rows = []

    def add(key, label, value, unit=""):
        if value in (None, ""):
            return
        rows.append(
            {
                "key": key,
                "label": label,
                "value": f"{value}{(' ' + unit) if unit else ''}",
                "level": flags.get(key, NORMAL),
                "level_label": LEVEL_LABELS[flags.get(key, NORMAL)],
            }
        )

    if vitals.temperature_c is not None:
        add("temperature_c", "Temp", f"{vitals.temperature_c} °C ({vitals.temperature_f} °F)")
    add("bp", "BP", vitals.bp, "mmHg")
    add("pulse", "Pulse", vitals.pulse, "/min")
    add("respiratory_rate", "Resp. rate", vitals.respiratory_rate, "/min")
    add("spo2", "SpO₂", vitals.spo2, "%")
    add("weight_kg", "Weight", vitals.weight_kg, "kg")
    add("height_cm", "Height", vitals.height_cm, "cm")
    add("bmi", "BMI", vitals.bmi)
    if vitals.blood_glucose is not None:
        add(
            "blood_glucose",
            f"Glucose ({vitals.get_glucose_context_display().lower()})",
            vitals.blood_glucose,
            "mg/dL",
        )
    return rows


def fahrenheit_to_celsius(value) -> Decimal:
    return ((Decimal(value) - 32) / Decimal("1.8")).quantize(Decimal("0.1"))


@transaction.atomic
def record(*, request, patient, data: dict, token=None, encounter=None) -> VitalSign:
    """Save one set of readings. `data` holds model field values (already validated)."""
    if "vitals.record" not in request.sd_perms:
        raise VitalsError("You are not allowed to record vitals.")
    if token is not None and token.patient_id != patient.pk:
        raise VitalsError("That token belongs to another patient.")
    if encounter is not None and encounter.patient_id != patient.pk:
        raise VitalsError("That consultation belongs to another patient.")
    if encounter is None and token is not None:
        encounter = getattr(token, "encounter", None)
    vitals = VitalSign(
        organization=request.organization,
        branch=request.branch,
        patient=patient,
        token=token,
        encounter=encounter,
        recorded_by=request.user,
        **{k: v for k, v in data.items() if k in VitalSign.MEASURES + ["glucose_context", "notes"]},
    )
    if not vitals.has_readings():
        raise VitalsError("Enter at least one reading.")
    if bool(vitals.bp_systolic) != bool(vitals.bp_diastolic):
        raise VitalsError("Enter both the systolic and the diastolic blood pressure.")
    if vitals.bp_systolic and vitals.bp_systolic <= vitals.bp_diastolic:
        raise VitalsError("Systolic pressure must be higher than diastolic.")
    if vitals.blood_glucose is None:
        vitals.glucose_context = ""
    vitals.save()
    return vitals


@transaction.atomic
def mark_error(vitals, *, request, reason: str) -> VitalSign:
    if "vitals.record" not in request.sd_perms:
        raise VitalsError("You are not allowed to correct vitals.")
    if not reason.strip():
        raise VitalsError("Give the reason the reading is wrong.")
    vitals = VitalSign.objects.select_for_update(of=("self",)).get(pk=vitals.pk)
    if vitals.entered_in_error:
        raise VitalsError("This reading is already marked as entered in error.")
    vitals.entered_in_error = True
    vitals.error_reason = reason.strip()[:200]
    vitals.error_marked_by = request.user
    vitals.error_marked_at = timezone.now()
    vitals.save(
        update_fields=[
            "entered_in_error",
            "error_reason",
            "error_marked_by",
            "error_marked_at",
            "updated_at",
        ]
    )
    return vitals


# --- Queries -----------------------------------------------------------------------------------


def valid_for(patient, organization):
    return VitalSign.objects.filter(
        organization=organization, patient=patient, entered_in_error=False
    )


def latest(patient, organization, *, since=None):
    qs = valid_for(patient, organization)
    if since is not None:
        qs = qs.filter(recorded_at__gte=since)
    return qs.select_related("recorded_by").first()


def latest_today(patient, organization):
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return latest(patient, organization, since=start)


TREND_MEASURES = [
    ("bp_systolic", "BP systolic", "mmHg"),
    ("bp_diastolic", "BP diastolic", "mmHg"),
    ("pulse", "Pulse", "/min"),
    ("temperature_c", "Temperature", "°C"),
    ("spo2", "SpO₂", "%"),
    ("respiratory_rate", "Resp. rate", "/min"),
    ("weight_kg", "Weight", "kg"),
    ("bmi", "BMI", ""),
    ("blood_glucose", "Blood glucose", "mg/dL"),
]


def trends(patient, organization, *, limit=12) -> list[dict]:
    """Per measure, the last `limit` values oldest → newest, for the trend charts."""
    readings = list(valid_for(patient, organization).order_by("-recorded_at")[:60])
    readings.reverse()
    result = []
    for field, label, unit in TREND_MEASURES:
        points = [
            (v.recorded_at, float(getattr(v, field)), assess(v, patient).get(field, NORMAL))
            for v in readings
            if getattr(v, field) is not None
        ][-limit:]
        if not points:
            continue
        first, last = points[0][1], points[-1][1]
        result.append(
            {
                "field": field,
                "label": label,
                "unit": unit,
                "points": points,
                "latest": points[-1],
                "change": round(last - first, 1) if len(points) > 1 else None,
            }
        )
    return result
