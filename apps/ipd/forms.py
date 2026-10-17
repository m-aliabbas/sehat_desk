from decimal import Decimal

from django import forms

from apps.billing.models import InvoiceItem
from apps.billing.services import active_methods
from apps.core.forms import StyledFormMixin
from apps.masterdata.models import Department, DoctorProfile, Service
from apps.prescriptions.models import Medicine, PrescriptionItem

from . import services
from .models import Admission, Bed, MedicationOrder, Room, Ward


class BedChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, bed):
        rate = f" · {bed.room.daily_rate:,.0f}/day" if bed.room.daily_rate else ""
        reserved = " · reserved" if bed.status == Bed.Status.RESERVED else ""
        return f"{bed.code} — {bed.room.ward.name}, room {bed.room.name}{rate}{reserved}"


class WardForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Ward
        fields = ["name", "code", "kind", "floor", "department", "sort_order", "is_active"]
        labels = {"is_active": "In use"}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(
            organization=organization, is_active=True
        )
        self.fields["department"].required = False

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()


class RoomForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Room
        fields = ["ward", "name", "kind", "daily_rate", "is_active"]
        labels = {"is_active": "In use", "daily_rate": "Bed charge per day"}

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ward"].queryset = Ward.objects.filter(branch=branch)


class AddBedsForm(StyledFormMixin, forms.Form):
    room = forms.ModelChoiceField(queryset=Room.objects.none())
    prefix = forms.CharField(max_length=12, help_text="Beds are labelled PREFIX-01, PREFIX-02 …")
    count = forms.IntegerField(min_value=1, max_value=100, initial=1, label="How many beds")
    start = forms.IntegerField(min_value=1, initial=1, label="First number")

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room"].queryset = Room.objects.filter(
            branch=branch, is_active=True
        ).select_related("ward")


class BedStatusForm(StyledFormMixin, forms.Form):
    status = forms.ChoiceField(choices=[(s, Bed.Status(s).label) for s in Bed.MANUAL_STATUSES])
    note = forms.CharField(max_length=120, required=False)


class AdmissionForm(StyledFormMixin, forms.Form):
    doctor = forms.ModelChoiceField(queryset=DoctorProfile.objects.none(), label="Admitting doctor")
    department = forms.ModelChoiceField(queryset=Department.objects.none())
    bed = BedChoiceField(queryset=Bed.objects.none(), label="Ward / room / bed")
    admission_reason = forms.CharField(max_length=300, label="Reason for admission")
    admission_diagnosis = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}), required=False, label="Admission diagnosis"
    )
    notes = forms.CharField(max_length=300, required=False)
    attendant_name = forms.CharField(max_length=150, required=False, label="Attendant name")
    attendant_relation = forms.CharField(max_length=40, required=False, label="Relation")
    attendant_phone = forms.CharField(max_length=20, required=False, label="Attendant phone")
    deposit_amount = forms.DecimalField(
        min_value=Decimal("0"), max_digits=12, decimal_places=2, required=False,
        label="Advance deposit",
    )  # fmt: skip
    deposit_method = forms.ModelChoiceField(queryset=None, required=False, label="Paid by")
    deposit_reference = forms.CharField(max_length=80, required=False, label="Reference")

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        org = request.organization
        self.fields["doctor"].queryset = DoctorProfile.objects.filter(
            organization=org, user__is_active=True
        ).select_related("user", "department")
        self.fields["department"].queryset = Department.objects.filter(
            organization=org, is_active=True
        )
        self.fields["bed"].queryset = services.free_beds(request.branch)
        self.fields["deposit_method"].queryset = active_methods(org)
        self.can_take_deposit = "billing.payment.collect" in request.sd_perms
        if not self.can_take_deposit:
            for name in ("deposit_amount", "deposit_method", "deposit_reference"):
                del self.fields[name]

    def clean(self):
        data = super().clean()
        if data.get("deposit_amount") and not data.get("deposit_method"):
            self.add_error("deposit_method", "Choose how the deposit was paid.")
        return data


class TransferForm(StyledFormMixin, forms.Form):
    to_bed = BedChoiceField(queryset=Bed.objects.none(), label="Move to bed")
    reason = forms.CharField(max_length=200, label="Reason for transfer")

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["to_bed"].queryset = services.free_beds(branch)


class DepositForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=12, decimal_places=2)
    method = forms.ModelChoiceField(queryset=None, label="Paid by")
    reference = forms.CharField(max_length=80, required=False)
    idempotency_key = forms.CharField(max_length=40, required=False, widget=forms.HiddenInput)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].queryset = active_methods(organization)


class ChargeForm(StyledFormMixin, forms.Form):
    KINDS = [
        (InvoiceItem.Kind.SERVICE, "Service"),
        (InvoiceItem.Kind.PROCEDURE, "Procedure"),
        (InvoiceItem.Kind.MISC, "Other (diet, consumables …)"),
    ]
    service = forms.ModelChoiceField(
        queryset=Service.objects.none(), required=False, help_text="Or describe the charge below."
    )
    kind = forms.ChoiceField(choices=KINDS, initial=InvoiceItem.Kind.SERVICE)
    description = forms.CharField(max_length=160, required=False)
    unit_price = forms.DecimalField(
        min_value=Decimal("0"), max_digits=12, decimal_places=2, required=False, label="Price",
        help_text="Leave empty to use the service's price.",
    )  # fmt: skip
    quantity = forms.IntegerField(min_value=1, initial=1)

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = Service.objects.filter(
            organization=organization, is_active=True
        )

    def clean(self):
        data = super().clean()
        if not data.get("service") and not (data.get("description") or "").strip():
            raise forms.ValidationError("Choose a service or describe the charge.")
        if not data.get("service") and data.get("unit_price") is None:
            self.add_error("unit_price", "Give the price.")
        return data


class RoundForm(StyledFormMixin, forms.Form):
    findings = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="Findings")
    plan = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class MedicationOrderForm(StyledFormMixin, forms.Form):
    medicine_name = forms.CharField(
        max_length=200, label="Medicine", widget=forms.TextInput(attrs={"list": "medicine-list"})
    )
    dose = forms.CharField(max_length=40, help_text="e.g. 1 g, 500 mg, 10 ml")
    route = forms.ChoiceField(choices=Medicine.Route.choices, initial=Medicine.Route.ORAL)
    frequency = forms.ChoiceField(choices=PrescriptionItem.Frequency.choices, initial="BD")
    instructions = forms.CharField(max_length=200, required=False)
    ends_on = forms.DateField(
        required=False, label="Last day", widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization

    def order_data(self) -> dict:
        data = dict(self.cleaned_data)
        name = data["medicine_name"].strip()
        data["medicine"] = next(
            (
                m
                for m in Medicine.objects.filter(
                    organization=self.organization, is_active=True, brand_name__iexact=name
                )[:5]
                if m.display_name.lower() == name.lower() or m.brand_name.lower() == name.lower()
            ),
            None,
        )
        return data


class DischargeForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Admission
        fields = [
            "discharge_type",
            "discharge_condition",
            "discharge_diagnosis",
            "hospital_course",
            "discharge_advice",
            "follow_up_date",
            "follow_up_instructions",
        ]
        widgets = {
            "discharge_diagnosis": forms.Textarea(attrs={"rows": 2}),
            "hospital_course": forms.Textarea(attrs={"rows": 4}),
            "discharge_advice": forms.Textarea(attrs={"rows": 2}),
            "follow_up_instructions": forms.Textarea(attrs={"rows": 2}),
            "follow_up_date": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {"discharge_condition": "Condition at discharge"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["discharge_condition"].choices = [("", "—")] + list(Admission.Condition.choices)


def parse_discharge_medicines(post) -> list[dict]:
    """Rows from the repeating medicine inputs (med_name, med_dose ...). Empty rows dropped."""
    names = post.getlist("med_name")
    cols = {k: post.getlist(f"med_{k}") for k in ("dose", "frequency", "duration", "instructions")}
    rows = []
    for i, name in enumerate(names):
        name = name.strip()[:200]
        if not name:
            continue
        rows.append(
            {"medicine": name}
            | {k: (v[i].strip()[:120] if i < len(v) else "") for k, v in cols.items()}
        )
    return rows[:40]


def medicines_from_orders(admission) -> list[dict]:
    return [
        {
            "medicine": o.medicine_name,
            "dose": o.dose,
            "frequency": o.frequency_label,
            "duration": "",
            "instructions": o.instructions,
        }
        for o in admission.medication_orders.filter(status=MedicationOrder.Status.ACTIVE)
    ]


class ReasonForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200)
