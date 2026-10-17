from django import forms

from apps.core.forms import StyledFormMixin

from .models import IntakeOutput, MedicationAdministration, NursingNote, VitalsSchedule


class NursingNoteForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(choices=NursingNote.Kind.choices, label="Type")
    note = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class IntakeOutputForm(StyledFormMixin, forms.Form):
    kind = forms.ChoiceField(choices=IntakeOutput.Kind.choices, label="Intake or output")
    category = forms.ChoiceField(choices=IntakeOutput.Category.choices)
    volume_ml = forms.IntegerField(min_value=1, max_value=10000, label="Volume (ml)")
    notes = forms.CharField(max_length=200, required=False)


class VitalsScheduleForm(StyledFormMixin, forms.Form):
    interval_hours = forms.TypedChoiceField(
        choices=VitalsSchedule.INTERVALS, coerce=int, label="Observations"
    )


class DoseForm(StyledFormMixin, forms.Form):
    status = forms.ChoiceField(choices=MedicationAdministration.Status.choices)
    scheduled_for = forms.DateTimeField(required=False, widget=forms.HiddenInput)
    reason = forms.CharField(max_length=200, required=False)
    dose_given = forms.CharField(max_length=40, required=False)

    def clean_scheduled_for(self):
        return self.cleaned_data.get("scheduled_for")


class HandoverForm(StyledFormMixin, forms.Form):
    summary = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    pending_tasks = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class ReasonForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200)
