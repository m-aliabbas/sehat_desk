"""Procedures added to a patient (Phase 33): ECG, EEG, ECHO, ultrasound, operations, dental work.

The catalogue is masterdata.Service rows marked is_procedure (Settings → Services), so prices
and branch prices are managed in one place. Ordering a procedure copies the list price, lets
authorised staff charge a different price with a reason, and issues an invoice (one line of
kind "procedure" carrying the performing doctor) that the patient pays at any billing desk.

ordered ──complete──▶ completed
   └──cancel (invoice holds no money)──▶ cancelled
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.scoping import BranchScopedModel
from apps.masterdata.models import money


class PatientProcedure(BranchScopedModel):
    class Status(models.TextChoices):
        ORDERED = "ordered", "Ordered"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    number = models.CharField(max_length=20)
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="procedures"
    )
    encounter = models.ForeignKey(
        "encounters.Encounter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="procedures",
        help_text="The consultation it was added from, when there is one.",
    )
    service = models.ForeignKey(
        "masterdata.Service", on_delete=models.PROTECT, related_name="patient_procedures"
    )
    # Copied when ordered, so later catalogue changes never alter this record.
    name = models.CharField(max_length=120)
    performed_by = models.ForeignKey(
        "masterdata.DoctorProfile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="procedures",
    )
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    list_price = models.DecimalField(**money, help_text="Catalogue price when ordered.")
    unit_price = models.DecimalField(**money, help_text="Price charged to this patient.")
    price_change_reason = models.CharField(max_length=200, blank=True)
    notes = models.CharField(max_length=300, blank=True, help_text="e.g. tooth 36, left knee")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ORDERED)
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    findings = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_patient_procedure_number"
            )
        ]
        indexes = [models.Index(fields=["branch", "status", "-created_at"])]

    def __str__(self):
        return f"{self.number} · {self.name}"

    @property
    def total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))

    @property
    def price_changed(self) -> bool:
        return self.unit_price != self.list_price

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.ORDERED
