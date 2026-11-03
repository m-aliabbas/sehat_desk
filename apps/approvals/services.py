"""Stock edits that wait for an approver (admin.approvals.manage).

`request_stock_change` records what someone asked for; `approve` runs it through
`pharmacy.stock` (the only code that moves stock) and `reject` closes it. An approver's own
request is approved at once, so admins are not slowed down, but it is still kept here.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
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
        if change.action == Action.DETAILS:
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
        summary=f"{batch.product.sku} {batch.label} {change.summary}",
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
