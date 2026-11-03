"""Pharmacy inventory (PRD §9.1, §9.2, §9.4): products, batches and the stock ledger.

Stock rules (CLAUDE.md: stock writes are transactional):
- Quantities are counted in the product's base unit (tablet, bottle, ...). Pack size converts.
- A batch's available quantity changes only through apps/pharmacy/stock.py, which writes a
  StockTransaction for every change in the same database transaction. InventoryBatch.save()
  refuses any other change, and a database constraint keeps stock from going negative.
- The ledger is append-only. Mistakes are corrected by a counter-entry (adjustment/return).
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.models import LIVE, RecycleBinMixin, TimeStampedModel
from apps.core.scoping import BranchScopedModel, OrgScopedModel
from apps.masterdata.models import money

# Prices per base unit keep six decimals: a box of 30 at 355 is 11.833333 a tablet, and 30 of them
# must come back to exactly 355.00. Screens show two decimals.
unit_money = {
    "max_digits": 16,
    "decimal_places": 6,
    "validators": [MinValueValidator(Decimal("0"))],
}


class UnitOfMeasure(RecycleBinMixin, OrgScopedModel):
    """A packing phrase the hospital wants suggested (packing.py): "Strip of 14 tablets"."""

    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], condition=LIVE, name="uniq_uom_name"
            )
        ]

    def __str__(self):
        return self.name


class ProductCategory(RecycleBinMixin, OrgScopedModel):
    # Filled into a new product when this category is picked; each product can still differ.
    default_tax_percent = models.DecimalField(
        "Default tax (%)",
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text="Added on top of the sale price at the till. Leave 0 if the printed price "
        "already includes tax.",
    )
    requires_prescription = models.BooleanField("Prescription needed by default", default=False)
    tax_note = models.CharField(max_length=200, blank=True)
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "product categories"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], condition=LIVE, name="uniq_product_category"
            )
        ]

    def __str__(self):
        return self.name


class Product(RecycleBinMixin, OrgScopedModel):
    class Unit(models.TextChoices):
        TABLET = "tablet", "Tablet"
        CAPSULE = "capsule", "Capsule"
        BOTTLE = "bottle", "Bottle"
        TUBE = "tube", "Tube"
        VIAL = "vial", "Vial"
        AMPOULE = "ampoule", "Ampoule"
        SACHET = "sachet", "Sachet"
        INHALER = "inhaler", "Inhaler"
        PIECE = "piece", "Piece"
        PACK = "pack", "Pack"

    class PackUnit(models.TextChoices):
        STRIP = "strip", "Strip"
        BOX = "box", "Box"
        BOTTLE = "bottle", "Bottle"
        PACK = "pack", "Pack"
        TUBE = "tube", "Tube"
        CARTON = "carton", "Carton"

    # What a pack of each base unit is usually called (tablets come in strips, vials in boxes).
    PACK_FOR_UNIT = {
        "bottle": "bottle", "tube": "tube", "inhaler": "box", "vial": "box",
        "ampoule": "box", "sachet": "box", "piece": "pack", "pack": "pack",
    }  # fmt: skip

    sku = models.CharField("SKU", max_length=30)
    barcode = models.CharField(max_length=40, blank=True)
    brand_name = models.CharField(max_length=120)
    generic_name = models.CharField(max_length=160, blank=True)
    medicine = models.ForeignKey(
        "prescriptions.Medicine",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="products",
        help_text="Link to the medicine master so prescriptions can be matched to stock.",
    )
    strength = models.CharField(max_length=60, blank=True)
    manufacturer = models.CharField(max_length=120, blank=True)
    category = models.ForeignKey(
        ProductCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="products"
    )
    # Free text: the Unit and PackUnit lists are suggestions, a hospital can use any name.
    unit = models.CharField(
        "Smallest unit sold",
        max_length=30,
        default=Unit.TABLET,
        help_text="Tablet, ml, vial, sachet, or anything else. Stock is counted in this.",
    )
    pack_size = models.PositiveIntegerField(
        default=1, help_text="Base units in one pack/strip/box, e.g. 10 tablets."
    )
    pack_unit = models.CharField(
        "Pack name", max_length=30, default=PackUnit.STRIP,
        help_text="What one pack is called: strip, bottle, tray, anything.",
    )  # fmt: skip
    box_unit = models.CharField(
        "Box name", max_length=30, default="box",
        help_text="What the larger pack is called: box, carton, anything.",
    )  # fmt: skip
    packs_per_box = models.PositiveIntegerField(
        "Packs in one box (larger pack)",
        default=1,
        validators=[MinValueValidator(1)],
        help_text="e.g. 10 strips in a box. 1 = not bought or sold by the box.",
    )
    preferred_vendor = models.ForeignKey(
        "pharmacy.Vendor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_products",
        verbose_name="Usual supplier",
        help_text="Automatic orders go here. Blank = whoever supplied it last.",
    )
    allow_loose_sale = models.BooleanField(
        default=True,
        help_text="A pack can be opened, like 5 tablets from a strip. Off = whole packs only.",
    )
    purchase_price = models.DecimalField(
        **unit_money, default=Decimal("0"), help_text="Default cost per unit."
    )
    sale_price = models.DecimalField(
        **unit_money, default=Decimal("0"), help_text="Price per unit for new stock."
    )
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    reorder_level = models.PositiveIntegerField(
        "Minimum stock",
        default=0,
        help_text="In base units. At or below this it shows as low stock and on the automatic "
        "purchase order. 0 = no minimum.",
    )
    expiry_alert_days = models.PositiveSmallIntegerField(
        "Expiry alert (days before)",
        null=True,
        blank=True,
        help_text="Blank = hospital default.",
    )
    requires_prescription = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    search_text = models.CharField(max_length=400, editable=False, blank=True, db_index=True)

    class Meta:
        ordering = ["brand_name", "strength"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "sku"], condition=LIVE, name="uniq_product_sku"
            ),
            models.UniqueConstraint(
                fields=["organization", "barcode"],
                condition=~models.Q(barcode="") & LIVE,
                name="uniq_product_barcode",
            ),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        self.search_text = " ".join(
            [self.sku, self.barcode, self.brand_name, self.generic_name, self.strength]
        ).lower()
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        return " ".join(p for p in [self.brand_name, self.strength] if p)

    # --- Units of measure: stock is counted in the base unit; a pack holds pack_size of them.

    def get_unit_display(self) -> str:
        return self.unit.capitalize()

    def get_pack_unit_display(self) -> str:
        return self.pack_unit.capitalize()

    def unit_label(self, count: int = 1) -> str:
        return _plural(self.unit.lower(), count)

    def pack_label(self, count: int = 1) -> str:
        return _plural(self.pack_unit.lower(), count)

    def box_label(self, count: int = 1) -> str:
        return _plural(self.box_unit.lower(), count)

    @property
    def has_packs(self) -> bool:
        return self.pack_size > 1

    @property
    def has_boxes(self) -> bool:
        return self.packs_per_box > 1

    @property
    def box_size(self) -> int:
        """Base units in one box."""
        return self.pack_size * self.packs_per_box

    @property
    def pack_price(self) -> Decimal:
        return (self.sale_price * self.pack_size).quantize(Decimal("0.01"))

    @property
    def box_price(self) -> Decimal:
        return (self.sale_price * self.box_size).quantize(Decimal("0.01"))

    @property
    def top_level(self) -> str:
        """The largest size this product comes in: "box", "pack" or "unit"."""
        return "box" if self.has_boxes else "pack" if self.has_packs else "unit"

    @property
    def top_level_name(self) -> str:
        return self.level_name(self.top_level)

    def units_in(self, level: str) -> int:
        return {"box": self.box_size, "pack": self.pack_size}.get(level, 1)

    def level_name(self, level: str) -> str:
        return {"box": self.box_unit, "pack": self.pack_unit}.get(level, self.unit).lower()

    @property
    def price_ladder(self) -> list[dict]:
        """Every size it sells in, smallest first, with what one of them costs and sells for."""
        levels = (
            ["unit"] + (["pack"] if self.has_packs else []) + (["box"] if self.has_boxes else [])
        )
        return [
            {
                "level": level,
                "name": self.level_name(level),
                "units": self.units_in(level),
                "cost": (self.purchase_price * self.units_in(level)).quantize(Decimal("0.01")),
                "price": (self.sale_price * self.units_in(level)).quantize(Decimal("0.01")),
            }
            for level in levels
        ]

    def pack_breakdown(self, quantity: int) -> str:
        """Base units as packs: 25 -> "2 strips + 5"; 125 with 10 strips a box ->
        "1 box + 2 strips + 5"; "" when under one pack."""
        if not self.has_packs:
            return ""
        packs, loose = divmod(abs(quantity or 0), self.pack_size)
        if not packs:
            return ""
        boxes, packs = divmod(packs, self.packs_per_box) if self.has_boxes else (0, packs)
        parts = [f"{boxes} {self.box_label(boxes)}"] if boxes else []
        if packs:
            parts.append(f"{packs} {self.pack_label(packs)}")
        if loose:
            parts.append(str(loose))
        return " + ".join(parts)

    def describe_quantity(self, quantity: int) -> str:
        """Base units in words: "25 tablets (2 strips + 5)", "5 tablets"."""
        quantity = quantity or 0
        text = f"{quantity} {self.unit_label(quantity)}"
        packs = self.pack_breakdown(quantity)
        return f"{text} ({packs})" if packs else text

    def loose_sale_error(self, quantity: int) -> str:
        """Why this quantity cannot be sold, or "" when it can."""
        if self.allow_loose_sale or not self.has_packs or quantity % self.pack_size == 0:
            return ""
        return (
            f"{self} is sold in whole {self.pack_label(2)} of {self.pack_size} "
            f"{self.unit_label(self.pack_size)} only. Enter a multiple of {self.pack_size}."
        )


def _plural(word: str, count: int) -> str:
    if abs(count) == 1:
        return word
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


class InventoryBatch(BranchScopedModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="batches")
    batch_number = models.CharField(max_length=40)
    # The same printed batch can arrive twice at a different cost or price. Each is its own lot, so
    # stock value and margin stay true; the batch number stays the one printed, for recalls.
    lot = models.PositiveSmallIntegerField(default=1)
    expiry_date = models.DateField()
    purchase_cost = models.DecimalField(**unit_money, help_text="Cost per unit.")
    sale_price = models.DecimalField(**unit_money, help_text="Sale price per unit for this batch.")
    quantity_received = models.PositiveIntegerField(default=0)
    quantity_available = models.IntegerField(default=0)

    class Meta:
        ordering = ["expiry_date", "pk"]
        verbose_name_plural = "inventory batches"
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "product", "batch_number", "lot"],
                name="uniq_batch_lot_per_branch",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_available__gte=0), name="batch_stock_not_negative"
            ),
        ]
        indexes = [models.Index(fields=["branch", "product", "expiry_date"])]

    def __str__(self):
        return f"{self.product} · {self.label}"

    @property
    def label(self) -> str:
        return self.batch_number if self.lot == 1 else f"{self.batch_number} (lot {self.lot})"

    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_ledger_write", False):
            stored = (
                InventoryBatch.objects.filter(pk=self.pk)
                .values_list("quantity_available", "quantity_received")
                .first()
            )
            if stored and stored != (self.quantity_available, self.quantity_received):
                raise ValidationError(
                    "Stock quantities change only through the stock ledger (pharmacy.stock)."
                )
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return self.expiry_date < timezone.localdate()

    @property
    def days_to_expiry(self) -> int:
        return (self.expiry_date - timezone.localdate()).days

    @property
    def stock_value(self) -> Decimal:
        return self.purchase_cost * self.quantity_available


class StockTransaction(BranchScopedModel):
    class Kind(models.TextChoices):
        OPENING = "opening", "Opening stock"
        PURCHASE = "purchase", "Purchase stock-in"
        SALE = "sale", "Sale"
        SALE_RETURN = "sale_return", "Sale return"
        PURCHASE_RETURN = "purchase_return", "Purchase return"
        ADJUSTMENT = "adjustment", "Stock adjustment"
        EXPIRED = "expired", "Expired write-off"
        DAMAGED = "damaged", "Damaged write-off"

    INCOMING = (Kind.OPENING, Kind.PURCHASE, Kind.SALE_RETURN)
    OUTGOING = (Kind.SALE, Kind.PURCHASE_RETURN, Kind.EXPIRED, Kind.DAMAGED)

    kind = models.CharField(max_length=16, choices=Kind.choices)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="transactions")
    batch = models.ForeignKey(InventoryBatch, on_delete=models.PROTECT, related_name="transactions")
    quantity = models.IntegerField(help_text="Signed: positive in, negative out.")
    balance_after = models.IntegerField(help_text="What this batch held after the movement.")
    unit_cost = models.DecimalField(**unit_money)
    unit_price = models.DecimalField(
        **unit_money, default=Decimal("0"), help_text="The batch's sale price at the time."
    )
    reference = models.CharField(
        max_length=80, blank=True, help_text="Supplier invoice, sale or return number."
    )
    reason = models.CharField(max_length=200, blank=True)
    original = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="returns",
        help_text="For returns: the sale or purchase being returned.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["branch", "product", "-created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.quantity:+d} {self.batch}"

    @property
    def value(self) -> Decimal:
        """Stock value this movement added or took away, at the batch's cost."""
        return (self.unit_cost * self.quantity).quantize(Decimal("0.01"))

    @property
    def document(self) -> tuple[str, str] | None:
        """(number, link) of the bill or sale behind this movement, when there is one."""
        for attr, field, route in (
            ("sale_item", "sale", "sale-detail"),
            ("sale_return_item", "sale_return", None),
            ("purchase_item", "purchase", "purchase-detail"),
            ("purchase_return_item", "purchase_return", None),
        ):
            line = getattr(self, attr, None)
            if line is None:
                continue
            doc = getattr(line, field)
            if attr == "sale_return_item":
                return doc.number, reverse("pharmacy:sale-detail", args=[doc.sale_id])
            if attr == "purchase_return_item":
                if doc.purchase_id is None:
                    return doc.number, reverse("pharmacy:vendor-detail", args=[doc.vendor_id])
                return doc.number, reverse("pharmacy:purchase-detail", args=[doc.purchase_id])
            return doc.number, reverse(f"pharmacy:{route}", args=[doc.pk])
        return None


