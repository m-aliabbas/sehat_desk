"""Finance: admin dashboard, profit & loss, complete ledger, expenses and supplier payments."""

import csv
import uuid
from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.accounts.services import accessible_branches, effective_permissions
from apps.audit import services as audit
from apps.billing.models import ZERO
from apps.core.scoping import get_scoped_or_404, paginate, scoped

from . import segments, services, statements
from .forms import CategoryForm, ExpenseForm, SupplierPaymentForm, VoidForm
from .models import Expense, ExpenseCategory

VIEW, EXPENSE = "finance.view", "finance.expense.manage"

PERIODS = [
    ("today", "Today"),
    ("month", "This month"),
    ("last_month", "Last month"),
    ("year", "This year"),
    ("custom", "Custom"),
]


def _crumbs(*extra):
    return [{"label": "Finance", "url": reverse("finance:dashboard")}, *extra]


def _date(value, default):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def period(request) -> dict:
    """Period (?period=…&from=&to=) and branch scope (?scope=all) for finance pages."""
    today = timezone.localdate()
    key = request.GET.get("period", "month")
    if key == "today":
        first = last = today
    elif key == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        first = last.replace(day=1)
    elif key == "year":
        first, last = today.replace(month=1, day=1), today
    elif key == "custom":
        first = _date(request.GET.get("from"), today.replace(day=1))
        last = _date(request.GET.get("to"), today)
        if last < first:
            first, last = last, first
    else:
        key, first, last = "month", today.replace(day=1), today
    scope = "all" if request.GET.get("scope") == "all" else "branch"
    if scope == "all":
        # Only branches where this user also holds finance.view.
        branches = [
            b
            for b in accessible_branches(request.user)
            if VIEW in effective_permissions(request.user, b)
        ]
    else:
        branches = [request.branch]
    return {
        "period": key,
        "first": first,
        "last": last,
        "scope": scope,
        "branches": branches,
        "periods": PERIODS,
        "scope_label": "All branches" if scope == "all" else request.branch.name,
    }


