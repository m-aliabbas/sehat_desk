"""Patient flow board (who is where today, what each counter is owed) and its settings."""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.decorators import permission_required
from apps.core.scoping import get_scoped_or_404, scoped
from apps.masterdata.models import Service
from apps.organizations.models import Branch

from . import services
from .forms import FlowSettingsForm
from .models import CounterLocation, PatientVisit, VisitState

# Reception, pharmacy, lab and cashiers all work from the board.
COUNTERS = ("queue.manage", "pharmacy.sale", "lab.sample.collect", "billing.payment.collect")
SETTINGS = "admin.organization.manage"
# Columns in the order a patient walks through them.
COLUMNS = [
    VisitState.TOKEN_ISSUED,
    VisitState.IN_QUEUE,
    VisitState.WITH_DOCTOR,
    VisitState.LAB_QUEUE,
    VisitState.PHARMACY_QUEUE,
    VisitState.PAYMENT_DUE,
    VisitState.COMPLETED,
]


@permission_required(*COUNTERS, any_of=True)
def board(request):
    services.close_stale_visits(organization=request.organization)
    groups = services.board(request)
    perms = request.sd_perms
    return render(
        request,
        "flow/board.html",
        {
            "columns": [(VisitState(s), groups[s]) for s in COLUMNS],
            "cancelled": len(groups[VisitState.CANCELLED]),
            "counters": CounterLocation,
            "can_pull_pharmacy": "pharmacy.sale" in perms,
            "can_pull_lab": "lab.sample.collect" in perms,
            "can_collect": "billing.payment.collect" in perms,
            "can_add_fee": services.ADD_FEE in perms,
            # Procedures are ordered from the consultation's Procedures tab, not added as fees.
            "services": scoped(Service, request)
            .filter(is_active=True, is_procedure=False)
            .order_by("name"),
        },
    )


def _int(value) -> int:
    return int(value) if (value or "").isdigit() else 0


def _back(request):
    """Back to the page the button was on (a counter's own screen), else the board."""
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
        return redirect(nxt)
    return redirect(reverse("flow:board"))


@require_POST
@permission_required("pharmacy.sale", "lab.sample.collect", any_of=True)
def pull(request):
    to = request.POST.get("to", "")
    try:
        visit = services.pull_by_token(request=request, label=request.POST.get("token"), to=to)
    except services.FlowError as err:
        messages.error(request, err.message)
    else:
        messages.success(request, f"{visit.token.label} is now in the {visit.get_state_display()}.")
    return _back(request)


@require_POST
@permission_required("billing.payment.collect")
def bill(request, pk):
    visit = get_scoped_or_404(PatientVisit, request, pk=pk)
    try:
        invoice = services.counter_invoice(
            request=request, visit=visit, counter=request.POST.get("counter", "")
        )
    except services.FlowError as err:
        messages.error(request, err.message)
        return _back(request)
    return redirect("billing:invoice-pay", pk=invoice.pk)


@require_POST
@permission_required("pharmacy.sale", "lab.sample.collect", any_of=True)
def send_on(request, pk):
    visit = get_scoped_or_404(PatientVisit, request, pk=pk)
    try:
        visit = services.send_on(request=request, visit=visit)
    except services.FlowError as err:
        messages.error(request, err.message)
    else:
        messages.success(request, f"{visit.token.label}: {visit.get_state_display()}.")
    return _back(request)


@require_POST
@permission_required(services.ADD_FEE)
def charge(request, pk):
    visit = get_scoped_or_404(PatientVisit, request, pk=pk)
    service = get_scoped_or_404(Service, request, pk=_int(request.POST.get("service")))
    try:
        item = services.add_charge(request=request, visit=visit, service=service)
    except services.FlowError as err:
        messages.error(request, err.message)
    else:
        messages.success(
            request, f"{item.description} added; paid at {item.get_billed_at_display()}."
        )
    return _back(request)


@permission_required(SETTINGS)
def flow_settings(request):
    """The hospital's rules, or one branch's own (?branch=<id>)."""
    branches = list(Branch.objects.filter(organization=request.organization).order_by("name"))
    branch = next((b for b in branches if b.pk == _int(request.GET.get("branch"))), None)
    obj = services.settings_row_for_edit(request, branch)
    form = FlowSettingsForm(request.POST or None, instance=obj, for_branch=branch is not None)
    if request.method == "POST" and request.POST.get("action") == "use-hospital" and branch:
        services.use_hospital_rules(request=request, branch=branch)
        messages.success(request, f"{branch.name} follows the hospital's patient flow again.")
        return redirect("flow:settings")
    if request.method == "POST" and form.is_valid():
        services.save_settings(
            form,
            request=request,
            print_blank_rx=form.cleaned_data.get(
                "reception_prints_blank_rx", obj.reception_prints_blank_rx
            ),
        )
        messages.success(request, "Patient flow saved. New visits follow it.")
        url = reverse("flow:settings")
        return redirect(f"{url}?branch={branch.pk}" if branch else url)
    overrides = services.branch_rows(request.organization)
    return render(
        request,
        "flow/settings.html",
        {
            "form": form,
            "settings_tab": "flow",
            "branches": branches,
            "branch": branch,
            "overrides": overrides,
            "has_own": branch is not None and branch.pk in overrides,
        },
    )
