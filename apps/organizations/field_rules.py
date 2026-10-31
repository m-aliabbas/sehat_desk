"""Admin controls (Settings → Fields & tax): which entry fields are hidden, optional or required.
Everything is optional until an admin says otherwise. Batch and expiry on stock are not here: stock
cannot be sold safely without them."""

from .models import preferences_for

HIDDEN, OPTIONAL, REQUIRED = "hidden", "optional", "required"
LABELS = {HIDDEN: "Hidden", OPTIONAL: "Optional", REQUIRED: "Required"}

# key, label, group, the choices that make sense for it
FIELDS = [
    ("purchase.supplier_invoice", "Supplier's invoice number", "Purchases", (OPTIONAL, REQUIRED)),
    ("purchase.gst", "GST on each purchase row", "Purchases", (HIDDEN, OPTIONAL, REQUIRED)),
    ("purchase.further_tax", "Further tax on the bill", "Purchases", (HIDDEN, OPTIONAL, REQUIRED)),
    ("purchase.advance_tax", "Advance tax on the bill", "Purchases", (HIDDEN, OPTIONAL, REQUIRED)),
    ("product.generic", "Generic name", "Products", (OPTIONAL, REQUIRED)),
    ("product.category", "Category", "Products", (OPTIONAL, REQUIRED)),
    ("product.tax", "Tax column when adding products", "Products", (HIDDEN, OPTIONAL)),
    ("till.customer", "Customer name or patient", "Pharmacy till", (OPTIONAL, REQUIRED)),
    ("till.discount_reason", "Reason for a discount", "Pharmacy till", (OPTIONAL, REQUIRED)),
    ("till.discount_asked_by", "Who asked for a discount", "Pharmacy till", (OPTIONAL, REQUIRED)),
    ("visit.reason", "Reason for visit", "Reception", (OPTIONAL, REQUIRED)),
    ("visit.cnic", "CNIC of a new patient", "Reception", (OPTIONAL, REQUIRED)),
]
ALLOWED = {key: choices for key, _, _, choices in FIELDS}


def rule(organization, key) -> str:
    saved = preferences_for(organization).field_rules.get(key, OPTIONAL)
    return saved if saved in ALLOWED.get(key, ()) else OPTIONAL


def is_required(organization, key) -> bool:
    return rule(organization, key) == REQUIRED


def is_hidden(organization, key) -> bool:
    return rule(organization, key) == HIDDEN
