"""Adding products: the save every entry path shares, standard categories and the many-at-once
grid. Stock is not touched here; quantities arrive through purchases."""

import re

from django.db import transaction
from django.db.models import Count

from apps.core.models import next_number
from apps.prescriptions.models import Medicine

from . import pricing, scan
from .barcodes import new_barcode
from .forms import ProductForm
from .models import Product, ProductCategory, UnitOfMeasure
from .packing import PRESETS, packing_text, parse_packing

# Common pharmacy groups: (name, prescription needed, note shown to whoever sets the tax).
# Rates are left at 0 on purpose. Printed retail prices often already include tax, and adding it
# again would overcharge patients. The hospital's accountant sets what applies to them.
_REGISTERED = "Registered drugs: reduced rate at manufacture or import."
STANDARD_CATEGORIES = [
    ("Prescription medicines", True, _REGISTERED),
    (
        "Over-the-counter medicines",
        False,
        _REGISTERED,
    ),
    ("Controlled and narcotic medicines", True, "Keep a register as your licence requires."),
    ("Antibiotics", True, _REGISTERED),
    ("Injections and IV fluids", True, _REGISTERED),
    ("Vaccines and cold chain", True, "Store cold. Check the rate with your accountant."),
    ("Syrups and suspensions", False, _REGISTERED),
    (
        "Creams, drops and inhalers",
        False,
        _REGISTERED,
    ),
    ("Herbal and homeopathic", False, "Reported at the standard rate since 2024."),
    ("Vitamins and nutraceuticals", False, "Check the rate with your accountant."),
    ("Surgical and disposables", False, "Check the rate with your accountant."),
    ("Medical devices", False, "Check the rate with your accountant."),
    ("Baby care", False, "Check the rate with your accountant."),
    ("Cosmetics and personal care", False, "Usually taxed on the printed retail price."),
]


def packing_suggestions(organization) -> list[str]:
    """What staff are offered: this hospital's UoM list and whatever its products already use, most
    used first, then the common ones."""
    used = (
        Product.objects.filter(organization=organization)
        .values("unit", "pack_unit", "pack_size", "packs_per_box", "box_unit")
        .annotate(n=Count("id"))
        .order_by("-n")[:40]
    )
    phrases = [
        *UnitOfMeasure.objects.filter(organization=organization).values_list("name", flat=True),
        *(packing_text(Product(**{k: v for k, v in row.items() if k != "n"})) for row in used),
        *PRESETS,
    ]
    return list(dict.fromkeys(phrases))


def packing_presets(organization) -> list[dict]:
    """The same suggestions as one-click buttons on the packing table, sizes filled in."""
    presets = []
    for phrase in packing_suggestions(organization):
        try:
            presets.append({"label": phrase, **parse_packing(phrase)})
        except ValueError:
            continue
    return presets[:8]


def ensure_standard_categories(organization) -> int:
    """Adds the groups a hospital does not have yet. Existing categories are left alone."""
    created = 0
    for name, rx, note in STANDARD_CATEGORIES:
        _, was_new = ProductCategory.objects.get_or_create(
            organization=organization,
            name=name,
            defaults={"requires_prescription": rx, "tax_note": note},
        )
        created += was_new
    return created


def save_product(form, *, request) -> Product:
    """Fill in what was left blank (SKU, barcode, medicine link) and save."""
    stored = Product.objects.filter(pk=form.instance.pk).first() if form.instance.pk else None
    product = form.save(commit=False)
    org = request.organization
    if not product.sku:
        product.sku = f"P-{next_number(org, 'product'):05d}"
    if not product.barcode:
        product.barcode = new_barcode(org)
    if form.cleaned_data.get("create_medicine"):
        form_choice = product.unit if product.unit in Medicine.Form.values else Medicine.Form.OTHER
        product.medicine, _ = Medicine.objects.get_or_create(
            organization=org,
            brand_name=product.brand_name,
            strength=product.strength,
            form=form_choice,
            defaults={"generic_name": product.generic_name, "manufacturer": product.manufacturer},
        )
    product.save()
    if stored:
        pricing.record_edit(
            product,
            request=request,
            old_cost=stored.purchase_price,
            old_price=stored.sale_price,
            apply_to_shelf=form.cleaned_data.get("apply_to_shelf", False),
            reason=form.cleaned_data.get("price_reason", ""),
        )
    return product


BULK_FIELDS = (
    "brand_name", "generic_name", "strength", "category", "unit", "pack_unit", "pack_size",
    "packs_per_box", "pack_cost", "pack_price", "tax_percent", "reorder_packs", "expiry_days",
    "barcode", "rx", "packing", "box_unit", "price_per",
)  # fmt: skip


def read_rows(post) -> list[dict]:
    """The grid posts one list per column. Rows with no brand name are blank and skipped."""
    columns = {f: post.getlist(f) for f in BULK_FIELDS}
    count = max(len(v) for v in columns.values())
    rows = [
        {f: (columns[f][i] if i < len(columns[f]) else "").strip() for f in BULK_FIELDS}
        for i in range(count)
    ]
    return [r for r in rows if r["brand_name"]]


_STRENGTH = re.compile(r"^(.*?\S)\s+(\d[\d./]*\s*(?:mg|mcg|g|kg|ml|l|iu|%).*)$", re.IGNORECASE)


def _row_form(row, request, *, add_to_medicines, apply_rules=True) -> ProductForm:
    """Grid prices are for the largest size on the row, as on a supplier's bill (`price_per`).
    The form turns them into the per-unit prices products store."""
    row = dict(row)
    if row.get("packing"):
        row.update({k: str(v) for k, v in parse_packing(row["packing"]).items()})
    if not row.get("strength") and (match := _STRENGTH.match(row["brand_name"])):
        row["brand_name"], row["strength"] = match.groups()
    if not row.get("rx") and row.get("category", "").isdigit():
        category = ProductCategory.objects.filter(
            organization=request.organization, pk=row["category"]
        ).first()
        row["rx"] = "1" if category and category.requires_prescription else ""
    data = {
        **row,
        "pack_size": row["pack_size"] or 1,
        "packs_per_box": row["packs_per_box"] or 1,
        "cost_per": row.get("price_per", ""),
        "tax_percent": row["tax_percent"] or 0,
        "expiry_alert_days": row["expiry_days"],
        "allow_loose_sale": "on",
        "requires_prescription": "on" if row["rx"] else "",
        "create_medicine": "on" if add_to_medicines and row["generic_name"] else "",
    }
    return ProductForm(
        data,
        instance=Product(organization=request.organization),
        request=request,
        apply_rules=apply_rules,
    )


def add_many(rows, *, request, add_to_medicines=False, apply_rules=True):
    """All rows or none. Returns (products, {row index: error text})."""
    errors, forms, seen = {}, [], set()
    for i, row in enumerate(rows):
        try:
            form = _row_form(
                row, request, add_to_medicines=add_to_medicines, apply_rules=apply_rules
            )
        except ValueError as err:
            errors[i] = str(err)
            continue
        if not form.is_valid():
            errors[i] = " ".join(
                f"{form.fields[k].label or k}: {e}" if k in form.fields else e
                for k, errs in form.errors.items()
                for e in errs
            )
        elif row["barcode"] and scan.code_key(row["barcode"]) in seen:
            errors[i] = "This barcode is on another row."
        seen.add(scan.code_key(row["barcode"]))
        forms.append(form)
    if errors or not rows:
        return [], errors
    with transaction.atomic():
        return [save_product(f, request=request) for f in forms], {}