class ProductPriceChange(OrgScopedModel):
    """Append-only history of a product's cost and sale price, per base unit. A price change moves
    no stock, so it is not a StockTransaction; the product ledger shows both side by side."""

    class Scope(models.TextChoices):
        NEW_STOCK = "new_stock", "New stock only"
        SHELF = "shelf", "New stock and stock on the shelf"
        BATCH = "batch", "One batch"
        RECEIPT = "receipt", "From a purchase"

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="price_changes")
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        help_text="Where stock was repriced. Blank when only the product's price changed.",
    )
    batch = models.ForeignKey(
        InventoryBatch, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.NEW_STOCK)
    old_cost = models.DecimalField(**unit_money)
    new_cost = models.DecimalField(**unit_money)
    old_price = models.DecimalField(**unit_money)
    new_price = models.DecimalField(**unit_money)
    units_repriced = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=200, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["product", "-created_at"])]

    def __str__(self):
        return f"{self.product}: {self.old_price} to {self.new_price}"


# --- Point of sale (Phase 13, PRD §9.3) --------------------------------------------------------
#
# A sale is a cart (open/held) until checkout. Each line is one product from one batch, so the
# cart shows the batch and price that will be sold. Checkout (apps/pharmacy/pos.py) re-checks the
# stock, posts the stock-out, issues a billing invoice and records the payments in one database
# transaction. A completed sale is never edited; returns are separate rows that restock the batch
# and refund money from the original payment.


