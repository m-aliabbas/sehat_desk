from django.conf import settings
from django.db import models

from apps.core.scoping import BranchScopedModel


class StockChangeRequest(BranchScopedModel):
    """A stock edit someone asked for. Stock moves only when an approver accepts it, so a count or
    write-off cannot hide missing stock. People who hold admin.approvals.manage are approved on
    the spot; the request is still kept for the audit."""

    class Action(models.TextChoices):
        COUNT = "count", "Physical count"
        EXPIRED = "expired", "Write off as expired"
        DAMAGED = "damaged", "Write off as damaged"
        PURCHASE_RETURN = "purchase_return", "Return to supplier"
        DETAILS = "details", "Correct batch number or expiry"
        UNIT = "unit", "Correct unit"
        DELETE = "delete", "Write off all and delete"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    # Empty for a unit correction, which covers the whole product.
    batch = models.ForeignKey(
        "pharmacy.InventoryBatch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="change_requests",
    )
    product = models.ForeignKey(
        "pharmacy.Product", on_delete=models.PROTECT, related_name="change_requests"
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    # count: counted quantity; write-offs and returns: units removed. Unused for details.
    quantity = models.IntegerField(null=True, blank=True)
    stock_at_request = models.IntegerField(default=0)
    new_batch_number = models.CharField(max_length=40, blank=True)
    new_expiry_date = models.DateField(null=True, blank=True)
    # Unit correction: the right unit and pack, and how many old units make one new unit.
    old_unit = models.CharField(max_length=30, blank=True)
    new_unit = models.CharField(max_length=30, blank=True)
    new_pack_unit = models.CharField(max_length=30, blank=True)
    new_pack_size = models.PositiveIntegerField(default=1)
    unit_factor = models.PositiveIntegerField(default=1)
    # Write off all and delete: what the stock left is written off as (expired or damaged).
    write_off_as = models.CharField(max_length=10, blank=True)
    reason = models.CharField(max_length=200)
    reference = models.CharField(max_length=80, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True)
    transaction = models.ForeignKey(
        "pharmacy.StockTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "branch", "status", "-created_at"])]

    def __str__(self):
        return f"{self.get_action_display()} · {self.batch_id} · {self.get_status_display()}"

    @property
    def change(self) -> int | None:
        """Signed units this request moves, as asked (None for a details correction)."""
        if self.action == self.Action.DETAILS:
            return None
        if self.action == self.Action.UNIT:
            if self.unit_factor == 1:
                return None
            return self.stock_at_request // self.unit_factor - self.stock_at_request
        if self.action == self.Action.COUNT:
            return (self.quantity or 0) - self.stock_at_request
        if self.action == self.Action.DELETE:
            return -self.stock_at_request
        return -(self.quantity or 0)

    @property
    def summary(self) -> str:
        if self.action == self.Action.UNIT:
            text = f"{self.old_unit} → {self.new_unit}"
            if self.unit_factor > 1:
                text += f" ({self.unit_factor} {self.old_unit} = 1 {self.new_unit})"
            if self.new_pack_size > 1:
                text += f", {self.new_pack_unit} of {self.new_pack_size}"
            return text
        if self.action == self.Action.DETAILS:
            parts = []
            if self.new_batch_number:
                parts.append(f"batch → {self.new_batch_number}")
            if self.new_expiry_date:
                parts.append(f"expiry → {self.new_expiry_date:%d %b %Y}")
            return ", ".join(parts)
        if self.action == self.Action.COUNT:
            return f"{self.stock_at_request} → {self.quantity} ({self.change:+d})"
        if self.action == self.Action.DELETE:
            return f"{self.change:+d} {self.write_off_as}, then Recycle bin"
        return f"{self.change:+d}"
