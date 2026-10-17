"""Monthly payroll and the staff ledger (Phase 35).

Absence deduction only counts days explicitly marked absent (half for a half day); unmarked
days are not deducted, so clinics that do not mark attendance still get full salaries.
Daily wages pay for days marked present, late (full) or half day (half).
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import ZERO, to_money
from apps.core.db import locked

from . import attendance, commission
from .models import AttendanceRecord, PayProfile, PayrollRun, Payslip, StaffLedgerEntry

Kind = StaffLedgerEntry.Kind


class PayrollError(ValidationError):
    pass


def _period_end(run) -> datetime:
    _, last = attendance.month_bounds(run.year, run.month)
    return timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min))


def _calculate(slip: Payslip, profile: PayProfile) -> None:
    run = slip.run
    first, last = attendance.month_bounds(run.year, run.month)
    records = AttendanceRecord.objects.filter(
        organization_id=run.organization_id, user=slip.user, date__range=(first, last)
    )
    s = attendance.summarize(records)
    slip.pay_type = profile.pay_type
    slip.monthly_salary = profile.monthly_salary
    slip.daily_rate = profile.daily_rate
    slip.working_days = attendance.working_days(run.year, run.month, profile.weekly_off)
    slip.present_days = Decimal(str(s["present_days"]))
    slip.absent_days = Decimal(str(s["absent_days"]))
    slip.leave_days = s["leave"]
    slip.late_count = s["late"]
    slip.absence_deduction = ZERO
    if profile.pay_type == PayProfile.PayType.MONTHLY:
        slip.base_pay = profile.monthly_salary
        if profile.deduct_absences and slip.working_days:
            per_day = profile.monthly_salary / slip.working_days
            slip.absence_deduction = min(slip.base_pay, to_money(per_day * slip.absent_days))
    elif profile.pay_type == PayProfile.PayType.DAILY:
        slip.base_pay = to_money(profile.daily_rate * slip.present_days)
    else:
        slip.base_pay = ZERO
    slip.commission = commission.unsettled_total(slip.user, before=_period_end(run))
    slip.net_pay = (
        slip.base_pay - slip.absence_deduction + slip.commission + slip.bonus - slip.deduction
    )


def profiles_for(branch):
    return PayProfile.objects.filter(
        branch=branch, is_active=True, user__is_active=True
    ).select_related("user")


@transaction.atomic
def create_run(*, request, year: int, month: int) -> PayrollRun:
    first, _ = attendance.month_bounds(year, month)
    if first > timezone.localdate():
        raise PayrollError("Payroll cannot be run for a future month.")
    try:
        with transaction.atomic():
            run = PayrollRun.objects.create(
                organization=request.organization,
                branch=request.branch,
                year=year,
                month=month,
                created_by=request.user,
            )
    except IntegrityError as err:
        raise PayrollError(f"Payroll for {first:%B %Y} already exists in this branch.") from err
    return recalculate(run)


@transaction.atomic
def recalculate(run) -> PayrollRun:
    """Refresh every draft payslip from attendance and commission. Bonus and other deduction
    entered by the admin are kept. Staff with a pay profile here get a payslip."""
    run = locked(run)
    if not run.is_draft:
        raise PayrollError("A finalized payroll cannot be recalculated.")
    profiles = list(profiles_for(run.branch))
    # Drafts hold no money: drop payslips of staff who no longer have a pay profile here.
    run.payslips.exclude(user__in=[p.user_id for p in profiles]).delete()
    for profile in profiles:
        slip = Payslip.objects.filter(run=run, user=profile.user).first() or Payslip(
            run=run, user=profile.user
        )
        _calculate(slip, profile)
        slip.save()
    return run


@transaction.atomic
def set_adjustments(slip, *, bonus, deduction, note) -> Payslip:
    run = locked(slip.run)
    if not run.is_draft:
        raise PayrollError("This payroll is finalized.")
    bonus, deduction = to_money(bonus), to_money(deduction)
    if bonus < 0 or deduction < 0:
        raise PayrollError("Bonus and deduction must be zero or more.")
    if (bonus or deduction) and not note.strip():
        raise PayrollError("Give a reason for the bonus or deduction.")
    slip.bonus, slip.deduction, slip.adjustment_note = bonus, deduction, note.strip()[:200]
    slip.net_pay = (
        slip.base_pay - slip.absence_deduction + slip.commission + slip.bonus - slip.deduction
    )
    slip.save()
    return slip


@transaction.atomic
def finalize(run, *, request) -> PayrollRun:
    """Recalculate, then post every payslip to the staff ledger and settle commission rows."""
    run = recalculate(run)
    end = _period_end(run)
    for slip in run.payslips.select_related("user"):
        common = {
            "organization_id": run.organization_id,
            "branch_id": run.branch_id,
            "user": slip.user,
            "payslip": slip,
            "created_by": request.user,
            "note": f"Payroll {run.label}",
        }
        rows = [
            (Kind.SALARY, slip.salary_pay),
            (Kind.COMMISSION, slip.commission),
            (Kind.BONUS, slip.bonus),
            (Kind.DEDUCTION, -slip.deduction),
        ]
        for kind, amount in rows:
            if amount:
                StaffLedgerEntry.objects.create(kind=kind, amount=amount, **common)
        commission.unsettled(slip.user, before=end).update(payslip=slip)
    run.status = PayrollRun.Status.FINALIZED
    run.finalized_at = timezone.now()
    run.finalized_by = request.user
    run.save(update_fields=["status", "finalized_at", "finalized_by", "updated_at"])
    return run


# --- Staff ledger ------------------------------------------------------------------------------


def balance(user) -> Decimal:
    return StaffLedgerEntry.objects.filter(user=user).aggregate(s=Sum("amount"))["s"] or ZERO


@transaction.atomic
def pay_staff(
    *, request, user, amount, method, kind=Kind.PAYMENT, reference="", note="", idempotency_key=""
) -> StaffLedgerEntry:
    """Money handed to a staff member: a salary payout or an advance. Both reduce what the
    hospital owes; an advance may take the balance below zero (recovered from later pay)."""
    if idempotency_key:
        done = StaffLedgerEntry.objects.filter(
            organization=request.organization, idempotency_key=idempotency_key
        ).first()
        if done:
            return done
    if user.organization_id != request.organization.pk:
        raise PayrollError("That staff member is not in this hospital.")
    if kind not in StaffLedgerEntry.PAYOUT_KINDS:
        raise PayrollError("Choose payment or advance.")
    if method is None or method.organization_id != request.organization.pk or not method.is_active:
        raise PayrollError("Choose an active payment method.")
    amount = to_money(amount)
    if amount <= ZERO:
        raise PayrollError("Enter an amount greater than zero.")
    if method.requires_reference and not reference.strip():
        raise PayrollError(f"{method.name} payments need a reference number.")
    try:
        with transaction.atomic():
            return StaffLedgerEntry.objects.create(
                organization=request.organization,
                branch=request.branch,
                user=user,
                kind=kind,
                amount=-amount,
                method=method,
                reference=reference.strip()[:80],
                note=note.strip()[:200],
                created_by=request.user,
                idempotency_key=idempotency_key[:40],
            )
    except IntegrityError as err:
        raise PayrollError("This payment was already recorded.") from err


@transaction.atomic
def post_opening(*, request, user, amount, note="") -> StaffLedgerEntry:
    if StaffLedgerEntry.objects.filter(user=user, kind=Kind.OPENING).exists():
        raise PayrollError("An opening balance is already recorded.")
    return StaffLedgerEntry.objects.create(
        organization=request.organization,
        branch=request.branch,
        user=user,
        kind=Kind.OPENING,
        amount=to_money(amount),
        note=note.strip()[:200] or "Opening balance",
        created_by=request.user,
    )
