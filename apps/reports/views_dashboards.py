"""Laboratory and pharmacy KPI dashboards (Phase 21). The admin KPIs live on the main dashboard."""

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.accounts.decorators import has_perm, permission_required

from . import kpis

LAB_PERMS = (
    "lab.sample.collect",
    "lab.result.enter",
    "lab.result.verify",
    "lab.catalog.manage",
    "reports.view",
)
PHARMACY_PERMS = ("pharmacy.sale", "pharmacy.inventory.manage", "reports.view")


@require_GET
@permission_required(*LAB_PERMS, any_of=True)
def lab_dashboard(request):
    return render(
        request,
        "reports/lab_dashboard.html",
        {"k": kpis.lab_kpis(request), "today": timezone.localdate()},
    )


@require_GET
@permission_required(*PHARMACY_PERMS, any_of=True)
def pharmacy_dashboard(request):
    show_money = any(
        has_perm(request, c) for c in ("pharmacy.sale", "reports.financial.view", "billing.view")
    )
    return render(
        request,
        "reports/pharmacy_dashboard.html",
        {
            "k": kpis.pharmacy_kpis(request, show_money=show_money),
            "show_money": show_money,
            "today": timezone.localdate(),
        },
    )
