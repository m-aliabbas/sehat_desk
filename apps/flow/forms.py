from django import forms

from apps.core.forms import StyledFormMixin

from .models import HospitalFlowSetting


class FlowSettingsForm(StyledFormMixin, forms.ModelForm):
    reception_prints_blank_rx = forms.BooleanField(
        label="Reception prints an empty prescription",
        required=False,
        help_text="Printed with the token for doctors who write by hand. Same switch as on "
        "Preferences.",
    )

    class Meta:
        model = HospitalFlowSetting
        fields = [
            "consultation_fee_billed_at",
            "doctor_must_click_next",
            "doctor_generates_digital_rx",
            "rx_print_at",
            "pharmacy_handles_manual_rx",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reception_prints_blank_rx"].initial = self.instance.reception_prints_blank_rx
