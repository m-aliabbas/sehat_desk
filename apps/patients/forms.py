from datetime import date

from django import forms

from apps.core.forms import StyledFormMixin

from .identity import is_valid_cnic, is_valid_phone, normalize_cnic, normalize_phone
from .models import Allergy, Patient, PatientIdentifier
from .services import find_duplicates

PATIENT_FIELDS = [
    "full_name",
    "gender",
    "date_of_birth",
    "phone",
    "cnic",
    "email",
    "address",
    "city",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relation",
    "blood_group",
    "allergy_status",
    "photo",
    "consent_recorded",
]
QUICK_FIELDS = {"full_name", "gender", "date_of_birth", "phone", "cnic"}


class PatientForm(StyledFormMixin, forms.ModelForm):
    age_years = forms.IntegerField(
        label="Age (years)",
        required=False,
        min_value=0,
        max_value=130,
        help_text="Use when the date of birth is not known.",
    )
    guardian_cnic = forms.CharField(
        label="Guardian CNIC",
        required=False,
        help_text="For children without their own CNIC.",
    )
    not_duplicate = forms.BooleanField(
        required=False, label="I checked the matches above. This is a different person."
    )

    class Meta:
        model = Patient
        fields = PATIENT_FIELDS
        labels = {
            "phone": "Mobile number",
            "cnic": "CNIC",
            "consent_recorded": "Patient consent for contact and record use recorded",
            "allergy_status": "Allergies",
        }
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "phone": forms.TextInput(attrs={"inputmode": "tel", "placeholder": "0300 1234567"}),
            "cnic": forms.TextInput(
                attrs={"inputmode": "numeric", "placeholder": "12345-1234567-1"}
            ),
            "emergency_contact_phone": forms.TextInput(attrs={"inputmode": "tel"}),
            "photo": forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
        }

    def __init__(self, *args, organization, quick=False, **kwargs):
        self.organization = organization
        self.quick = quick
        self.duplicates = []
        super().__init__(*args, **kwargs)
        self.fields["phone"].required = True
        if "cnic" in self.fields:
            # Accept "12345-1234567-1" as typed; clean_cnic stores the 13 digits.
            self.fields["cnic"].validators = []
            self.fields["cnic"].widget.attrs["maxlength"] = "15"
        # Both have a meaningful "not recorded yet" default, so reception may leave them.
        for name in ("gender", "allergy_status"):
            if name in self.fields:
                self.fields[name].required = False
        if quick:
            for name in list(self.fields):
                if name not in QUICK_FIELDS | {"age_years", "not_duplicate"}:
                    del self.fields[name]
        if self.instance.pk:
            del self.fields["guardian_cnic"]
            if self.instance.cnic:
                self.initial["cnic"] = self.instance.cnic_display

    def clean_gender(self):
        return self.cleaned_data.get("gender") or Patient.Gender.UNKNOWN

    def clean_allergy_status(self):
        return self.cleaned_data.get("allergy_status") or Patient.AllergyStatus.UNKNOWN

    def clean_phone(self):
        value = self.cleaned_data["phone"]
        if value and not is_valid_phone(value):
            raise forms.ValidationError("Enter a valid phone number, e.g. 0300 1234567.")
        return normalize_phone(value)

    def clean_emergency_contact_phone(self):
        value = self.cleaned_data.get("emergency_contact_phone", "")
        if value and not is_valid_phone(value):
            raise forms.ValidationError("Enter a valid phone number.")
        return normalize_phone(value)

    def clean_cnic(self):
        value = self.cleaned_data.get("cnic", "")
        if value and not is_valid_cnic(value):
            raise forms.ValidationError("A CNIC has 13 digits, e.g. 12345-1234567-1.")
        return normalize_cnic(value)

    def clean_guardian_cnic(self):
        value = self.cleaned_data.get("guardian_cnic", "")
        if value and not is_valid_cnic(value):
            raise forms.ValidationError("A CNIC has 13 digits.")
        return normalize_cnic(value)

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get("date_of_birth")
        if dob and dob > date.today():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        return dob

    def clean(self):
        c = super().clean()
        dob, age = c.get("date_of_birth"), c.get("age_years")
        if not dob and age is not None:
            today = date.today()
            c["date_of_birth"] = date(today.year - age, 1, 1)
            self.instance.dob_is_estimated = True
        elif dob:
            self.instance.dob_is_estimated = False
        if not self.errors:
            self.duplicates = find_duplicates(
                self.organization,
                full_name=c.get("full_name", ""),
                phone=c.get("phone", ""),
                cnic=c.get("cnic", ""),
                date_of_birth=dob,
                exclude_pk=self.instance.pk,
            )
            strong = [m for m in self.duplicates if m.strong]
            if strong and not c.get("not_duplicate"):
                raise forms.ValidationError(
                    "This looks like an existing patient. Open the matching record, or confirm "
                    "below that this is a different person."
                )
        return c

    def model_data(self) -> dict:
        """Cleaned values that map to Patient fields (for services.register_patient)."""
        data = {k: v for k, v in self.cleaned_data.items() if k in PATIENT_FIELDS}
        data["dob_is_estimated"] = self.instance.dob_is_estimated
        return data


class IdentifierForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PatientIdentifier
        fields = ["kind", "value"]


class AllergyForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Allergy
        fields = ["substance", "reaction", "severity", "source"]


class MarkErrorForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200, label="Why is this entry wrong?")
