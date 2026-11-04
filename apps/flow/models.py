"""Patient flow: where an OPD patient is now, and which counter takes each fee.

A PatientVisit follows one queue token: TOKEN_ISSUED → IN_QUEUE → WITH_DOCTOR → LAB_QUEUE /
PHARMACY_QUEUE → COMPLETED. The state field is protected; only the transition methods below move
it, and only services (apps/flow/services.py) call them. Each transition's conditions read the
hospital's HospitalFlowSetting row, so an administrator changes the route without code.

PatientBillItem is a fee the visit owes, routed to a counter by the service's billed_at. It is not
money: the money is the billing.Invoice that collects it. is_paid follows that invoice.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django_fsm import RETURN_VALUE, FSMField, transition

from apps.billing.models import ZERO, InvoiceItem, to_money
from apps.core.scoping import BranchScopedModel, OrgScopedModel
from apps.masterdata.models import money

from .choices import CounterLocation, RxPrintAt, VisitState

__all__ = [
    "CounterLocation",
    "RxPrintAt",
    "VisitState",
    "HospitalFlowSetting",
    "PatientVisit",
    "PatientBillItem",
]


class HospitalFlowSetting(OrgScopedModel):
    """How patients move through this hospital (Settings → Patient flow). One row per
    organization; use services.flow_settings_for(org). Not a global singleton: every hospital on
    a server has its own rules."""

    consultation_fee_billed_at = models.CharField(
        "Doctor's fee is paid at",
        max_length=16,
        choices=CounterLocation.choices,
        default=CounterLocation.RECEPTION,
        help_text="Reception: paid before the patient joins the doctor's queue. Pharmacy: added "
        "to the medicine bill. Lab or Central cash: paid there.",
    )
    doctor_must_click_next = models.BooleanField(
        "Doctor sends the patient on",
        default=True,
        help_text="On: the patient leaves the doctor's queue only when the doctor presses "
        "Checked · Next. Pharmacy and lab can still take a patient by token if the doctor "
        "forgets. Off: signing the consultation sends the patient on by itself.",
    )
    doctor_generates_digital_rx = models.BooleanField(
        "Doctors write prescriptions in SehatDesk",
        default=True,
        help_text="Off: doctors write on paper, so every visit reaches the pharmacy marked "
        "'paper prescription'.",
    )
    rx_print_at = models.CharField(
        "Print the doctor's prescription",
        max_length=10,
        choices=RxPrintAt.choices,
        default=RxPrintAt.NONE,
        help_text="Prints on the printer of the PC where it happens (Settings → Printers), "
        "or opens the print window when that PC has none. The pharmacy prints each one once.",
    )
    pharmacy_handles_manual_rx = models.BooleanField(
        "Pharmacy fills paper prescriptions",
        default=True,
        help_text="On: a patient with a paper prescription goes to the pharmacy queue. Off: the "
        "visit closes after the doctor unless something is still to pay at the pharmacy.",
    )

    class Meta:
        verbose_name = "patient flow settings"
        verbose_name_plural = "patient flow settings"
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="uniq_flow_settings_per_org")
        ]

    def __str__(self):
        return f"Patient flow · {self.organization}"

    @property
    def reception_prints_blank_rx(self) -> bool:
        """Kept on Preferences (opd_print_blank_prescription), which the visit papers already
        read, so there is one switch, not two that can disagree."""
        from apps.organizations.models import preferences_for

        return preferences_for(self.organization).opd_print_blank_prescription


class PatientVisit(BranchScopedModel):
    token = models.OneToOneField(
        "queue.QueueToken", on_delete=models.PROTECT, related_name="flow_visit"
    )
    appointment = models.OneToOneField(
        "appointments.Appointment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="flow_visit",
    )
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="flow_visits"
    )
    state = FSMField(
        max_length=16, choices=VisitState.choices, default=VisitState.TOKEN_ISSUED, protected=True
    )
    is_emergency = models.BooleanField(default=False)
    print_blank_rx = models.BooleanField(
        default=False, help_text="Reception prints an empty prescription with the token."
    )
    is_manual_rx_flagged = models.BooleanField(
        "Paper prescription", default=False, help_text="The doctor wrote on paper."
    )
    digital_rx_generated = models.BooleanField(default=False)
    pulled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    pulled_at = models.DateTimeField(null=True, blank=True)
    pulled_to = models.CharField(max_length=16, choices=CounterLocation.choices, blank=True)
    state_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "state"], name="flow_visit_branch_state"),
            models.Index(fields=["patient", "-created_at"], name="flow_visit_patient"),
        ]

    def __str__(self):
        return f"{self.token.label} · {self.get_state_display()}"

    # --- Conditions: each reads the visit's own rows, never the request -----------------------

    def unpaid(self, counter=None):
        items = self.bill_items.filter(is_paid=False)
        return items.filter(billed_at=counter) if counter else items

    def reception_paid(self) -> bool:
        """An emergency is seen first and pays later."""
        return self.is_emergency or not self.unpaid(CounterLocation.RECEPTION).exists()

    def pharmacy_paid(self) -> bool:
        return not self.unpaid(CounterLocation.PHARMACY).exists()

    def lab_paid(self) -> bool:
        from .services import unpaid_lab_lines

        return self.is_emergency or not unpaid_lab_lines(self)

    def auto_next_allowed(self) -> bool:
        from .services import flow_settings_for

        return not flow_settings_for(self.organization).doctor_must_click_next

    # --- Transitions. Targets that depend on the visit come back from services.route_after_* --

    @transition(field=state, source=VisitState.TOKEN_ISSUED, target=VisitState.IN_QUEUE,
                conditions=[reception_paid])  # fmt: skip
    def admit_to_queue(self):
        """Reception fees are paid (or it is an emergency): the doctor can call the patient."""

    @transition(field=state, source=VisitState.IN_QUEUE, target=VisitState.WITH_DOCTOR)
    def start_consult(self):
        pass

    @transition(field=state, source=VisitState.WITH_DOCTOR,
                target=RETURN_VALUE(VisitState.LAB_QUEUE, VisitState.PHARMACY_QUEUE,
                                    VisitState.COMPLETED))  # fmt: skip
    def doctor_next(self, destination):
        """The doctor pressed Checked · Next."""
        return destination

    @transition(field=state, source=VisitState.WITH_DOCTOR,
                target=RETURN_VALUE(VisitState.LAB_QUEUE, VisitState.PHARMACY_QUEUE,
                                    VisitState.COMPLETED),
                conditions=[auto_next_allowed])  # fmt: skip
    def auto_next(self, destination):
        """The consultation was signed and the hospital does not wait for the doctor's click."""
        return destination

    @transition(field=state, source=[VisitState.IN_QUEUE, VisitState.WITH_DOCTOR],
                target=RETURN_VALUE(VisitState.LAB_QUEUE, VisitState.PHARMACY_QUEUE))  # fmt: skip
    def pull_by_token(self, destination):
        """Fail-safe: the doctor never sent the patient on, so the counter takes them."""
        return destination

    @transition(field=state, source=VisitState.LAB_QUEUE,
                target=RETURN_VALUE(VisitState.PHARMACY_QUEUE, VisitState.COMPLETED),
                conditions=[lab_paid])  # fmt: skip
    def lab_done(self, destination):
        return destination

    @transition(field=state, source=VisitState.PHARMACY_QUEUE, target=VisitState.COMPLETED,
                conditions=[pharmacy_paid])  # fmt: skip
    def pharmacy_done(self):
        pass

    @transition(field=state, source=[VisitState.TOKEN_ISSUED, VisitState.IN_QUEUE,
                                     VisitState.WITH_DOCTOR],
                target=VisitState.CANCELLED)  # fmt: skip
    def cancel(self):
        pass


class PatientBillItem(BranchScopedModel):
    """One fee a visit owes, and the counter that must take it."""

    visit = models.ForeignKey(PatientVisit, on_delete=models.PROTECT, related_name="bill_items")
    kind = models.CharField(
        max_length=16, choices=InvoiceItem.Kind.choices, default=InvoiceItem.Kind.SERVICE
    )
    service = models.ForeignKey(
        "masterdata.Service", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    description = models.CharField(max_length=160)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(**money)
    billed_at = models.CharField(max_length=16, choices=CounterLocation.choices)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Set once an invoice carries the fee; is_paid follows that invoice (services.sync_invoice).
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    invoice_item = models.OneToOneField(
        "billing.InvoiceItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flow_bill_item",
    )
    is_paid = models.BooleanField(default=False)
    collected_by_counter = models.CharField(
        max_length=16, choices=CounterLocation.choices, blank=True
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["visit", "billed_at", "is_paid"], name="flow_item_due")]

    def __str__(self):
        return f"{self.description} · {self.get_billed_at_display()}"

    @property
    def amount(self) -> Decimal:
        return to_money((self.unit_price or ZERO) * self.quantity)
