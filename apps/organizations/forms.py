from django import forms

from apps.core.forms import StyledFormMixin

from .models import Branch, Organization, OrganizationPreferences


class OrganizationForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Organization
        fields = [
            "name",
            "registration_number",
            "phone",
            "email",
            "address",
            "logo",
            "timezone",
            "currency",
            "language",
            "patient_id_prefix",
            "receipt_prefix",
            "report_header",
            "report_footer",
            "print_terms",
        ]
        labels = {"name": "Hospital name", "patient_id_prefix": "Patient ID prefix"}
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "report_header": forms.Textarea(attrs={"rows": 2}),
            "report_footer": forms.Textarea(attrs={"rows": 2}),
            "print_terms": forms.Textarea(attrs={"rows": 3}),
            "logo": forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
        }

    def clean_patient_id_prefix(self):
        return self.cleaned_data["patient_id_prefix"].upper()

    def clean_receipt_prefix(self):
        return self.cleaned_data["receipt_prefix"].upper()


class BranchForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Branch
        fields = ["name", "code", "city", "phone", "address", "is_main"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}
        labels = {"is_main": "Main branch"}

    def __init__(self, *args, organization, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)

    def clean_code(self):
        code = self.cleaned_data["code"].upper()
        qs = Branch.objects.filter(organization=self.organization, code=code)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Another branch already uses this code.")
        return code

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Branch.objects.filter(organization=self.organization, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Another branch already has this name.")
        return name


class PreferencesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = OrganizationPreferences
        fields = [
            "feature_lab",
            "feature_pharmacy",
            "feature_ipd",
            "default_slot_minutes",
            "default_follow_up_days",
            "display_refresh_seconds",
            "display_show_doctor",
            "near_expiry_days",
            "default_reorder_level",
        ]
