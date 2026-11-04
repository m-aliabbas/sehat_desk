"""Stock edits that wait for an approver (admin.approvals.manage).

`request_stock_change` records what someone asked for; `approve` runs it through
`pharmacy.stock` (the only code that moves stock) and `reject` closes it. An approver's own
request is approved at once, so admins are not slowed down, but it is still kept here.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit import services as audit
from apps.core.db import locked
from apps.pharmacy import stock
from apps.pharmacy.models import InventoryBatch

from .models import StockChangeRequest

APPROVE = "admin.approvals.manage"
Action = StockChangeRequest.Action
Status = StockChangeRequest.Status


class ApprovalError(ValidationError):
    pass


def can_approve(request) -> bool:
    return APPROVE in request.sd_perms


def pending_count(request) -> int:
    if request.branch is None:
        return 0
    return StockChangeRequest.objects.filter(
        organization=request.organization, branch=request.branch, status=Status.PENDING
    ).count()


def _check(batch, *, action, quantity, reason, new_batch_number, new_expiry_date):
    if action not in Action.values:
        raise ApprovalError("Choose what to change.")
    if not reason.strip():
        raise ApprovalError("Give the reason for the change.")
    if action == Action.DETAILS:
        if not new_batch_number and not new_expiry_date:
            raise ApprovalError("Enter the correct batch number or expiry date.")
        if new_batch_number == batch.batch_number and new_expiry_date in (None, batch.expiry_date):
            raise ApprovalError("That is already the batch's number and expiry.")
        return
    if quantity is None or quantity < 0:
        raise ApprovalError("Enter a quantity.")
    if action == Action.COUNT:
        if quantity == batch.quantity_available:
            raise ApprovalError("The count matches the system stock; nothing to adjust.")
    elif quantity == 0:
        raise ApprovalError("Enter a quantity greater than zero.")
    elif quantity > batch.quantity_available:
        raise ApprovalError(f"Only {batch.quantity_available} left in batch {batch.label}.")


@transaction.atomic
def request_stock_change(
    request,
    batch: InventoryBatch,
    *,
    action: str,
    quantity: int | None = None,
    reason: str,
    reference: str = "",
    new_batch_number: str = "",
    new_expiry_date=None,
) -> StockChangeRequest:
    """Record a stock edit. Approvers' own requests are applied straight away."""
    if "pharmacy.inventory.manage" not in request.sd_perms:
        raise ApprovalError("You are not allowed to change stock.")
    if batch.branch_id != request.branch.pk:
        raise ApprovalError("That batch belongs to another branch.")
    new_batch_number = " ".join((new_batch_number or "").split()).upper()[:40]
    _check(
        batch,
        action=action,
        quantity=quantity,
        reason=reason,
        new_batch_number=new_batch_number,
        new_expiry_date=new_expiry_date,
    )
    change = StockChangeRequest.objects.create(
        organization=request.organization,
        branch=request.branch,
        batch=batch,
        product=batch.product,
        action=action,
        quantity=None if action == Action.DETAILS else quantity,
        stock_at_request=batch.quantity_available,
        new_batch_number=new_batch_number,
        new_expiry_date=new_expiry_date,
        reason=reason.strip()[:200],
        reference=reference.strip()[:80],
        requested_by=request.user,
    )
    audit.record(
        request,
        "stock_change.request",
        change,
        summary=f"{batch.product.sku} {batch.label} {change.get_action_display()} {change.summary}",
    )
    if can_approve(request):
        change = approve(request, change, note="Approved by the person who made the change.")
    return change


@transaction.atomic
def request_unit_correction(
    request, product, *, unit: str, pack_unit: str = "", pack_size: int = 1, factor: int = 1,
    reason: str,
) -> StockChangeRequest:  # fmt: skip
    """A product saved in the wrong unit (tablet that is really a bottle). With factor > 1 the
    stock and prices are converted when approved (`pharmacy.stock.correct_unit`)."""
    if "pharmacy.inventory.manage" not in request.sd_perms:
        raise ApprovalError("You are not allowed to change stock.")
    unit = " ".join((unit or "").split()).lower()[:30]
    pack_unit = " ".join((pack_unit or "").split()).lower()[:30]
    if not unit:
        raise ApprovalError("Enter the correct unit.")
    if not reason.strip():
        raise ApprovalError("Give the reason for the correction.")
    if factor < 1 or pack_size < 1:
        raise ApprovalError("Sizes must be 1 or more.")
    if pack_size > 1 and not pack_unit:
        raise ApprovalError("Name the pack, like box or carton.")
    same_pack = pack_size == product.pack_size and (
        pack_size == 1 or pack_unit == product.pack_unit
    )
    if unit == product.unit and factor == 1 and same_pack:
        raise ApprovalError("That is already the product's unit and pack.")
    if why := stock.unit_correction_blocker(product, factor):
        raise ApprovalError(why)
    total = (
        InventoryBatch.objects.filter(product=product).aggregate(n=Sum("quantity_available"))["n"]
        or 0
    )
    change = StockChangeRequest.objects.create(
        organization=request.organization, branch=request.branch, product=product,
        action=Action.UNIT, stock_at_request=total, old_unit=product.unit, new_unit=unit,
        new_pack_unit=pack_unit if pack_size > 1 else "", new_pack_size=pack_size,
        unit_factor=factor, reason=reason.strip()[:200], requested_by=request.user,
    )  # fmt: skip
    audit.record(request, "stock_change.request", change, summary=f"{product.sku} {change.summary}")
    if can_approve(request):
        change = approve(request, change, note="Approved by the person who made the change.")
    return change