class PharmacySale(BranchScopedModel):
    class Status(models.TextChoices):
        OPEN = "open", "In progress"
        HELD = "held", "Held"
        COMPLETED = "completed", "Completed"
        VOIDED = "voided", "Cleared"

    number = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pharmacy_sales",
    )
    customer_name = models.CharField(max_length=120, blank=True, help_text="Walk-in customer.")
    prescription = models.ForeignKey(
        "prescriptions.Prescription",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pharmacy_sales",
    )
    invoice = models.OneToOneField(
        "billing.Invoice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pharmacy_sale",
    )
    discount = models.DecimalField(**money, default=Decimal("0"))
    discount_reason = models.CharField(max_length=200, blank=True)
    # Who asked for it (a doctor, a manager); discount_by and each row's discount_by gave it.
    discount_asked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    discount_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    hold_note = models.CharField(max_length=120, blank=True)
    held_at = models.DateTimeField(null=True, blank=True)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Who is working the cart (and completed the sale).",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    grand_total = models.DecimalField(**money, default=Decimal("0"))

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_pharmacy_sale_number"
            )
        ]
        indexes = [models.Index(fields=["branch", "status", "-created_at"])]

    def __str__(self):
        return self.number

    @property
    def bill_to(self) -> str:
        return self.patient.full_name if self.patient_id else (self.customer_name or "Walk-in")

    @property
    def is_cart(self) -> bool:
        return self.status in (self.Status.OPEN, self.Status.HELD)


