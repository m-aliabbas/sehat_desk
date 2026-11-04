"""The shared edit form for kinds with no edit page of their own (registry `edit_fields`)."""

from django import forms
from django.forms.utils import ErrorList

from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped


def edit_form(kind, request):
    """A ModelForm for `kind.edit_fields`. Every dropdown is limited to this hospital (and
    branch for branch data), so an edit can never point at another hospital's record."""

    class EditForm(StyledFormMixin, forms.ModelForm):
        class Meta:
            model = kind.model_class
            fields = kind.edit_fields

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for field in self.fields.values():
                queryset = getattr(field, "queryset", None)
                if queryset is not None:
                    field.queryset = scoped(queryset, request)

        def _get_validation_exclusions(self):
            # Unique names are per hospital (or branch): check them although those fields are
            # not on the form (with deleted_at, which the condition reads), so a duplicate shows
            # as an error instead of failing to save.
            return super()._get_validation_exclusions() - {"organization", "branch", "deleted_at"}

        def _post_clean(self):
            super()._post_clean()
            # A unique rule with a condition reports itself by its internal name; say it plainly.
            errors = self._errors.get(forms.forms.NON_FIELD_ERRORS)
            if errors and any("uniq_" in str(e) for e in errors):
                self._errors[forms.forms.NON_FIELD_ERRORS] = ErrorList(
                    [f"Another {kind.label.lower()} already has this name or code."]
                )

    return EditForm
