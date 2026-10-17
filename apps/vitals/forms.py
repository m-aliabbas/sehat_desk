from django import forms

from apps.core.forms import StyledFormMixin

from .models import VitalSign
from .services import fahrenheit_to_celsius


class VitalsForm(StyledFormMixin, forms.ModelForm):
    """Vitals entry component. Temperature may be typed in °F or °C (stored as °C)."""

    temperature = forms.DecimalField(
        label="Temperature", required=False, max_digits=5, decimal_places=1
    )
    temperature_unit = forms.ChoiceField(
        label="Unit", choices=[("F", "°F"), ("C", "°C")], initial="F"
    )

    class Meta:
        model = VitalSign
        fields = [
            "bp_systolic", "bp_diastolic", "pulse", "respiratory_rate", "spo2",
            "weight_kg", "height_cm", "blood_glucose", "glucose_context", "notes",
        ]  # fmt: skip
        labels = {"glucose_context": "Glucose taken", "notes": "Notes"}
        widgets = {
            f: forms.NumberInput(attrs={"inputmode": "decimal", "step": "any"})
            for f in ["bp_systolic", "bp_diastolic", "pulse", "respiratory_rate", "spo2",
                      "weight_kg", "height_cm", "blood_glucose"]
        }  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["glucose_context"].choices = VitalSign.GlucoseContext.choices
        self.fields["bp_systolic"].widget.attrs["aria-label"] = "BP systolic"
        self.fields["bp_diastolic"].widget.attrs["aria-label"] = "BP diastolic"

    def clean(self):
        data = super().clean()
        temp, unit = data.get("temperature"), data.get("temperature_unit")
        data["temperature_c"] = None
        if temp is not None:
            celsius = fahrenheit_to_celsius(temp) if unit == "F" else temp
            if not (30 <= celsius <= 45):
                self.add_error("temperature", "That temperature is not plausible. Check the unit.")
            data["temperature_c"] = celsius
        return data

    def vitals_data(self) -> dict:
        data = {f: self.cleaned_data.get(f) for f in self.Meta.fields}
        data["temperature_c"] = self.cleaned_data.get("temperature_c")
        return data


class MarkErrorForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200, label="Why is this reading wrong?")