def delete_blocker(request, product) -> str | None:
    """Why a product cannot be written off and deleted from this branch, or None."""
    from apps.pharmacy.models import PurchaseItem

    elsewhere = (
        InventoryBatch.objects.filter(product=product)
        .exclude(branch=request.branch)
        .aggregate(n=Sum("quantity_available"))["n"]
        or 0
    )
    if elsewhere:
        return f"{elsewhere} is in stock in another branch. Write it off there first."
    if PurchaseItem.objects.filter(
        product=product, purchase__status__in=("draft", "ordered")
    ).exists():
        return "It is on an open purchase. Receive or cancel that purchase first."
    if product.change_requests.filter(status=Status.PENDING, action=Action.DELETE).exists():
        return "Its delete is already waiting for approval."
    return None


@transaction.atomic
def request_delete(request, product, *, write_off_as: str, reason: str) -> StockChangeRequest:
    """Write off all and delete: one request that, once approved, writes off every batch left in
    this branch and moves the product to the Recycle bin. Approvers' own requests apply at once."""
    from apps.recyclebin import services as recyclebin
    from apps.recyclebin.registry import KINDS

    if not recyclebin.can_delete(request, KINDS["product"]):
        raise ApprovalError("You are not allowed to delete products.")
    if write_off_as not in (Action.EXPIRED, Action.DAMAGED):
        raise ApprovalError("Choose expired or damaged.")
    if not reason.strip():
        raise ApprovalError("Give the reason for deleting it.")
    if why := delete_blocker(request, product):
        raise ApprovalError(why)
    left = (
        InventoryBatch.objects.filter(branch=request.branch, product=product).aggregate(
            n=Sum("quantity_available")
        )["n"]
        or 0
    )
    change = StockChangeRequest.objects.create(
        organization=request.organization, branch=request.branch, product=product,
        action=Action.DELETE, stock_at_request=left, quantity=left, write_off_as=write_off_as,
        reason=reason.strip()[:200], requested_by=request.user,
    )  # fmt: skip
    audit.record(request, "stock_change.request", change, summary=f"{product.sku} {change.summary}")
    if can_approve(request):
        change = approve(request, change, note="Approved by the person who made the change.")
    return change


@transaction.atomic
def approve(request, change: StockChangeRequest, *, note: str = "") -> StockChangeRequest:
    if not can_approve(request):
        raise ApprovalError("You are not allowed to approve stock changes.")
    change = locked(change)
    if change.status != Status.PENDING:
        raise ApprovalError(f"This request is already {change.get_status_display().lower()}.")
    batch = change.batch
    who = change.requested_by
    reason = change.reason if who == request.user else f"{change.reason} (asked by {who})"
    try:
        if change.action == Action.UNIT:
            stock.correct_unit(
                change.product,
                request=request,
                unit=change.new_unit,
                pack_unit=change.new_pack_unit,
                pack_size=change.new_pack_size,
                factor=change.unit_factor,
                reason=reason,
            )
            txn = None
        elif change.action == Action.DELETE:
            # Whatever is left now goes, sales since the request included.
            from apps.recyclebin import services as recyclebin

            stock.write_off_all(
                change.product, request=request, kind=change.write_off_as, reason=reason
            )
            try:
                recyclebin.send_to_bin(request, change.product, reason=change.reason)
            except recyclebin.BinError as err:
                raise ApprovalError(err.message) from err
            txn = None
        elif change.action == Action.DETAILS:
            stock.correct_batch(
                batch,
                request=request,
                batch_number=change.new_batch_number,
                expiry_date=change.new_expiry_date,
                reason=reason,
            )
            txn = None
        elif change.action == Action.COUNT:
            # The shelf was counted when asked; sales since then still count, so apply the
            # difference found, not the old total.
            current = locked(batch).quantity_available
            txn = stock.adjust_to_count(
                batch, request=request, counted=current + change.change, reason=reason
            )
        elif change.action == Action.PURCHASE_RETURN:
            txn = stock.purchase_return(
                batch,
                request=request,
                quantity=change.quantity,
                reason=reason,
                reference=change.reference,
            )
        else:
            txn = stock.write_off(
                batch, request=request, quantity=change.quantity, kind=change.action, reason=reason
            )
    except stock.StockError as err:
        raise ApprovalError(err.message) from err
    change.status = Status.APPROVED
    change.decided_by = request.user
    change.decided_at = timezone.now()
    change.decision_note = note.strip()[:200]
    change.transaction = txn
    change.save()
    audit.record(
        request,
        "stock_change.approve",
        change,
        summary=f"{change.product.sku} {batch.label if batch else ''} {change.summary}",
    )
    return change


@transaction.atomic
def reject(request, change: StockChangeRequest, *, note: str) -> StockChangeRequest:
    if not can_approve(request):
        raise ApprovalError("You are not allowed to reject stock changes.")
    if not note.strip():
        raise ApprovalError("Say why it is rejected.")
    change = locked(change)
    if change.status != Status.PENDING:
        raise ApprovalError(f"This request is already {change.get_status_display().lower()}.")
    change.status = Status.REJECTED
    change.decided_by = request.user
    change.decided_at = timezone.now()
    change.decision_note = note.strip()[:200]
    change.save()
    audit.record(request, "stock_change.reject", change, summary=change.decision_note)
    return change