def _csv(filename, header, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(response)
    w.writerow(header)
    w.writerows(rows)
    return response


# --- Dashboard and statements ------------------------------------------------------------------


@require_GET
@permission_required(VIEW, EXPENSE, any_of=True)
def dashboard(request):
    if not has_perm(request, VIEW):  # expense clerks land on their own screen
        return redirect("finance:expense-list")
    p = period(request)
    org, branches = request.organization, p["branches"]
    st = statements.statement(statements.pnl_rows(org, branches, p["first"], p["last"]))
    cash = statements.ledger(org, branches, p["first"], p["last"])
    expense_lines = sorted(
        ((k.removeprefix("Expense · "), sum(v.values(), ZERO)) for k, v in st.cost_lines.items()),
        key=lambda kv: -kv[1],
    )[:6]
    top = expense_lines[0][1] if expense_lines else 1
    return render(
        request,
        "finance/dashboard.html",
        {
            **p,
            "st": st,
            "segments": segments.SEGMENTS,
            "cash": cash,
            "outstanding": statements.outstanding(org, branches),
            "trend": statements.monthly_trend(org, branches),
            "cost_lines": [(k, v, round(v * 100 / top) if top else 0) for k, v in expense_lines],
            "doctors": statements.doctor_shares(org, branches, p["first"], p["last"])[:8],
            "can_expense": has_perm(request, EXPENSE),
        },
    )


@require_GET
@permission_required(VIEW)
def pnl(request):
    p = period(request)
    st = statements.statement(
        statements.pnl_rows(request.organization, p["branches"], p["first"], p["last"])
    )
    if request.GET.get("export") == "csv":
        audit.record(request, "report.export", summary=f"P&L {p['first']}–{p['last']}")
        header = ["Line"] + [lbl for _, lbl in segments.SEGMENTS] + ["Total"]

        def line(label, values):
            return [label] + [values[k] for k in segments.KEYS] + [sum(values.values(), ZERO)]

        rows = [line(k, v) for k, v in st.income_lines.items()]
        rows.append(line("Total income", st.income))
        rows += [line(k, v) for k, v in st.cost_lines.items()]
        rows.append(line("Total costs", st.costs))
        rows.append(line("Net profit", st.profit))
        return _csv(f"pnl-{p['first']}-{p['last']}.csv", header, rows)
    return render(
        request,
        "finance/pnl.html",
        {**p, "st": st, "segments": segments.SEGMENTS, "breadcrumbs": _crumbs({"label": "P&L"})},
    )


@require_GET
@permission_required(VIEW)
def ledger(request):
    p = period(request)
    segment = request.GET.get("segment", "")
    segment = segment if segment in segments.KEYS else ""
    method = request.GET.get("method", "")[:60]
    data = statements.ledger(
        request.organization, p["branches"], p["first"], p["last"], segment=segment, method=method
    )
    if request.GET.get("export") == "csv":
        audit.record(request, "report.export", summary=f"Ledger {p['first']}–{p['last']}")
        rows = [
            [
                timezone.localtime(r["entry"].when).strftime("%Y-%m-%d %H:%M"),
                r["entry"].kind,
                r["entry"].number,
                r["entry"].description,
                r["entry"].method,
                r["entry"].branch,
                r["amount"],
                r["running"],
            ]
            for r in data["rows"]
        ]
        return _csv(
            f"ledger-{segment or 'all'}-{p['first']}-{p['last']}.csv",
            ["When", "Type", "Number", "Detail", "Method", "Branch", "Amount", "Balance"],
            [["", "Opening balance", "", "", "", "", "", data["opening"]], *rows],
        )
    from apps.billing.services import active_methods

    return render(
        request,
        "finance/ledger.html",
        {
            **p,
            **data,
            "rows": data["rows"][::-1],
            "segment": segment,
            "keep": {k: v for k, v in {"segment": segment, "method": method}.items() if v},
            "segment_label": segments.LABELS.get(segment, "All segments"),
            "segments": segments.SEGMENTS,
            "method": method,
            "methods": active_methods(request.organization),
            "breadcrumbs": _crumbs({"label": "Ledger"}),
        },
    )


@require_GET
@permission_required(VIEW)
def ledger_doctors(request):
    """Doctors' ledger: what each doctor brought in, their share, what was paid out."""
    from apps.hr import payroll
    from apps.hr.commission import unsettled_total

    p = period(request)
    rows = statements.doctor_shares(request.organization, p["branches"], p["first"], p["last"])
    from apps.accounts.models import User

    users = {u.pk: u for u in User.objects.filter(pk__in=[r["user_id"] for r in rows])}
    for r in rows:
        u = users[r["user_id"]]
        r["balance"] = payroll.balance(u)
        r["unsettled"] = unsettled_total(u)
    return render(
        request,
        "finance/ledger_doctors.html",
        {
            **p,
            "rows": rows,
            "can_pay": has_perm(request, "hr.pay.manage"),
            "totals": {
                k: sum((r[k] for r in rows), ZERO) for k in ("income", "commission", "clinic")
            },
            "breadcrumbs": _crumbs({"label": "Doctors ledger"}),
        },
    )


# --- Expenses ----------------------------------------------------------------------------------


@require_GET
@permission_required(VIEW, EXPENSE, any_of=True)
def expense_list(request):
    p = period(request) if has_perm(request, VIEW) else {**period(request), "scope": "branch"}
    qs = Expense.objects.filter(
        organization=request.organization,
        branch__in=p["branches"] if p["scope"] == "all" else [request.branch],
        date__range=(p["first"], p["last"]),
    ).select_related("category", "method", "created_by", "branch")
    cat = request.GET.get("category", "")
    if cat.isdigit():
        qs = qs.filter(category_id=cat)
    seg = request.GET.get("segment", "")
    if seg in segments.KEYS:
        qs = qs.filter(segment=seg)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(payee__icontains=q) | Q(note__icontains=q))
    total = qs.filter(voided_at__isnull=True).aggregate(s=Sum("amount"))["s"] or ZERO
    services.ensure_categories(request.organization)
    return render(
        request,
        "finance/expense_list.html",
        {
            **p,
            "page_obj": paginate(request, qs),
            "total": total,
            "categories": scoped(ExpenseCategory, request),
            "category": cat,
            "segment": seg,
            "segments": segments.SEGMENTS,
            "q": q,
            "can_expense": has_perm(request, EXPENSE),
            "void_form": VoidForm(),
            "breadcrumbs": _crumbs({"label": "Expenses"}),
        },
    )


