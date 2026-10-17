from django import forms

from apps.core.forms import StyledFormMixin

from .models import Medicine, PrescriptionItem

Item = PrescriptionItem


class PrescriptionItemForm(StyledFormMixin, forms.Form):
    """One prescription line. `medicine` is set by picking a search result; typing a name
    without picking one adds a free-text line (medicines not yet in the master)."""

    medicine = forms.IntegerField(required=False, widget=forms.HiddenInput)
    medicine_name = forms.CharField(label="Medicine", max_length=200)
    dose = forms.CharField(max_length=40, help_text="e.g. 1 tablet, 5 ml, 2 puffs")
    route = forms.ChoiceField(choices=Medicine.Route.choices, initial="oral")
    frequency = forms.ChoiceField(choices=Item.Frequency.choices, initial="BD")
    duration_value = forms.IntegerField(
        label="Duration", required=False, min_value=1, max_value=365
    )
    duration_unit = forms.ChoiceField(
        label="Unit", choices=Item.DurationUnit.choices, initial="days"
    )
    quantity = forms.IntegerField(
        required=False, min_value=1, max_value=9999, help_text="Blank = calculated for tablets."
    )
    food = forms.ChoiceField(label="Food", choices=Item.Food.choices, required=False)
    instructions = forms.CharField(max_length=200, required=False)
    override_reason = forms.CharField(
        label="Reason to prescribe despite the allergy", max_length=200, required=False
    )

    def item_data(self) -> dict:
        c = self.cleaned_data
        return {
            k: c.get(k)
            for k in ["medicine_name", "dose", "route", "frequency", "duration_value",
                      "duration_unit", "quantity", "food", "instructions"]
        }  # fmt: skip


class MedicineForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Medicine
        fields = ["brand_name", "generic_name", "strength", "form", "route", "manufacturer"]

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization

    def clean(self):
        data = super().clean()
        qs = Medicine.objects.filter(
            organization=self.organization,
            brand_name__iexact=data.get("brand_name", ""),
            strength__iexact=data.get("strength", ""),
            form=data.get("form"),
        ).exclude(pk=self.instance.pk)
        if data.get("brand_name") and qs.exists():
            raise forms.ValidationError("This medicine is already in the list.")
        return data
