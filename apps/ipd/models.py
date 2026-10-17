"""Inpatient department (PRD §10): wards, rooms, beds, admissions, transfers and discharge.

Branch ─▶ Ward ─▶ Room ─▶ Bed. A bed holds at most one patient: BedOccupancy rows are the
occupancy ledger, and a partial unique constraint allows only one open row per bed (and one
per admission), so two desks can never put two patients in the same bed, even in a race.

admitted ──plan discharge──▶ discharge_planned ──release──▶ discharged
   └────────── cancel (admitted by mistake, no charges) ──────────▶ cancelled

Clinical entries (rounds, medication orders) are never edited or deleted: a wrong entry is
marked "entered in error" with a reason, and the discharge summary is signed into numbered
versions, so a correction adds a version instead of overwriting one (CLAUDE.md rule).
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import BranchScopedModel

money = {"max_digits": 12, "decimal_places": 2}


class Ward(BranchScopedModel):
    class Kind(models.TextChoices):
        GENERAL = "general", "General ward"
        PRIVATE = "private", "Private rooms"
        ICU = "icu", "ICU / critical care"
        MATERNITY = "maternity", "Maternity"
        PAEDIATRIC = "paediatric", "Paediatric"
        SURGICAL = "surgical", "Surgical"
        EMERGENCY = "emergency", "Emergency / observation"
        OTHER = "other", "Other"

    name = models.CharField(max_length=80)
    code = models.CharField(max_length=10, help_text="Short code used in bed labels, e.g. GW.")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.GENERAL)
    floor = models.CharField(max_length=30, blank=True)
    department = models.ForeignKey(
        "masterdata.Department", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "code"], name="uniq_ward_code_per_branch")
        ]

    def __str__(self):
        return self.name


class Room(BranchScopedModel):
    class Kind(models.TextChoices):
        WARD = "ward", "Shared ward room"
        SEMI_PRIVATE = "semi_private", "Semi-private"
        PRIVATE = "private", "Private"
        ICU = "icu", "ICU bay"

    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="rooms")
    name = models.CharField(max_length=40, help_text="Room number or name, e.g. 101.")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.WARD)
    daily_rate = models.DecimalField(
        **money, default=Decimal("0"), validators=[MinValueValidator(Decimal("0"))],
        help_text="Bed charge per day for each bed in this room.",
    )  # fmt: skip
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["ward__sort_order", "ward__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["ward", "name"], name="uniq_room_name_per_ward")
        ]

    def __str__(self):
        return f"{self.ward.name} · {self.name}"


class Bed(BranchScopedModel):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        OCCUPIED = "occupied", "Occupied"
        RESERVED = "reserved", "Reserved"
        CLEANING = "cleaning", "Cleaning"
        MAINTENANCE = "maintenance", "Maintenance"
        BLOCKED = "blocked", "Blocked"

    # Statuses staff can set by hand. Occupied is only set by admission and transfer.
    MANUAL_STATUSES = (
        Status.AVAILABLE,
        Status.RESERVED,
        Status.CLEANING,
        Status.MAINTENANCE,
        Status.BLOCKED,
    )

    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="beds")
    code = models.CharField(max_length=20, help_text="Bed label, e.g. G-01.")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.AVAILABLE, db_index=True
    )
    status_note = models.CharField(max_length=120, blank=True)
    status_changed_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["room__ward__sort_order", "room__ward__name", "room__name", "code"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "code"], name="uniq_bed_code_per_branch")
        ]

    def __str__(self):
        return self.code

    @property
    def ward(self):
        return self.room.ward

    @property
    def label(self) -> str:
        return f"{self.code} · {self.room.ward.name}, room {self.room.name}"


class Admission(BranchScopedModel):
    class Status(models.TextChoices):
        ADMITTED = "admitted", "Admitted"
        DISCHARGE_PLANNED = "discharge_planned", "Discharge planned"
        DISCHARGED = "discharged", "Discharged"
        CANCELLED = "cancelled", "Cancelled"

    ACTIVE_STATUSES = (Status.ADMITTED, Status.DISCHARGE_PLANNED)

    class DischargeType(models.TextChoices):
        ROUTINE = "routine", "Routine (advised)"
        LAMA = "lama", "Left against medical advice"
        REFERRED = "referred", "Referred to another facility"
        EXPIRED = "expired", "Expired"
        ABSCONDED = "absconded", "Absconded"

    class Condition(models.TextChoices):
        IMPROVED = "improved", "Improved"
        STABLE = "stable", "Stable"
        UNCHANGED = "unchanged", "Unchanged"
        DETERIORATED = "deteriorated", "Deteriorated"
        EXPIRED = "expired", "Expired"

    number = models.CharField(max_length=20)
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="admissions"
    )
    admitting_doctor = models.ForeignKey(
        "masterdata.DoctorProfile", on_delete=models.PROTECT, related_name="admissions"
    )
    department = models.ForeignKey(
        "masterdata.Department", on_delete=models.PROTECT, related_name="admissions"
    )
    status = models.CharField(
        max_length=18, choices=Status.choices, default=Status.ADMITTED, db_index=True
    )
    admitted_at = models.DateTimeField(default=timezone.now, db_index=True)
    admission_reason = models.CharField(max_length=300)
    admission_diagnosis = models.TextField(blank=True)
    notes = models.CharField(max_length=300, blank=True)
    current_bed = models.ForeignKey(
        Bed, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    attendant_name = models.CharField(max_length=150, blank=True)
    attendant_relation = models.CharField(max_length=40, blank=True)
    attendant_phone = models.CharField(max_length=20, blank=True)

    # The running inpatient bill: a draft invoice that collects bed, service and procedure
    # charges until the final bill is issued at discharge.
    invoice = models.OneToOneField(
        "billing.Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="admission"
    )
    bill_finalized_at = models.DateTimeField(null=True, blank=True)
    bill_finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # Discharge draft. Editable while the stay is open; signed into DischargeSummary versions.
    discharge_type = models.CharField(
        max_length=10, choices=DischargeType.choices, default=DischargeType.ROUTINE
    )
    discharge_condition = models.CharField(
        max_length=12, choices=Condition.choices, blank=True, default=""
    )
    discharge_diagnosis = models.TextField(blank=True)
    hospital_course = models.TextField(blank=True, help_text="Treatment given and progress.")
    discharge_medicines = models.JSONField(default=list, blank=True)
    discharge_advice = models.TextField(blank=True)
    follow_up_date = models.DateField(null=True, blank=True)
    follow_up_instructions = models.TextField(blank=True)
    discharge_planned_at = models.DateTimeField(null=True, blank=True)
    discharge_planned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    discharged_at = models.DateTimeField(null=True, blank=True)
    discharged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    outstanding_override_reason = models.CharField(
        max_length=200, blank=True, help_text="Why the patient left with an unpaid balance."
    )

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-admitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_admission_number"
            ),
            # One open stay per patient, across every branch of the organization.
            models.UniqueConstraint(
                fields=["patient"],
                condition=Q(status__in=["admitted", "discharge_planned"]),
                name="uniq_open_admission_per_patient",
            ),
        ]
        indexes = [models.Index(fields=["branch", "status", "-admitted_at"])]

    def __str__(self):
        return self.number

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def length_of_stay_days(self) -> int:
        """Calendar days in hospital, counting the admission day as day 1."""
        end = self.discharged_at or timezone.now()
        return (timezone.localdate(end) - timezone.localdate(self.admitted_at)).days + 1

    @property
    def stay_end(self):
        return self.discharged_at or self.cancelled_at


class BedOccupancy(TimeStampedModel):
    """Who was in which bed, from when to when. The bed's history and the basis of bed charges."""

    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="occupancies")
    admission = models.ForeignKey(Admission, on_delete=models.PROTECT, related_name="occupancies")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    daily_rate = models.DecimalField(**money, default=Decimal("0"))  # copied from the room
    charged = models.BooleanField(default=False)

    class Meta:
        ordering = ["started_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["bed"], condition=Q(ended_at__isnull=True), name="uniq_open_bed_occupancy"
            ),
            models.UniqueConstraint(
                fields=["admission"],
                condition=Q(ended_at__isnull=True),
                name="uniq_open_occupancy_per_admission",
            ),
        ]

    def __str__(self):
        return f"{self.bed} · {self.admission}"

    @property
    def charge_days(self) -> int:
        """Bed-days for billing: each started calendar day counts once, minimum one."""
        end = self.ended_at or timezone.now()
        return max(1, (timezone.localdate(end) - timezone.localdate(self.started_at)).days)


