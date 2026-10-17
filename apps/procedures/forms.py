from django import forms

from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped
from apps.masterdata.models import DoctorProfile, Service
from apps.patients.models import Patient


class ProcedureChoiceField(forms.ModelChoiceField):
    def __init__(self, *args, branch=None, **kwargs):
        self.branch = branch
        super().__init__(*args, **kwargs)

    def label_from_instance(self, obj):
        return f"{obj.name} · {obj.price_for(self.branch):,.2f}"


class ProcedureOrderForm(StyledFormMixin, forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), widget=forms.HiddenInput)
    service = ProcedureChoiceField(
        queryset=Service.objects.none(), label="Procedure", empty_label="Choose a procedure"
    )
    performed_by = forms.ModelChoiceField(
        queryset=DoctorProfile.objects.none(),
        required=False,
        label="Performed by",
        empty_label="Not a doctor / not yet known",
        help_text="The doctor earns the procedure commission set in their pay profile.",
    )
    quantity = forms.IntegerField(min_value=1, max_value=100, initial=1)
    unit_price = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        label="Price charged (each)",
        help_text="Leave empty to charge the list price.",
    )
    price_reason = forms.CharField(max_length=200, required=False, label="Reason for price change")
    notes = forms.CharField(
        max_length=300, required=False, label="Site / notes", help_text="e.g. tooth 36, left knee"
    )

    def __init__(self, *args, request, with_patient=True, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["service"]
        field.queryset = scoped(Service, request).filter(is_procedure=True, is_active=True)
        field.branch = request.branch
        self.fields["performed_by"].queryset = scoped(DoctorProfile, request).select_related("user")
        if with_patient:
            self.fields["patient"].queryset = scoped(Patient, request).filter(is_active=True)
        else:
            del self.fields["patient"]


class CompleteForm(StyledFormMixin, forms.Form):
    findings = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False, label="Findings / notes"
    )


class CancelForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200)
