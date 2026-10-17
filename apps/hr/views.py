"""Attendance & pay: self check-in, attendance register, pay profiles, payroll, staff ledger."""

import uuid
from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import has_perm, permission_required
from apps.accounts.models import User
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404, paginate, scoped

from . import attendance, commission, payroll
from .forms import (
    AdjustmentsForm,
    CommissionRuleForm,
    OpeningBalanceForm,
    PayProfileForm,
    PayrollCreateForm,
    PayStaffForm,
)
from .models import (
    AttendanceRecord,
    CommissionEarning,
    CommissionRule,
    PayProfile,
    PayrollRun,
    Payslip,
    StaffLedgerEntry,
)

SELF, ATTEND, PAY = "hr.self", "hr.attendance.manage", "hr.pay.manage"


def _crumbs(*extra):
    return [{"label": "Attendance & pay", "url": reverse("hr:home")}, *extra]


def _parse_day(value) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return timezone.localdate()


def _parse_month(value) -> tuple[int, int]:
    try:
        y, m = (int(x) for x in value.split("-"))
        if 1 <= m <= 12:
            return y, m
    except (AttributeError, ValueError):
        pass
    today = timezone.localdate()
    return today.year, today.month


def _org_user(request, user_id) -> User:
    return get_object_or_404(User, pk=user_id, organization=request.organization)


# --- Home and self service ---------------------------------------------------------------------


@require_GET
@permission_required(SELF, ATTEND, PAY, any_of=True)
def home(request):
    today = timezone.localdate()
    ctx = {"today": today, "breadcrumbs": None}
    if has_perm(request, SELF):
        first, last = attendance.month_bounds(today.year, today.month)
        mine = AttendanceRecord.objects.filter(
            organization=request.organization, user=request.user, date__range=(first, last)
        )
        profile = attendance.profile_for(request.user)
        ctx.update(
            {
                "my_today": attendance.today_record(request.user, request.organization),
                "my_month": attendance.summarize(mine),
                "my_records": mine.order_by("-date")[:10],
                "my_profile": profile,
                "my_unsettled": commission.unsettled_total(request.user),
                "my_balance": payroll.balance(request.user),
            }
        )
    if has_perm(request, ATTEND):
        staff = list(attendance.branch_staff(request.branch))
        marked = AttendanceRecord.objects.filter(
            organization=request.organization, date=today, user__in=staff
        )
        s = attendance.summarize(marked)
        ctx["register"] = {"staff": len(staff), "marked": marked.count(), **s}
    if has_perm(request, PAY):
        ctx["last_run"] = scoped(PayrollRun, request).first()
    return render(request, "hr/home.html", ctx)


@require_POST
@permission_required(SELF)
def attendance_check(request):
    action = request.POST.get("action")
    try:
        if action == "in":
            rec = attendance.check_in(request=request)
            msg = f"Checked in at {timezone.localtime(rec.check_in):%H:%M}" + (
                " (late)." if rec.status == AttendanceRecord.Status.LATE else "."
            )
        elif action == "out":
            rec = attendance.check_out(request=request)
            msg = f"Checked out at {timezone.localtime(rec.check_out):%H:%M}."
        else:
            raise attendance.AttendanceError("Unknown action.")
    except attendance.AttendanceError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, f"attendance.check_{action}", rec, summary=str(rec.date))
        messages.success(request, msg)
    return redirect("hr:home")


@require_GET
@permission_required(SELF)
def earnings(request):
    """A doctor's own commission and pay history."""
    rows = CommissionEarning.objects.filter(user=request.user).select_related(
        "invoice", "invoice_item"
    )
    return render(
        request,
        "hr/earnings.html",
        {
            "page_obj": paginate(request, rows),
            "unsettled": commission.unsettled_total(request.user),
            "balance": payroll.balance(request.user),
            "payslips": Payslip.objects.filter(
                user=request.user, run__status=PayrollRun.Status.FINALIZED
            ).select_related("run")[:12],
            "ledger": StaffLedgerEntry.objects.filter(user=request.user).order_by("-created_at")[
                :20
            ],
            "breadcrumbs": _crumbs({"label": "My earnings"}),
        },
    )


# --- Attendance admin --------------------------------------------------------------------------


