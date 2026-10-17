from django import forms

from apps.billing.services import active_methods
from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped
from apps.masterdata.models import Service

from .models import AttendanceRecord, CommissionRule, PayProfile, StaffLedgerEntry


class PayProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PayProfile
        fields = [
            "pay_type",
            "monthly_salary",
            "daily_rate",
            "deduct_absences",
            "work_start",
            "late_grace_minutes",
            "weekly_off",
            "consultation_percent",
            "procedure_percent",
            "lab_referral_percent",
            "is_active",
        ]
        widgets = {"work_start": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}


class CommissionRuleForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CommissionRule
        fields = ["service", "percent", "fixed_amount"]

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = scoped(Service, request).filter(is_active=True)
        self.fields["service"].label = "Procedure / service"


class PayStaffForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(
        choices=[
            (StaffLedgerEntry.Kind.PAYMENT, "Salary / commission payout"),
            (StaffLedgerEntry.Kind.ADVANCE, "Advance"),
        ],
        label="Type",
    )
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    method = forms.ModelChoiceField(queryset=None, label="Paid by", empty_label=None)
    reference = forms.CharField(max_length=80, required=False)
    note = forms.CharField(max_length=200, required=False)
    idempotency_key = forms.CharField(max_length=40, widget=forms.HiddenInput)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].queryset = active_methods(request.organization)


class OpeningBalanceForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Positive: the hospital already owes this person. Negative: they owe "
        "(e.g. an earlier advance).",
    )
    note = forms.CharField(max_length=200, required=False)


class AdjustmentsForm(StyledFormMixin, forms.Form):
    bonus = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0, initial=0)
    deduction = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, initial=0, label="Other deduction"
    )
    note = forms.CharField(max_length=200, required=False, label="Reason")


class PayrollCreateForm(StyledFormMixin, forms.Form):
    month = forms.CharField(
        widget=forms.TextInput(attrs={"type": "month"}), help_text="Month to pay, e.g. 2026-10."
    )

    def clean_month(self):
        value = self.cleaned_data["month"]
        try:
            year, month = (int(x) for x in value.split("-"))
            if not 1 <= month <= 12 or year < 2000:
                raise ValueError
        except ValueError as err:
            raise forms.ValidationError("Choose a month.") from err
        return year, month


STATUS_CHOICES = AttendanceRecord.Status.choices
