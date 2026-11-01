from django import forms


def remember_id(source: str) -> str:
    return "past-" + source.replace(".", "-").lower()


class StyledFormMixin:
    """Applies design-system classes to widgets and marks invalid fields for screen readers.

    Render a field with {% include "components/field.html" with field=form.name %}.
    """

    # {form field: "app.Model.field"}: a typed-in category (city, floor, payee...). What this
    # hospital typed before comes back as suggestions; field.html prints the list.
    remember: dict[str, str] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, source in self.remember.items():
            if name in self.fields:
                self.fields[name].remember = source
                self.fields[name].widget.attrs.update(
                    {"data-list": remember_id(source), "role": "combobox", "autocomplete": "off"}
                )
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                css = "checkbox"
            elif isinstance(widget, forms.Select):
                css = "select"
            elif isinstance(widget, forms.ClearableFileInput):
                css = "block text-body file:btn file:btn-secondary file:mr-3"
            else:
                css = "input"
            widget.attrs.setdefault("class", css)

    def full_clean(self):
        # Mark invalid fields after validation. Reading self.errors in __init__ would validate
        # before subclasses finish setting up (e.g. limiting a ModelChoiceField's queryset).
        super().full_clean()
        for name in self.errors:
            if name in self.fields:
                attrs = self.fields[name].widget.attrs
                attrs["aria-invalid"] = "true"
                attrs["aria-describedby"] = f"{self.add_prefix(name)}-error"