@permission_required(ATTEND)
def attendance_register(request):
    day = _parse_day(request.POST.get("date") or request.GET.get("date"))
    staff = list(attendance.branch_staff(request.branch))
    if request.method == "POST":
        changed, errors = 0, []
        for u in staff:
            status = request.POST.get(f"status_{u.pk}", "")
            if not status:
                continue
            try:
                rec, did = attendance.mark(
                    request=request,
                    user=u,
                    day=day,
                    status=status,
                    note=request.POST.get(f"note_{u.pk}", ""),
                )
            except attendance.AttendanceError as err:
                errors.append(f"{u}: {err.message}")
                continue
            if did:
                changed += 1
                audit.record(request, "attendance.mark", rec, summary=f"{u} {day} {rec.status}")
        for e in dict.fromkeys(errors):
            messages.error(request, e)
        if changed:
            messages.success(request, f"Attendance saved for {changed} staff on {day:%d %b %Y}.")
        return redirect(f"{reverse('hr:attendance-register')}?date={day.isoformat()}")
    records = {
        r.user_id: r
        for r in AttendanceRecord.objects.filter(
            organization=request.organization, date=day, user__in=staff
        ).select_related("branch")
    }
    return render(
        request,
        "hr/attendance_register.html",
        {
            "day": day,
            "rows": [(u, records.get(u.pk)) for u in staff],
            "statuses": AttendanceRecord.Status.choices,
            "locked": attendance.is_locked(request.branch, day),
            "is_future": day > timezone.localdate(),
            "summary": attendance.summarize(records.values()),
            "breadcrumbs": _crumbs({"label": "Daily register"}),
        },
    )


@require_GET
@permission_required(ATTEND)
def attendance_month(request):
    year, month = _parse_month(request.GET.get("month", ""))
    sheet = attendance.month_sheet(request.branch, year, month)
    return render(
        request,
        "hr/attendance_month.html",
        {
            **sheet,
            "month_value": f"{year}-{month:02d}",
            "month_label": date(year, month, 1).strftime("%B %Y"),
            "legend": [
                (AttendanceRecord.SHORT[v], lbl) for v, lbl in AttendanceRecord.Status.choices
            ],
            "breadcrumbs": _crumbs({"label": "Monthly sheet"}),
        },
    )


# --- Pay profiles ------------------------------------------------------------------------------


@require_GET
@permission_required(PAY)
def pay_list(request):
    staff = list(attendance.branch_staff(request.branch))
    profiles = {
        p.user_id: p for p in PayProfile.objects.filter(user__in=staff).select_related("branch")
    }
    balances = dict(
        StaffLedgerEntry.objects.filter(user__in=staff)
        .values("user")
        .annotate(s=Sum("amount"))
        .values_list("user", "s")
    )
    unsettled = dict(
        CommissionEarning.objects.filter(user__in=staff, payslip__isnull=True)
        .values("user")
        .annotate(s=Sum("amount"))
        .values_list("user", "s")
    )
    rows = [
        {
            "user": u,
            "profile": profiles.get(u.pk),
            "balance": balances.get(u.pk) or 0,
            "unsettled": unsettled.get(u.pk) or 0,
        }
        for u in staff
    ]
    return render(
        request,
        "hr/pay_list.html",
        {"rows": rows, "breadcrumbs": _crumbs({"label": "Staff pay"})},
    )


def _profile_here(request, user):
    """The user's pay profile, or None. A profile paid from another branch is read-only here."""
    profile = PayProfile.objects.filter(user=user).select_related("branch").first()
    if profile and profile.branch_id != request.branch.pk:
        raise PermissionDenied
    return profile


@permission_required(PAY)
def pay_profile(request, user_id):
    user = _org_user(request, user_id)
    profile = _profile_here(request, user)
    creating = profile is None
    form = PayProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        before = {} if creating else {f: str(getattr(profile, f)) for f in form.fields}
        obj = form.save(commit=False)
        if creating:
            obj.organization, obj.branch, obj.user = request.organization, request.branch, user
        obj.save()
        after = {f: str(getattr(obj, f)) for f in form.fields}
        audit.record(
            request,
            "pay_profile.create" if creating else "pay_profile.update",
            obj,
            changes={k: [before.get(k), v] for k, v in after.items() if before.get(k) != v},
            summary=f"{user}: {obj.get_pay_type_display()}",
        )
        messages.success(request, f"Pay profile for {user} saved.")
        return redirect("hr:pay-profile", user_id=user.pk)
    return render(
        request,
        "hr/pay_profile.html",
        {
            "staff": user,
            "profile": profile,
            "form": form,
            "rule_form": CommissionRuleForm(request=request) if profile else None,
            "rules": profile.rules.select_related("service") if profile else [],
            "breadcrumbs": _crumbs(
                {"label": "Staff pay", "url": reverse("hr:pay-list")}, {"label": str(user)}
            ),
        },
    )