class PharmacySaleItem(TimeStampedModel):
    sale = models.ForeignKey(PharmacySale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    batch = models.ForeignKey(InventoryBatch, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(**unit_money)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    discount_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    prescription_item = models.ForeignKey(
        "prescriptions.PrescriptionItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dispensed_lines",
    )
    substituted = models.BooleanField(
        default=False, help_text="A different product than the one prescribed."
    )
    substitution_reason = models.CharField(max_length=200, blank=True)
    stock_txn = models.OneToOneField(
        StockTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sale_item",
        help_text="The sale stock-out, set at checkout.",
    )

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["sale", "batch"], name="uniq_sale_line_per_batch")
        ]

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def gross(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))

    @property
    def discount_amount(self) -> Decimal:
        return (self.gross * self.discount_percent / 100).quantize(Decimal("0.01"))

    @property
    def tax_amount(self) -> Decimal:
        net = self.gross - self.discount_amount
        return (net * self.tax_percent / 100).quantize(Decimal("0.01"))

    @property
    def line_total(self) -> Decimal:
        return self.gross - self.discount_amount + self.tax_amount

    @property
    def sold_as(self) -> tuple[int, str]:
        """(quantity, "unit" | "pack" | "box"): the largest whole size this quantity fits."""
        product = self.product
        if product.has_boxes and self.quantity % product.box_size == 0:
            return self.quantity // product.box_size, "box"
        if product.has_packs and self.quantity % product.pack_size == 0:
            return self.quantity // product.pack_size, "pack"
        return self.quantity, "unit"

    @property
    def sold_qty(self) -> int:
        return self.sold_as[0]

    @property
    def sold_unit(self) -> str:
        return self.sold_as[1]

    @property
    def sold_price(self) -> Decimal:
        """The price of one of whatever this row is sold in."""
        size = {"box": self.product.box_size, "pack": self.product.pack_size}.get(self.sold_unit, 1)
        return (self.unit_price * size).quantize(Decimal("0.01"))

    @property
    def returned_quantity(self) -> int:
        return self.return_lines.aggregate(s=models.Sum("quantity"))["s"] or 0


