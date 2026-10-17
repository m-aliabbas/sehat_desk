from django import forms

from apps.accounts.models import User
from apps.core.forms import StyledFormMixin

from .models import (
    WEEKDAYS,
    Department,
    DoctorLeave,
    DoctorProfile,
    Service,
    ServiceCategory,
    Specialty,
)


class OrgUniqueMixin:
    """Case-insensitive uniqueness of `unique_fields` within the organization."""

    unique_fields: tuple[str, ...] = ()

    def __init__(self, *args, organization, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        model = self._meta.model
        for field in self.unique_fields:
            value = cleaned.get(field)
            if not value:
                continue
            qs = model.objects.filter(organization=self.organization, **{f"{field}__iexact": value})
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error(
                    field, f"Another {model._meta.verbose_name} already uses this {field}."
                )
        return cleaned


class DepartmentForm(StyledFormMixin, OrgUniqueMixin, forms.ModelForm):
    unique_fields = ("name", "code")

    class Meta:
        model = Department
        fields = ["name", "code", "token_prefix", "description"]

    def clean_code(self):
        return self.cleaned_data["code"].upper()

    def clean_token_prefix(self):
        return self.cleaned_data.get("token_prefix", "").upper()


class SpecialtyForm(StyledFormMixin, OrgUniqueMixin, forms.ModelForm):
    unique_fields = ("name",)

    class Meta:
        model = Specialty
        fields = ["name"]


class DoctorProfileForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DoctorProfile
        fields = [
            "user",
            "department",
            "specialty",
            "registration_number",
            "qualifications",
            "consultation_fee",
            "follow_up_fee",
            "follow_up_days",
            "slot_minutes",
            "allow_overbooking",
        ]
        labels = {"user": "Staff account", "follow_up_days": "Follow-up window (days)"}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(
            organization=organization, is_active=True
        )
        self.fields["specialty"].queryset = Specialty.objects.filter(organization=organization)
        if self.instance.pk:
            del self.fields["user"]  # the linked account never changes
        else:
            self.fields["user"].queryset = User.objects.filter(
                organization=organization, is_active=True, doctor_profile__isnull=True
            )
            self.fields[
                "user"
            ].help_text = "Create the staff account first (Staff → Add staff, role Doctor)."


class ScheduleDayForm(StyledFormMixin, forms.Form):
    """One weekday row of the weekly schedule grid."""

    weekday = forms.TypedChoiceField(choices=WEEKDAYS, coerce=int, widget=forms.HiddenInput)
    enabled = forms.BooleanField(required=False, label="Works")
    start_time = forms.TimeField(
        required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    end_time = forms.TimeField(
        required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    break_start = forms.TimeField(
        required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    break_end = forms.TimeField(
        required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    room = forms.CharField(required=False, max_length=40)

    def clean(self):
        c = super().clean()
        if not c.get("enabled"):
            return c
        start, end = c.get("start_time"), c.get("end_time")
        if not start or not end:
            raise forms.ValidationError("Enter start and end times for working days.")
        if start >= end:
            raise forms.ValidationError("End time must be after start time.")
        bs, be = c.get("break_start"), c.get("break_end")
        if bool(bs) != bool(be):
            raise forms.ValidationError("Give both break times, or neither.")
        if bs and not (start <= bs < be <= end):
            raise forms.ValidationError("The break must fall inside clinic hours.")
        return c


ScheduleFormSet = forms.formset_factory(ScheduleDayForm, extra=0, max_num=7, absolute_max=7)


class DoctorLeaveForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DoctorLeave
        fields = ["start_date", "end_date", "reason"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def clean(self):
        c = super().clean()
        if c.get("start_date") and c.get("end_date") and c["end_date"] < c["start_date"]:
            raise forms.ValidationError("Leave must end on or after its start date.")
        return c


class ServiceCategoryForm(StyledFormMixin, OrgUniqueMixin, forms.ModelForm):
    unique_fields = ("name",)

    class Meta:
        model = ServiceCategory
        fields = ["name"]


class ServiceForm(StyledFormMixin, OrgUniqueMixin, forms.ModelForm):
    unique_fields = ("code",)

    class Meta:
        model = Service
        fields = ["category", "department", "code", "name", "price"]

    def __init__(self, *args, organization, branches, **kwargs):
        super().__init__(*args, organization=organization, **kwargs)
        self.fields["category"].queryset = ServiceCategory.objects.filter(organization=organization)
        self.fields["department"].queryset = Department.objects.filter(organization=organization)
        self.fields["department"].required = False
        self.branches = list(branches)
        existing = (
            {p.branch_id: p.price for p in self.instance.branch_prices.all()}
            if self.instance.pk
            else {}
        )
        for b in self.branches:
            self.fields[f"branch_price_{b.pk}"] = forms.DecimalField(
                label=f"{b.name} price",
                required=False,
                min_value=0,
                max_digits=12,
                decimal_places=2,
                initial=existing.get(b.pk),
                help_text="Leave empty to use the default price.",
                widget=forms.NumberInput(attrs={"class": "input", "step": "0.01"}),
            )

    def clean_code(self):
        return self.cleaned_data["code"].upper()

    def branch_price_fields(self):
        return [self[f"branch_price_{b.pk}"] for b in self.branches]
