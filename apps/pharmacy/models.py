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
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import BranchScopedModel, OrgScopedModel
from apps.masterdata.models import money


class ProductCategory(OrgScopedModel):
    name = models.CharField(max_length=80)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "product categories"
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_product_category")
        ]

    def __str__(self):
        return self.name


class Product(OrgScopedModel):
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
    unit = models.CharField(
        max_length=10, choices=Unit.choices, default=Unit.TABLET, help_text="Base unit sold."
    )
    pack_size = models.PositiveIntegerField(
        default=1, help_text="Base units in one pack/strip/box, e.g. 10 tablets."
    )
    purchase_price = models.DecimalField(
        **money, default=Decimal("0"), help_text="Default cost per unit."
    )
    sale_price = models.DecimalField(**money, default=Decimal("0"), help_text="Price per unit.")
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    reorder_level = models.PositiveIntegerField(
        default=0, help_text="Warn when stock in this branch falls to this many units."
    )
    requires_prescription = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    search_text = models.CharField(max_length=400, editable=False, blank=True, db_index=True)

    class Meta:
        ordering = ["brand_name", "strength"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "sku"], name="uniq_product_sku"),
            models.UniqueConstraint(
                fields=["organization", "barcode"],
                condition=~models.Q(barcode=""),
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


class InventoryBatch(BranchScopedModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="batches")
    batch_number = models.CharField(max_length=40)
    expiry_date = models.DateField()
    purchase_cost = models.DecimalField(**money, help_text="Cost per unit.")
    sale_price = models.DecimalField(**money, help_text="Sale price per unit for this batch.")
    quantity_received = models.PositiveIntegerField(default=0)
    quantity_available = models.IntegerField(default=0)

    class Meta:
        ordering = ["expiry_date", "pk"]
        verbose_name_plural = "inventory batches"
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "product", "batch_number"], name="uniq_batch_per_branch"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_available__gte=0), name="batch_stock_not_negative"
            ),
        ]
        indexes = [models.Index(fields=["branch", "product", "expiry_date"])]

    def __str__(self):
        return f"{self.product} · {self.batch_number}"

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
    balance_after = models.IntegerField()
    unit_cost = models.DecimalField(**money)
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
    unit_price = models.DecimalField(**money)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
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
    def tax_amount(self) -> Decimal:
        return (self.gross * self.tax_percent / 100).quantize(Decimal("0.01"))

    @property
    def line_total(self) -> Decimal:
        return self.gross + self.tax_amount

    @property
    def returned_quantity(self) -> int:
        return self.return_lines.aggregate(s=models.Sum("quantity"))["s"] or 0


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


class Vendor(OrgScopedModel):
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
            models.UniqueConstraint(fields=["organization", "name"], name="uniq_vendor_name")
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
    unit_cost = models.DecimalField(**money, help_text="Cost per unit bought.")
    # Filled in when the goods arrive (goods receipt).
    batch_number = models.CharField(max_length=40, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    sale_price = models.DecimalField(
        **money, null=True, blank=True, help_text="Blank = the product's sale price."
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
        return (self.unit_cost * self.quantity).quantize(Decimal("0.01"))

    @property
    def received_quantity(self) -> int:
        return self.quantity + self.free_quantity

    @property
    def effective_unit_cost(self) -> Decimal:
        """Cost per unit on the shelf: free units lower it."""
        if not self.received_quantity:
            return self.unit_cost
        return (self.line_cost / self.received_quantity).quantize(Decimal("0.01"))

    @property
    def returned_quantity(self) -> int:
        return self.return_lines.aggregate(s=models.Sum("quantity"))["s"] or 0


class PurchaseReturn(BranchScopedModel):
    number = models.CharField(max_length=20)
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="returns")
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
    purchase_item = models.ForeignKey(
        PurchaseItem, on_delete=models.PROTECT, related_name="return_lines"
    )
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(**money)
    amount = models.DecimalField(**money)
    stock_txn = models.OneToOneField(
        StockTransaction, on_delete=models.PROTECT, related_name="purchase_return_item"
    )

    def __str__(self):
        return f"{self.purchase_item.product} × {self.quantity}"


class VendorLedgerEntry(BranchScopedModel):
    """Append-only supplier account. amount > 0: the hospital owes more; < 0: owes less.
    Balance = sum of amounts. Supplier payments (kind "payment") arrive in a later phase."""

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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at", "pk"]
        verbose_name_plural = "vendor ledger entries"

    def __str__(self):
        return f"{self.vendor} {self.get_kind_display()} {self.amount}"