class PharmacyCartReturn(TimeStampedModel):
    """A return entered on the till. It waits on the cart and is carried out at checkout through
    pos.return_items, so the refund and restock are the same as any other sale return."""

    sale = models.ForeignKey(PharmacySale, on_delete=models.CASCADE, related_name="cart_returns")
    source = models.ForeignKey(PharmacySaleItem, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=200, default="Customer return")
    settled_by = models.ForeignKey(
        "pharmacy.PharmacySaleReturn",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["sale", "source"], name="uniq_cart_return_per_source")
        ]

    def __str__(self):
        return f"Return {self.source.product} × {self.quantity}"

    @property
    def amount(self) -> Decimal:
        """What this return refunds: the original price, less its share of any sale discount."""
        source = self.source
        sale = source.sale
        lines = sum((i.line_total for i in sale.items.all()), Decimal("0"))
        factor = (sale.invoice.grand_total / lines) if lines and sale.invoice_id else Decimal(0)
        return (source.line_total * self.quantity / source.quantity * factor).quantize(
            Decimal("0.01")
        )


class PharmacySaleReturn(BranchScopedModel):
    number = models.CharField(max_length=20)
    sale = models.ForeignKey(PharmacySale, on_delete=models.PROTECT, related_name="returns")
    reason = models.CharField(max_length=200)
    amount = models.DecimalField(**money, help_text="Money refunded to the customer.")
    refunds = models.ManyToManyField("billing.Refund", blank=True, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_pharmacy_return_number"
            )
        ]

    def __str__(self):
        return self.number


