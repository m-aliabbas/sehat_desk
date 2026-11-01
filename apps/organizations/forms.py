from django import forms

from apps.core.forms import StyledFormMixin

from .models import Branch, Organization, OrganizationPreferences, PrintLayout


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
    remember = {"city": "organizations.Branch.city"}

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


class ModulesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = OrganizationPreferences
        fields = ["feature_lab", "feature_pharmacy", "feature_ipd", "feature_nursing"]


class PreferencesForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = OrganizationPreferences
        fields = [
            "default_slot_minutes",
            "default_follow_up_days",
            "use_doctor_shifts",
            "opd_collect_fee_upfront",
            "opd_print_blank_prescription",
            "opd_print_office_copy",
            "opd_auto_print",
            "display_refresh_seconds",
            "display_show_doctor",
            "pharmacy_require_prescription",
            "pharmacy_auto_print_receipt",
            "near_expiry_days",
            "default_reorder_level",
        ]


class PrintLayoutForm(StyledFormMixin, forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.document != PrintLayout.Document.PRESCRIPTION:
            for name in ("header_image", "footer_image", "image_keep_clinic_text"):
                del self.fields[name]
        self.fields["paper"].choices = [
            ("", "Standard for this document"),
            *PrintLayout.Paper.choices,
        ]

    class Meta:
        model = PrintLayout
        fields = [
            "paper", "margin_mm", "top_space_mm", "bottom_space_mm", "text_size", "show_header",
            "header_align", "show_logo", "logo_mm", "header_text", "show_footer", "footer_text",
            "header_image", "footer_image", "image_keep_clinic_text",
            "doctor_side", "show_qualifications", "show_registration", "show_vitals",
            "show_allergies", "extra_lines",
        ]  # fmt: skip
        widgets = {
            "header_text": forms.Textarea(attrs={"rows": 2}),
            "footer_text": forms.Textarea(attrs={"rows": 2}),
            "extra_lines": forms.Textarea(attrs={"rows": 3}),
        }
