from django import forms
from django.utils import timezone

from apps.billing.services import active_methods
from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped

from . import segments
from .models import ExpenseCategory


class ExpenseForm(StyledFormMixin, forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    category = forms.ModelChoiceField(queryset=ExpenseCategory.objects.none())
    segment = forms.ChoiceField(
        choices=[("", "Category default")] + segments.SEGMENTS,
        required=False,
        help_text="Which part of the hospital this cost belongs to in the P&L.",
    )
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    method = forms.ModelChoiceField(queryset=None, label="Paid by", empty_label=None)
    payee = forms.CharField(max_length=120, required=False, label="Paid to")
    reference = forms.CharField(max_length=80, required=False, label="Bill / receipt no.")
    note = forms.CharField(max_length=200, required=False)
    idempotency_key = forms.CharField(max_length=40, widget=forms.HiddenInput)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = scoped(ExpenseCategory, request).filter(is_active=True)
        self.fields["method"].queryset = active_methods(request.organization)
        self.fields["date"].initial = timezone.localdate()


class CategoryForm(StyledFormMixin, forms.Form):
    name = forms.CharField(max_length=80)
    segment = forms.ChoiceField(choices=segments.SEGMENTS)


class VoidForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(max_length=200)


class SupplierPaymentForm(StyledFormMixin, forms.Form):
    vendor = forms.ModelChoiceField(queryset=None, label="Supplier")
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    method = forms.ModelChoiceField(queryset=None, label="Paid by", empty_label=None)
    reference = forms.CharField(max_length=80, required=False)
    note = forms.CharField(max_length=200, required=False)
    idempotency_key = forms.CharField(max_length=40, widget=forms.HiddenInput)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.pharmacy.models import Vendor

        self.fields["vendor"].queryset = scoped(Vendor, request).filter(is_active=True)
        self.fields["method"].queryset = active_methods(request.organization)