class PharmacySaleReturnItem(models.Model):
    sale_return = models.ForeignKey(
        PharmacySaleReturn, on_delete=models.CASCADE, related_name="items"
    )
    sale_item = models.ForeignKey(
        PharmacySaleItem, on_delete=models.PROTECT, related_name="return_lines"
    )
    quantity = models.PositiveIntegerField()
    amount = models.DecimalField(**money)
    stock_txn = models.OneToOneField(
        StockTransaction, on_delete=models.PROTECT, related_name="sale_return_item"
    )

    def __str__(self):
        return f"{self.sale_item.product} × {self.quantity}"


# --- Purchasing (Phase 14) ---------------------------------------------------------------------
#
# Vendor → Purchase (draft → ordered → received; or cancelled before receipt). Receiving posts
# every line to stock (stock.receive) and the purchase total to the supplier ledger in one
# transaction. Purchase returns send stock back (stock.purchase_return) and credit the ledger.
# Supplier payments are not built yet; the ledger already has a kind for them.


class Vendor(RecycleBinMixin, OrgScopedModel):
    name = models.CharField(max_length=120)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=300, blank=True)
    tax_number = models.CharField("NTN / tax number", max_length=40, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(
        default=0, help_text="Days of credit the supplier gives. 0 = pay on delivery."
    )
    notes = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], condition=LIVE, name="uniq_vendor_name"
            )
        ]

    def __str__(self):
        return self.name

    @property
    def balance(self) -> Decimal:
        """What the hospital owes this supplier, across branches."""
        return self.ledger.aggregate(s=models.Sum("amount"))["s"] or Decimal("0.00")