class BedTransfer(TimeStampedModel):
    class Kind(models.TextChoices):
        BED = "bed", "Bed transfer (same ward)"
        WARD = "ward", "Ward transfer"

    admission = models.ForeignKey(Admission, on_delete=models.PROTECT, related_name="transfers")
    from_bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="+")
    to_bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=4, choices=Kind.choices)
    reason = models.CharField(max_length=200)
    transferred_at = models.DateTimeField(default=timezone.now)
    transferred_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-transferred_at"]

    def __str__(self):
        return f"{self.admission} {self.from_bed} → {self.to_bed}"


class AdmissionDeposit(BranchScopedModel):
    """Advance money taken at or during admission. Applied to the final bill at discharge;
    any amount left over is returned to the attendant and recorded here."""

    number = models.CharField(max_length=20)
    admission = models.ForeignKey(Admission, on_delete=models.PROTECT, related_name="deposits")
    method = models.ForeignKey("billing.PaymentMethod", on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(**money, validators=[MinValueValidator(Decimal("0.01"))])
    reference = models.CharField(max_length=80, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    applied_amount = models.DecimalField(**money, default=Decimal("0"))
    returned_amount = models.DecimalField(**money, default=Decimal("0"))
    returned_at = models.DateTimeField(null=True, blank=True)
    returned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    idempotency_key = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_deposit_number"),
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="uniq_deposit_idempotency",
            ),
        ]

    def __str__(self):
        return self.number

    @property
    def unapplied(self) -> Decimal:
        return self.amount - self.applied_amount - self.returned_amount


