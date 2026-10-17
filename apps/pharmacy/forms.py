from django import forms
from django.utils import timezone

from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped
from apps.prescriptions.models import Medicine

from .models import Product, ProductCategory, StockTransaction, Vendor


class ProductForm(StyledFormMixin, forms.ModelForm):
    create_medicine = forms.BooleanField(
        required=False,
        label="Also add to the medicine list (so doctors can prescribe it)",
    )

    class Meta:
        model = Product
        fields = [
            "brand_name", "generic_name", "strength", "medicine", "manufacturer", "category",
            "sku", "barcode", "unit", "pack_size", "pack_unit", "allow_loose_sale",
            "purchase_price", "sale_price", "tax_percent", "reorder_level", "requires_prescription",
        ]  # fmt: skip
        labels = {"sku": "SKU (blank = automatic)", "medicine": "Medicine master entry"}

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = request.organization
        self.fields["sku"].required = False
        self.fields["pack_unit"].required = False
        self.fields["category"].queryset = scoped(ProductCategory, request)
        self.fields["medicine"].queryset = scoped(Medicine, request).filter(is_active=True)

    def _unique(self, field, value, message):
        if (
            value
            and Product.objects.filter(organization=self.organization, **{field: value})
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(message)
        return value

    def clean_sku(self):
        return self._unique(
            "sku", self.cleaned_data["sku"].strip().upper(), "Another product uses this SKU."
        )

    def clean_pack_unit(self):
        unit = self.cleaned_data.get("unit", "")
        return self.cleaned_data["pack_unit"] or Product.PACK_FOR_UNIT.get(unit, "strip")

    def clean_barcode(self):
        return self._unique(
            "barcode", self.cleaned_data["barcode"].strip(), "Another product has this barcode."
        )

    def clean(self):
        data = super().clean()
        if data.get("create_medicine") and data.get("medicine"):
            self.add_error("create_medicine", "Already linked to a medicine.")
        if data.get("create_medicine") and not data.get("generic_name"):
            self.add_error("generic_name", "Needed to add it to the medicine list.")
        return data


class ReceiveForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    batch_number = forms.CharField(max_length=40)
    expiry_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    packs = forms.IntegerField(label="Packs received", min_value=0, required=False, initial=0)
    loose_units = forms.IntegerField(
        label="Extra loose units", min_value=0, required=False, initial=0
    )
    purchase_cost = forms.DecimalField(
        label="Cost per unit", min_value=0, max_digits=12, decimal_places=2
    )
    sale_price = forms.DecimalField(
        label="Sale price per unit", min_value=0, max_digits=12, decimal_places=2, required=False,
        help_text="Blank = the product's sale price.",
    )  # fmt: skip
    reference = forms.CharField(label="Supplier invoice / reference", max_length=80, required=False)
    opening = forms.BooleanField(label="Opening stock (not a purchase)", required=False)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = scoped(Product, request).filter(is_active=True)

    def clean_expiry_date(self):
        d = self.cleaned_data["expiry_date"]
        if d <= timezone.localdate():
            raise forms.ValidationError("This stock is expired. Do not receive it.")
        return d

    def clean(self):
        data = super().clean()
        product = data.get("product")
        if product:
            data["quantity"] = (data.get("packs") or 0) * product.pack_size + (
                data.get("loose_units") or 0
            )
            if data["quantity"] <= 0:
                raise forms.ValidationError("Enter the packs or units received.")
        return data


class BatchActionForm(StyledFormMixin, forms.Form):
    ACTIONS = [
        ("count", "Physical count (set to counted quantity)"),
        (StockTransaction.Kind.EXPIRED, "Write off as expired"),
        (StockTransaction.Kind.DAMAGED, "Write off as damaged"),
        (StockTransaction.Kind.PURCHASE_RETURN, "Return to supplier"),
    ]
    action = forms.ChoiceField(choices=ACTIONS)
    quantity = forms.IntegerField(
        min_value=0, help_text="Counted quantity for a count; units removed otherwise."
    )
    reason = forms.CharField(max_length=200)
    reference = forms.CharField(max_length=80, required=False, label="Reference (optional)")


# --- Purchasing --------------------------------------------------------------------------------


class VendorForm(StyledFormMixin, forms.ModelForm):
    opening_balance = forms.DecimalField(
        label="Opening balance (amount already owed)", max_digits=12, decimal_places=2,
        required=False, help_text="Only when adding the supplier. Leave blank if nothing is owed.",
    )  # fmt: skip

    class Meta:
        model = Vendor
        fields = [
            "name", "contact_person", "phone", "email", "address", "tax_number",
            "payment_terms_days", "notes",
        ]  # fmt: skip

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        if self.instance.pk:
            del self.fields["opening_balance"]

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if (
            Vendor.objects.filter(organization=self.organization, name__iexact=name)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("A supplier with this name already exists.")
        return name


class PurchaseCreateForm(StyledFormMixin, forms.Form):
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.none(), label="Supplier")
    supplier_invoice_number = forms.CharField(
        label="Supplier invoice number", max_length=60, required=False,
        help_text="Can be added later, before the goods are received.",
    )  # fmt: skip
    notes = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = scoped(Vendor, request).filter(is_active=True)


class PurchaseItemForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    packs = forms.IntegerField(label="Packs", min_value=0, required=False, initial=0)
    loose_units = forms.IntegerField(label="Loose units", min_value=0, required=False, initial=0)
    free_quantity = forms.IntegerField(label="Free / bonus units", min_value=0, initial=0,
                                       required=False)  # fmt: skip
    unit_cost = forms.DecimalField(label="Cost per unit", min_value=0, max_digits=12,
                                   decimal_places=2)  # fmt: skip
    batch_number = forms.CharField(max_length=40, required=False)
    expiry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    sale_price = forms.DecimalField(
        label="Sale price per unit", min_value=0, max_digits=12, decimal_places=2, required=False,
        help_text="Blank = the product's sale price.",
    )  # fmt: skip

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = scoped(Product, request).filter(is_active=True)

    def clean(self):
        data = super().clean()
        product = data.get("product")
        if product:
            data["quantity"] = (data.get("packs") or 0) * product.pack_size + (
                data.get("loose_units") or 0
            )
            if data["quantity"] <= 0:
                raise forms.ValidationError("Enter the packs or units bought.")
        return data


class PurchaseDetailsForm(StyledFormMixin, forms.Form):
    supplier_invoice_number = forms.CharField(label="Supplier invoice number", max_length=60,
                                              required=False)  # fmt: skip
    supplier_invoice_date = forms.DateField(
        label="Supplier invoice date", required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )  # fmt: skip
    discount = forms.DecimalField(label="Discount on invoice", min_value=0, max_digits=12,
                                  decimal_places=2, required=False)  # fmt: skip
    tax_amount = forms.DecimalField(label="Tax on invoice", min_value=0, max_digits=12,
                                    decimal_places=2, required=False)  # fmt: skip
    notes = forms.CharField(max_length=300, required=False)