@require_POST
@permission_required(PAY)
def pay_rule_add(request, user_id):
    user = _org_user(request, user_id)
    profile = _profile_here(request, user)
    if profile is None:
        raise PermissionDenied
    form = CommissionRuleForm(request.POST, request=request)
    if form.is_valid():
        rule, created = CommissionRule.objects.update_or_create(
            profile=profile,
            service=form.cleaned_data["service"],
            defaults={
                "percent": form.cleaned_data["percent"],
                "fixed_amount": form.cleaned_data["fixed_amount"],
            },
        )
        audit.record(request, "pay_profile.rule_set", profile, summary=f"{user}: {rule}")
        messages.success(request, f"Commission for {rule.service.name} saved.")
    else:
        messages.error(request, "Check the commission rule.")
    return redirect("hr:pay-profile", user_id=user.pk)


@require_POST
@permission_required(PAY)
def pay_rule_remove(request, user_id, rule_pk):
    user = _org_user(request, user_id)
    profile = _profile_here(request, user)
    rule = get_object_or_404(CommissionRule, pk=rule_pk, profile=profile)
    audit.record(request, "pay_profile.rule_remove", profile, summary=f"{user}: {rule}")
    rule.delete()  # a rate setting, not a money record; earned rows keep their own rate
    messages.success(request, "Commission rule removed.")
    return redirect("hr:pay-profile", user_id=user.pk)


# --- Staff ledger ------------------------------------------------------------------------------


@require_GET
@permission_required(PAY)
def staff_ledger(request, user_id):
    user = _org_user(request, user_id)
    entries = list(
        StaffLedgerEntry.objects.filter(user=user).select_related("method", "created_by", "branch")
    )
    running = 0
    for e in entries:
        running += e.amount
        e.running = running
    return render(
        request,
        "hr/staff_ledger.html",
        {
            "staff": user,
            "entries": list(reversed(entries)),
            "balance": running,
            "unsettled": commission.unsettled_total(user),
            "pay_form": PayStaffForm(
                request=request, initial={"idempotency_key": uuid.uuid4().hex}
            ),
            "opening_form": OpeningBalanceForm(),
            "has_opening": any(e.kind == StaffLedgerEntry.Kind.OPENING for e in entries),
            "breadcrumbs": _crumbs(
                {"label": "Staff pay", "url": reverse("hr:pay-list")}, {"label": f"{user} ledger"}
            ),
        },
    )


@require_POST
@permission_required(PAY)
def staff_pay(request, user_id):
    user = _org_user(request, user_id)
    form = PayStaffForm(request.POST, request=request)
    if form.is_valid():
        c = form.cleaned_data
        try:
            entry = payroll.pay_staff(
                request=request,
                user=user,
                amount=c["amount"],
                method=c["method"],
                kind=c["kind"],
                reference=c["reference"],
                note=c["note"],
                idempotency_key=c["idempotency_key"],
            )
        except payroll.PayrollError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "staff_pay.payout",
                entry,
                summary=f"{user}: {entry.get_kind_display()} {-entry.amount} by {entry.method}",
            )
            messages.success(request, f"Paid {-entry.amount:,.2f} to {user}.")
    else:
        messages.error(request, "Check the payment details.")
    return redirect("hr:staff-ledger", user_id=user.pk)


@require_POST
@permission_required(PAY)
def staff_opening(request, user_id):
    user = _org_user(request, user_id)
    form = OpeningBalanceForm(request.POST)
    if form.is_valid():
        try:
            entry = payroll.post_opening(
                request=request,
                user=user,
                amount=form.cleaned_data["amount"],
                note=form.cleaned_data["note"],
            )
        except payroll.PayrollError as err:
            messages.error(request, err.message)
        else:
            audit.record(request, "staff_pay.opening", entry, summary=f"{user}: {entry.amount}")
            messages.success(request, "Opening balance recorded.")
    return redirect("hr:staff-ledger", user_id=user.pk)