@permission_required(EXPENSE)
def expense_create(request):
    services.ensure_categories(request.organization)
    form = ExpenseForm(
        request.POST or None, request=request, initial={"idempotency_key": uuid.uuid4().hex}
    )
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            exp = services.record_expense(
                request=request,
                category=c["category"],
                amount=c["amount"],
                method=c["method"],
                day=c["date"],
                segment=c["segment"],
                payee=c["payee"],
                reference=c["reference"],
                note=c["note"],
                idempotency_key=c["idempotency_key"],
            )
        except services.FinanceError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "expense.create",
                exp,
                summary=f"{exp.number} {exp.category} {exp.amount} ({exp.get_segment_display()})",
            )
            messages.success(request, f"Expense {exp.number} recorded.")
            if request.POST.get("another"):
                return redirect("finance:expense-create")
            return redirect("finance:expense-list")
    return render(
        request,
        "finance/expense_form.html",
        {
            "form": form,
            "breadcrumbs": _crumbs(
                {"label": "Expenses", "url": reverse("finance:expense-list")},
                {"label": "New expense"},
            ),
        },
    )


@require_POST
@permission_required(EXPENSE)
def expense_void(request, pk):
    exp = get_scoped_or_404(Expense, request, pk=pk)
    form = VoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A reason is required to void an expense.")
    else:
        try:
            services.void_expense(exp, request=request, reason=form.cleaned_data["reason"])
        except services.FinanceError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "expense.void",
                exp,
                summary=f"{exp.number} {exp.amount}: {exp.void_reason}",
            )
            messages.success(request, f"{exp.number} voided.")
    return redirect("finance:expense-list")


@permission_required(EXPENSE)
def category_list(request):
    services.ensure_categories(request.organization)
    form = CategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        name = form.cleaned_data["name"].strip()
        if scoped(ExpenseCategory, request).filter(name__iexact=name).exists():
            form.add_error("name", "A category with this name exists.")
        else:
            cat = ExpenseCategory.objects.create(
                organization=request.organization, name=name, segment=form.cleaned_data["segment"]
            )
            audit.record(request, "expense.category_create", cat, summary=cat.name)
            messages.success(request, f"Category “{cat.name}” added.")
            return redirect("finance:category-list")
    return render(
        request,
        "finance/categories.html",
        {
            "form": form,
            "categories": scoped(ExpenseCategory, request),
            "breadcrumbs": _crumbs(
                {"label": "Expenses", "url": reverse("finance:expense-list")},
                {"label": "Categories"},
            ),
        },
    )


@require_POST
@permission_required(EXPENSE)
def category_toggle(request, pk):
    cat = get_scoped_or_404(ExpenseCategory, request, pk=pk)
    cat.is_active = not cat.is_active
    cat.save(update_fields=["is_active", "updated_at"])
    audit.record(
        request, "expense.category_toggle", cat, summary=f"{cat.name} active={cat.is_active}"
    )
    return redirect("finance:category-list")


# --- Supplier payments -------------------------------------------------------------------------


@permission_required(EXPENSE)
def supplier_pay(request):
    from apps.pharmacy.models import VendorLedgerEntry
    from apps.pharmacy.purchasing import vendors_with_balance

    initial = {"idempotency_key": uuid.uuid4().hex}
    if request.GET.get("vendor", "").isdigit():
        initial["vendor"] = request.GET["vendor"]
    form = SupplierPaymentForm(request.POST or None, request=request, initial=initial)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            entry = services.pay_supplier(
                request=request,
                vendor=c["vendor"],
                amount=c["amount"],
                method=c["method"],
                reference=c["reference"],
                note=c["note"],
                idempotency_key=c["idempotency_key"],
            )
        except services.FinanceError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "supplier_payment.create",
                entry,
                summary=f"{entry.vendor}: {-entry.amount} by {entry.method}",
            )
            messages.success(request, f"Paid {-entry.amount:,.2f} to {entry.vendor}.")
            return redirect("finance:supplier-pay")
    return render(
        request,
        "finance/supplier_pay.html",
        {
            "form": form,
            "vendors": vendors_with_balance(request.organization).filter(is_active=True),
            "recent": VendorLedgerEntry.objects.filter(
                organization=request.organization,
                branch=request.branch,
                kind=VendorLedgerEntry.Kind.PAYMENT,
            ).select_related("vendor", "method")[:15],
            "breadcrumbs": _crumbs({"label": "Pay suppliers"}),
        },
    )