class DoctorRound(TimeStampedModel):
    """One ward-round note. Never edited; a wrong note is marked entered in error."""

    admission = models.ForeignKey(Admission, on_delete=models.PROTECT, related_name="rounds")
    doctor = models.ForeignKey(
        "masterdata.DoctorProfile", on_delete=models.PROTECT, related_name="+"
    )
    round_at = models.DateTimeField(default=timezone.now)
    findings = models.TextField(help_text="Subjective and examination findings.")
    plan = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    entered_in_error = models.BooleanField(default=False)
    error_reason = models.CharField(max_length=200, blank=True)
    error_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-round_at"]

    def __str__(self):
        return f"Round {self.admission} {self.round_at:%Y-%m-%d %H:%M}"


class MedicationOrder(TimeStampedModel):
    """An inpatient medicine order on the medication chart. Nurses chart each dose against it
    (apps/nursing). Orders are stopped, never edited: a change is a stop plus a new order."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        STOPPED = "stopped", "Stopped"
        COMPLETED = "completed", "Completed"

    # Times of day each frequency is given on the ward (24 h clock). SOS has no schedule.
    SCHEDULE = {
        "OD": [9],
        "BD": [9, 21],
        "TDS": [8, 14, 20],
        "QID": [6, 12, 18, 22],
        "Q6H": [0, 6, 12, 18],
        "Q8H": [6, 14, 22],
        "HS": [21],
        "WEEKLY": [9],
        "STAT": [],
        "SOS": [],
    }

    admission = models.ForeignKey(
        Admission, on_delete=models.PROTECT, related_name="medication_orders"
    )
    medicine = models.ForeignKey(
        "prescriptions.Medicine", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    medicine_name = models.CharField(max_length=200)
    dose = models.CharField(max_length=40)
    route = models.CharField(max_length=12, default="oral")
    frequency = models.CharField(max_length=8, default="BD")
    instructions = models.CharField(max_length=200, blank=True)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_on = models.DateField(null=True, blank=True, help_text="Last day; empty = until stopped.")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    ordered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    stopped_at = models.DateTimeField(null=True, blank=True)
    stopped_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    stop_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["status", "-starts_at"]

    def __str__(self):
        return f"{self.medicine_name} {self.dose} {self.frequency}"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def frequency_label(self) -> str:
        from apps.prescriptions.models import PrescriptionItem

        return dict(PrescriptionItem.Frequency.choices).get(self.frequency, self.frequency)

    @property
    def route_label(self) -> str:
        from apps.prescriptions.models import Medicine

        return dict(Medicine.Route.choices).get(self.route, self.route)


class DischargeSummary(TimeStampedModel):
    """A signed discharge summary. Version 1 is signed at discharge; amendments add versions."""

    admission = models.ForeignKey(
        Admission, on_delete=models.PROTECT, related_name="summary_versions"
    )
    version = models.PositiveSmallIntegerField()
    content = models.JSONField()
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    amendment_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["admission", "version"], name="uniq_discharge_summary_version"
            )
        ]

    def __str__(self):
        return f"{self.admission} v{self.version}"