# --- Payroll -----------------------------------------------------------------------------------


@permission_required(PAY)
def payroll_list(request):
    today = timezone.localdate()
    form = PayrollCreateForm(request.POST or None, initial={"month": f"{today:%Y-%m}"})
    if request.method == "POST" and form.is_valid():
        year, month = form.cleaned_data["month"]
        try:
            run = payroll.create_run(request=request, year=year, month=month)
        except payroll.PayrollError as err:
            form.add_error("month", err.message)
        else:
            audit.record(request, "payroll.create", run, summary=run.label)
            messages.success(request, f"Payroll for {run.label} drafted.")
            return redirect("hr:payroll-detail", pk=run.pk)
    runs = scoped(PayrollRun, request).annotate(total=Sum("payslips__net_pay"))
    return render(
        request,
        "hr/payroll_list.html",
        {"runs": runs, "form": form, "breadcrumbs": _crumbs({"label": "Payroll"})},
    )


@require_GET
@permission_required(PAY)
def payroll_detail(request, pk):
    run = get_scoped_or_404(PayrollRun, request, pk=pk)
    slips = run.payslips.select_related("user")
    totals = slips.aggregate(
        base=Sum("base_pay"),
        absence=Sum("absence_deduction"),
        commission=Sum("commission"),
        bonus=Sum("bonus"),
        deduction=Sum("deduction"),
        net=Sum("net_pay"),
    )
    return render(
        request,
        "hr/payroll_detail.html",
        {
            "run": run,
            "slips": slips,
            "totals": totals,
            "breadcrumbs": _crumbs(
                {"label": "Payroll", "url": reverse("hr:payroll-list")}, {"label": run.label}
            ),
        },
    )


@require_POST
@permission_required(PAY)
def payroll_recalculate(request, pk):
    run = get_scoped_or_404(PayrollRun, request, pk=pk)
    try:
        payroll.recalculate(run)
    except payroll.PayrollError as err:
        messages.error(request, err.message)
    else:
        messages.success(request, "Payslips recalculated from attendance and commission.")
    return redirect("hr:payroll-detail", pk=pk)


@require_POST
@permission_required(PAY)
def payroll_finalize(request, pk):
    run = get_scoped_or_404(PayrollRun, request, pk=pk)
    try:
        run = payroll.finalize(run, request=request)
    except payroll.PayrollError as err:
        messages.error(request, err.message)
    else:
        total = run.payslips.aggregate(s=Sum("net_pay"))["s"] or 0
        audit.record(request, "payroll.finalize", run, summary=f"{run.label} net {total}")
        messages.success(request, f"Payroll for {run.label} finalized and posted to staff ledgers.")
    return redirect("hr:payroll-detail", pk=pk)


@require_POST
@permission_required(PAY)
def payslip_adjust(request, pk):
    slip = get_object_or_404(
        Payslip.objects.select_related("run"),
        pk=pk,
        run__organization=request.organization,
        run__branch=request.branch,
    )
    form = AdjustmentsForm(request.POST)
    if form.is_valid():
        c = form.cleaned_data
        try:
            payroll.set_adjustments(
                slip, bonus=c["bonus"], deduction=c["deduction"], note=c["note"]
            )
        except payroll.PayrollError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "payroll.adjust",
                slip,
                summary=f"{slip.user}: bonus {slip.bonus}, deduction {slip.deduction}",
            )
            messages.success(request, f"Payslip for {slip.user} updated.")
    else:
        messages.error(request, "Check the bonus and deduction.")
    return redirect("hr:payroll-detail", pk=slip.run_id)


@require_GET
@permission_required(SELF, PAY, any_of=True)
def payslip_print(request, pk):
    slip = get_object_or_404(
        Payslip.objects.select_related("run__branch", "user"),
        pk=pk,
        run__organization=request.organization,
    )
    own = slip.user_id == request.user.pk and slip.run.status == PayrollRun.Status.FINALIZED
    manager = has_perm(request, PAY) and slip.run.branch_id == request.branch.pk
    if not (own or manager):
        raise PermissionDenied
    return render(
        request,
        "hr/payslip_print.html",
        {"slip": slip, "run": slip.run, "earnings": slip.earnings.select_related("invoice")},
    )
