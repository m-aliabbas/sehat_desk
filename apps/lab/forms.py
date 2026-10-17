from django import forms
from django.forms import inlineformset_factory

from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped
from apps.masterdata.models import DoctorProfile
from apps.patients.models import Patient

from .models import LabOrder, LabPanel, LabReferenceRange, LabTest, LabTestCategory


class LabOrderForm(StyledFormMixin, forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), widget=forms.HiddenInput)
    tests = forms.ModelMultipleChoiceField(
        queryset=LabTest.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    panels = forms.ModelMultipleChoiceField(
        queryset=LabPanel.objects.none(), required=False, widget=forms.CheckboxSelectMultiple
    )
    priority = forms.ChoiceField(choices=LabOrder.Priority.choices, initial="routine")
    doctor = forms.ModelChoiceField(
        queryset=DoctorProfile.objects.none(), required=False, label="Ordering doctor (optional)"
    )
    clinical_notes = forms.CharField(
        max_length=300, required=False, label="Clinical notes for the lab"
    )

    def __init__(self, *args, request, with_patient=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tests"].queryset = scoped(LabTest, request).filter(is_active=True)
        self.fields["panels"].queryset = scoped(LabPanel, request).filter(is_active=True)
        self.fields["doctor"].queryset = scoped(DoctorProfile, request).filter(is_active=True)
        if with_patient:
            self.fields["patient"].queryset = scoped(Patient, request).filter(is_active=True)
        else:
            del self.fields["patient"]
            del self.fields["doctor"]

    def clean(self):
        data = super().clean()
        if not data.get("tests") and not data.get("panels"):
            raise forms.ValidationError("Choose at least one test or panel.")
        return data


class LabTestForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = LabTest
        fields = [
            "code", "name", "category", "sample_type", "price", "turnaround_hours",
            "result_type", "unit", "decimals", "options", "normal_value", "method",
        ]  # fmt: skip

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = request.organization
        self.fields["category"].queryset = scoped(LabTestCategory, request)

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        clash = LabTest.objects.filter(organization=self.organization, code=code).exclude(
            pk=self.instance.pk
        )
        if clash.exists():
            raise forms.ValidationError("Another test already uses this code.")
        return code

    def clean(self):
        data = super().clean()
        if data.get("result_type") == LabTest.ResultType.CHOICE and not data.get("options"):
            self.add_error("options", "List the options, separated by commas.")
        return data


RangeFormSet = inlineformset_factory(
    LabTest,
    LabReferenceRange,
    fields=[
        "gender",
        "age_min_years",
        "age_max_years",
        "low",
        "high",
        "critical_low",
        "critical_high",
    ],  # fmt: skip
    extra=2,
    can_delete=True,
)


class LabPanelForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = LabPanel
        fields = ["code", "name", "price", "tests"]
        widgets = {"tests": forms.CheckboxSelectMultiple}

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = request.organization
        self.fields["tests"].queryset = scoped(LabTest, request).filter(is_active=True)

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        clash = LabPanel.objects.filter(organization=self.organization, code=code).exclude(
            pk=self.instance.pk
        )
        if clash.exists():
            raise forms.ValidationError("Another panel already uses this code.")
        return code


class ReasonForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200)


class AttachmentForm(forms.Form):
    file = forms.FileField()
