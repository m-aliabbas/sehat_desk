from decimal import Decimal

from django import forms
from django.core.validators import MaxLengthValidator
from django.urls import reverse
from django.utils import timezone

from apps.core.forms import StyledFormMixin
from apps.core.scoping import scoped
from apps.organizations import field_rules
from apps.prescriptions.models import Medicine

from . import scan
from .models import Product, ProductCategory, StockTransaction, Vendor
from .packing import packing_text, parse_packing

LEVELS = [("box", "box"), ("pack", "pack"), ("unit", "unit")]


class ProductForm(StyledFormMixin, forms.ModelForm):
    # The price someone typed, at whichever size they typed it: cost_per / price_per say which.
    remember = {
        "strength": "pharmacy.Product.strength",
        "manufacturer": "pharmacy.Product.manufacturer",
    }

    pack_cost = forms.DecimalField(label="Buy price", min_value=0, max_digits=12,
                                   decimal_places=2, required=False)  # fmt: skip
    pack_price = forms.DecimalField(label="Sell price", min_value=0, max_digits=12,
                                    decimal_places=2, required=False)  # fmt: skip
    cost_per = forms.ChoiceField(choices=LEVELS, required=False)
    price_per = forms.ChoiceField(choices=LEVELS, required=False)
    apply_to_shelf = forms.BooleanField(
        label="Also change the price of stock already on the shelf", required=False
    )
    price_reason = forms.CharField(label="Why the price changed", max_length=200, required=False)
    reorder_packs = forms.IntegerField(
        label="Reorder when this many packs are left", min_value=0, required=False
    )
    # A whole phrase is still accepted from imports and the add-many code; screens post the sizes.
    packing = forms.CharField(required=False, max_length=80)
    create_medicine = forms.BooleanField(
        required=False,
        label="Also add to the medicine list (so doctors can prescribe it)",
    )

    class Meta:
        model = Product
        fields = [
            "brand_name", "generic_name", "strength", "medicine", "manufacturer", "category",
            "sku", "barcode", "unit", "pack_size", "pack_unit", "packs_per_box", "box_unit",
            "allow_loose_sale",
            "preferred_vendor",
            "purchase_price", "sale_price", "tax_percent", "reorder_level", "expiry_alert_days",
            "requires_prescription",
        ]  # fmt: skip
        labels = {
            "sku": "SKU (blank = automatic)",
            "barcode": "Barcode (scan it, or blank = automatic)",
            "medicine": "Medicine master entry",
        }

    def __init__(self, *args, request, apply_rules=True, **kwargs):
        self.full_edit = "records.edit_full" in getattr(request, "sd_perms", ())
        super().__init__(*args, **kwargs)
        self.organization = request.organization
        if apply_rules:
            for key, name in (
                ("product.generic", "generic_name"),
                ("product.category", "category"),
            ):
                self.fields[name].required = field_rules.is_required(self.organization, key)
        self.fields["sku"].required = False
        self.fields["pack_unit"].required = False
        self.fields["category"].queryset = scoped(ProductCategory, request)
        self.fields["medicine"].queryset = scoped(Medicine, request).filter(is_active=True)
        self.fields["preferred_vendor"].queryset = scoped(Vendor, request).filter(is_active=True)
        self.fields["packs_per_box"].required = False
        if self.instance.pk:
            self.initial["packing"] = packing_text(self.instance)
        # A new product starts as a strip of 10, so its default minimum converts at that size.
        size = self.instance.pack_size if self.instance.pk else 10
        self.initial["reorder_packs"] = (self.instance.reorder_level or 0) // size
        if "pack_price" in self.data:
            for name in ("purchase_price", "sale_price", "reorder_level"):
                self.fields[name].required = False
        if self.data.get("packing"):
            self.fields["unit"].required = False
        self.fields["pack_size"].required = False
        self.fields["box_unit"].required = False
        # Free text with suggestions, so any unit or pack name works.
        self.fields["unit"].widget.attrs["list"] = "unit-names"
        self.fields["pack_unit"].widget.attrs["list"] = "pack-names"
        self.fields["box_unit"].widget.attrs["list"] = "pack-names"
        # A scanner ends with Enter; that must not submit the form half filled.
        barcode = self.fields["barcode"]
        barcode.widget.attrs["@keydown.enter.prevent"] = ""
        # A QR code is far longer than the code kept from it, and maxlength would cut it off.
        barcode.widget.attrs.pop("maxlength", None)
        barcode.validators = [
            v for v in barcode.validators if not isinstance(v, MaxLengthValidator)
        ]
        scan_url = reverse("pharmacy:scan-read")
        barcode.widget.attrs.update(
            {
                "data-scan": "fill",
                "data-scan-url": scan_url,
                "data-scan-self": self.instance.pk or "",
            }
        )
        self.fields["brand_name"].widget.attrs.update(
            {"data-scan": "detect", "data-scan-url": scan_url}
        )
        if self.instance.pk:
            return
        self.fields["category"].widget.attrs["@change"] = "applyCategory($event.target.value)"
        self.fields["tax_percent"].widget.attrs["x-ref"] = "tax"
        self.fields["requires_prescription"].widget.attrs["x-ref"] = "rx"

    def clean_unit(self):
        return self.cleaned_data["unit"].strip().lower()

    def clean_box_unit(self):
        return (self.cleaned_data.get("box_unit") or "box").strip().lower()

    def clean_packs_per_box(self):
        return self.cleaned_data.get("packs_per_box") or 1

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
        name = self.cleaned_data["pack_unit"].strip().lower()
        return name or Product.PACK_FOR_UNIT.get(unit, "strip")

    def clean_barcode(self):
        code = scan.parse(self.cleaned_data["barcode"]).code
        if len(code) > Product._meta.get_field("barcode").max_length:
            raise forms.ValidationError("This code is too long to keep as a barcode.")
        same = Product.objects.filter(
            organization=self.organization, barcode__in=scan.code_variants(code)
        )
        if same.exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another product has this barcode.")
        return code

    def clean(self):
        data = super().clean()
        if data.get("packing"):
            try:
                data.update(parse_packing(data["packing"]))
            except ValueError as err:
                self.add_error("packing", str(err))
            for name in ("unit", "pack_size", "pack_unit", "packs_per_box", "box_unit"):
                self.errors.pop(name, None)
        self._normalise_sizes(data)
        if (
            self.instance.pk
            and "unit" in self.changed_data
            and not self.full_edit
            and self._has_moved_stock()
        ):
            self.add_error(
                "unit",
                "Stock is already counted in this unit, so it cannot change. To rename the packs "
                "or change how many they hold, edit those instead. If it was saved in the wrong "
                "unit, use Correct unit on the product page.",
            )
        if "pack_price" in self.data:
            size = data["pack_size"]
            levels = {"box": size * data["packs_per_box"], "pack": size, "unit": 1}
            data["purchase_price"] = self._unit_price(
                data.get("pack_cost"), levels[data.get("cost_per") or "pack"], "purchase_price",
                levels,
            )  # fmt: skip
            data["sale_price"] = self._unit_price(
                data.get("pack_price"), levels[data.get("price_per") or "pack"], "sale_price",
                levels,
            )  # fmt: skip
            data["reorder_level"] = (data.get("reorder_packs") or 0) * size
        if data.get("create_medicine") and data.get("medicine"):
            self.add_error("create_medicine", "Already linked to a medicine.")
        if data.get("create_medicine") and not data.get("generic_name"):
            self.add_error("generic_name", "Needed to add it to the medicine list.")
        return data

    def _normalise_sizes(self, data):
        """Blank sizes mean 1. A bigger pack with no pack under it is simply the pack."""
        data["pack_size"] = data.get("pack_size") or 1
        data["packs_per_box"] = data.get("packs_per_box") or 1
        if data["pack_size"] == 1 and data["packs_per_box"] > 1:
            data["pack_size"], data["packs_per_box"] = data["packs_per_box"], 1
            data["pack_unit"] = data.get("box_unit") or "box"
        if data["pack_size"] == 1 and data.get("unit"):
            data["pack_unit"] = data["unit"]

    def _has_moved_stock(self) -> bool:
        return StockTransaction.objects.filter(product=self.instance).exists()

    def _unit_price(self, typed, units, field, levels) -> Decimal:
        """Per base unit, from what was typed. Unchanged on screen means unchanged: re-dividing a
        rounded figure must not look like a price change."""
        typed = Decimal(typed or 0)
        stored = getattr(self.instance, field) if self.instance.pk else None
        same_sizes = (
            self.instance.pk
            and levels["pack"] == self.initial.get("pack_size")
            and (
                levels["box"]
                == self.initial.get("pack_size", 0) * self.initial.get("packs_per_box", 0)
            )
        )
        if same_sizes and (stored * units).quantize(Decimal("0.01")) == typed.quantize(
            Decimal("0.01")
        ):
            return stored
        return (typed / units).quantize(Decimal("0.000001"))

    def packing_state(self) -> dict:
        """What the packing and price table opens with."""
        if self.is_bound:
            get = self.data.get
            return {
                "unit": get("unit", ""), "pack_unit": get("pack_unit", ""),
                "pack_size": get("pack_size", ""), "box_unit": get("box_unit", ""),
                "packs_per_box": get("packs_per_box", ""),
                "cost": get("pack_cost", ""), "price": get("pack_price", ""),
                "cost_per": get("cost_per", "pack"), "price_per": get("price_per", "pack"),
            }  # fmt: skip
        p = self.instance
        if not p.pk:
            return {
                "unit": "tablet", "pack_unit": "strip", "pack_size": 10, "box_unit": "box",
                "packs_per_box": 1, "cost": "", "price": "", "cost_per": "pack",
                "price_per": "pack",
            }  # fmt: skip
        size = p.units_in(p.top_level)
        return {
            "unit": p.unit, "pack_unit": p.pack_unit, "pack_size": p.pack_size,
            "box_unit": p.box_unit, "packs_per_box": p.packs_per_box,
            "cost": str((p.purchase_price * size).quantize(Decimal("0.01"))),
            "price": str((p.sale_price * size).quantize(Decimal("0.01"))),
            "cost_per": p.top_level, "price_per": p.top_level,
        }  # fmt: skip


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
        ("details", "Correct batch number or expiry"),
    ]
    action = forms.ChoiceField(choices=ACTIONS)
    quantity = forms.IntegerField(
        min_value=0,
        required=False,
        help_text="Counted quantity for a count; units removed otherwise.",
    )
    new_batch_number = forms.CharField(max_length=40, required=False)
    new_expiry_date = forms.DateField(required=False)
    reason = forms.CharField(max_length=200)
    reference = forms.CharField(max_length=80, required=False, label="Reference (optional)")

    def clean(self):
        data = super().clean()
        if data.get("action") != "details" and data.get("quantity") is None:
            self.add_error("quantity", "Enter the quantity.")
        return data


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
        label="Their invoice no.",
        max_length=60,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    supplier_invoice_date = forms.DateField(
        label="Their invoice date",
        required=False,
        initial=timezone.localdate,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    tax_amount = forms.DecimalField(label="GST on bill", min_value=0, max_digits=12,
                                    decimal_places=2, required=False)  # fmt: skip
    further_tax = forms.DecimalField(label="Further tax", min_value=0, max_digits=12,
                                     decimal_places=2, required=False)  # fmt: skip
    advance_tax = forms.DecimalField(label="Advance tax", min_value=0, max_digits=12,
                                     decimal_places=2, required=False)  # fmt: skip
    discount = forms.DecimalField(label="Discount", min_value=0, max_digits=12,
                                  decimal_places=2, required=False)  # fmt: skip
    notes = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = scoped(Vendor, request).filter(is_active=True)


class PurchaseItemForm(StyledFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    packs = forms.IntegerField(label="Packs", min_value=0, required=False, initial=0)
    loose_units = forms.IntegerField(label="Loose units", min_value=0, required=False, initial=0)
    free_quantity = forms.IntegerField(label="Bonus (free) units", min_value=0, initial=0,
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
    tax_amount = forms.DecimalField(label="GST on invoice", min_value=0, max_digits=12,
                                    decimal_places=2, required=False)  # fmt: skip
    further_tax = forms.DecimalField(label="Further tax", min_value=0, max_digits=12,
                                     decimal_places=2, required=False)  # fmt: skip
    advance_tax = forms.DecimalField(label="Advance tax", min_value=0, max_digits=12,
                                     decimal_places=2, required=False)  # fmt: skip
    notes = forms.CharField(max_length=300, required=False)