class Purchase(BranchScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ORDERED = "ordered", "Ordered"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    EDITABLE = (Status.DRAFT, Status.ORDERED)

    number = models.CharField(max_length=20)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchases")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    supplier_invoice_number = models.CharField(max_length=60, blank=True)
    supplier_invoice_date = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    subtotal = models.DecimalField(**money, default=Decimal("0"))
    discount = models.DecimalField(**money, default=Decimal("0"))
    tax_amount = models.DecimalField(
        **money, default=Decimal("0"), help_text="Tax charged on the supplier invoice."
    )
    # Taxes on the supplier's bill, kept for the hospital's tax records.
    line_tax = models.DecimalField(
        "GST on lines", **money, default=Decimal("0"), help_text="Sum of each line's GST."
    )
    further_tax = models.DecimalField(**money, default=Decimal("0"))
    advance_tax = models.DecimalField("Advance / withholding tax", **money, default=Decimal("0"))
    grand_total = models.DecimalField(**money, default=Decimal("0"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    ordered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_purchase_number")
        ]
        indexes = [models.Index(fields=["branch", "status", "-created_at"])]

    def __str__(self):
        return self.number

    @property
    def is_editable(self) -> bool:
        return self.status in self.EDITABLE


class PurchaseItem(TimeStampedModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(help_text="Units bought (base units).")
    free_quantity = models.PositiveIntegerField(
        default=0, help_text="Bonus units the supplier gives free."
    )
    # Four decimals: a box of 30 at 100 must come back to exactly 100 on the supplier's bill.
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Cost per unit bought.",
    )
    tax_percent = models.DecimalField(
        "GST %",
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    discount_percent = models.DecimalField(
        "Discount %",
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    # Filled in when the goods arrive (goods receipt).
    batch_number = models.CharField(max_length=40, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    sale_price = models.DecimalField(
        **unit_money, null=True, blank=True, help_text="Blank = the product's sale price."
    )
    stock_txn = models.OneToOneField(
        StockTransaction,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_item",
    )

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def line_cost(self) -> Decimal:
        """After this line's discount: what the supplier charges for it, before GST."""
        gross = self.unit_cost * self.quantity
        return (gross * (100 - self.discount_percent) / 100).quantize(Decimal("0.01"))

    @property
    def tax_amount(self) -> Decimal:
        return (self.line_cost * self.tax_percent / 100).quantize(Decimal("0.01"))

    @property
    def received_quantity(self) -> int:
        return self.quantity + self.free_quantity

    @property
    def effective_unit_cost(self) -> Decimal:
        """Cost per unit on the shelf: free units lower it."""
        if not self.received_quantity:
            return self.unit_cost
        return (self.line_cost / self.received_quantity).quantize(Decimal("0.000001"))

    @property
    def returned_quantity(self) -> int:
        return self.return_lines.aggregate(s=models.Sum("quantity"))["s"] or 0


class PurchaseReturn(BranchScopedModel):
    number = models.CharField(max_length=20)
    # Empty for a return entered by supplier and medicine, not from one purchase.
    purchase = models.ForeignKey(
        Purchase, on_delete=models.PROTECT, null=True, blank=True, related_name="returns"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="returns")
    reason = models.CharField(max_length=200)
    reference = models.CharField(
        max_length=80, blank=True, help_text="Supplier debit/credit note number."
    )
    amount = models.DecimalField(**money)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_purchase_return_number"
            )
        ]

    def __str__(self):
        return self.number


class PurchaseReturnItem(models.Model):
    purchase_return = models.ForeignKey(
        PurchaseReturn, on_delete=models.CASCADE, related_name="items"
    )
    # The bought line when one is known; stock from opening balances or Receive stock has none.
    purchase_item = models.ForeignKey(
        PurchaseItem, on_delete=models.PROTECT, null=True, blank=True, related_name="return_lines"
    )
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(**money)
    amount = models.DecimalField(**money)
    stock_txn = models.OneToOneField(
        StockTransaction, on_delete=models.PROTECT, related_name="purchase_return_item"
    )

    def __str__(self):
        return f"{self.product} × {self.quantity}"

    @property
    def product(self):
        return self.stock_txn.product


class VendorLedgerEntry(BranchScopedModel):
    """Append-only supplier account. amount > 0: the hospital owes more; < 0: owes less.
    Balance = sum of amounts. Supplier payments (kind "payment") are recorded in Finance."""

    class Kind(models.TextChoices):
        OPENING = "opening", "Opening balance"
        PURCHASE = "purchase", "Purchase"
        PURCHASE_RETURN = "purchase_return", "Purchase return"
        PAYMENT = "payment", "Payment to supplier"
        ADJUSTMENT = "adjustment", "Adjustment"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="ledger")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    purchase = models.ForeignKey(
        Purchase, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    purchase_return = models.ForeignKey(
        PurchaseReturn, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=200, blank=True)
    # Supplier payments (apps.finance): how the money left the hospital.
    method = models.ForeignKey(
        "billing.PaymentMethod", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reference = models.CharField(max_length=80, blank=True)
    idempotency_key = models.CharField(max_length=40, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name_plural = "vendor ledger entries"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_vendor_ledger_idempotency_key",
            )
        ]

    def __str__(self):
        return f"{self.vendor} {self.get_kind_display()} {self.amount}"
