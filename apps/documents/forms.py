from django import forms

from apps.core.forms import StyledFormMixin

from .models import Document
from .services import ACCEPT_ATTR


class UploadForm(StyledFormMixin, forms.Form):
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={"accept": ACCEPT_ATTR}))
    title = forms.CharField(max_length=160, required=False, help_text="Defaults to the file name.")
    category = forms.ChoiceField(choices=Document.Category.choices, initial=Document.Category.OTHER)


class ArchiveForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200)
