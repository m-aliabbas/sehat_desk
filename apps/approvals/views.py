"""Approvals: stock edits waiting for admin.approvals.manage, in the current branch."""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.core.scoping import get_scoped_or_404, paginate, scoped

from . import services
from .models import StockChangeRequest

APPROVE = services.APPROVE
Status = StockChangeRequest.Status


def _requests(request):
    return scoped(StockChangeRequest, request).select_related(
        "product", "batch", "requested_by", "decided_by"
    )


@require_GET
@permission_required(APPROVE)
def approval_list(request):
    show = request.GET.get("show", "pending")
    rows = _requests(request)
    rows = (
        rows.filter(status=Status.PENDING)
        if show == "pending"
        else rows.exclude(status=Status.PENDING)
    )
    return render(
        request,
        "approvals/list.html",
        {
            "rows": (page := paginate(request, rows)),
            "page_obj": page,
            "show": show,
            "pending": scoped(StockChangeRequest, request).filter(status=Status.PENDING).count(),
        },
    )


def _decide(request, change, approve: bool) -> bool:
    try:
        if approve:
            services.approve(request, change, note=request.POST.get("note", ""))
        else:
            services.reject(request, change, note=request.POST.get("reason", ""))
    except services.ApprovalError as err:
        messages.error(request, f"{change.product}: {err.message}")
        return False
    return True


@require_POST
@permission_required(APPROVE)
def approval_approve(request, pk):
    change = get_scoped_or_404(StockChangeRequest, request, pk=pk)
    if _decide(request, change, True):
        messages.success(request, f"Approved: {change.product} {change.summary}.")
    return redirect("approvals:list")


@require_POST
@permission_required(APPROVE)
def approval_reject(request, pk):
    change = get_scoped_or_404(StockChangeRequest, request, pk=pk)
    if _decide(request, change, False):
        messages.success(request, f"Rejected: {change.product}. Stock not changed.")
    return redirect("approvals:list")


@require_POST
@permission_required(APPROVE)
def approval_bulk(request):
    """Approve (or reject, with one reason) every ticked request."""
    ids = [i for i in request.POST.getlist("ids") if i.isdigit()]
    approve = request.POST.get("decision") != "reject"
    if not approve and not request.POST.get("reason", "").strip():
        messages.error(request, "Give a reason to reject.")
        return redirect("approvals:list")
    done = sum(
        _decide(request, change, approve)
        for change in _requests(request).filter(pk__in=ids, status=Status.PENDING)
    )
    if done:
        messages.success(request, f"{done} request{'s' if done != 1 else ''} "
                         f"{'approved' if approve else 'rejected'}.")  # fmt: skip
    elif not ids:
        messages.error(request, "Tick the requests first.")
    return redirect("approvals:list")
