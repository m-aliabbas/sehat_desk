from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import InvoiceItem
from apps.hr import attendance, commission, payroll
from apps.hr.models import (
    AttendanceRecord,
    CommissionEarning,
    CommissionRule,
    PayProfile,
    PayrollRun,
    StaffLedgerEntry,
)
from apps.masterdata.models import Service, ServiceCategory

D = Decimal
Status = AttendanceRecord.Status


def _last_month():
    first_this = timezone.localdate().replace(day=1)
    last = first_this - timedelta(days=1)
    return last.year, last.month


@pytest.fixture
def admin_req(make_request, admin_user):
    return make_request(admin_user)


@pytest.fixture
def doctor_profile(org, branch, doctor):
    return PayProfile.objects.create(
        organization=org,
        branch=branch,
        user=doctor.user,
        pay_type=PayProfile.PayType.COMMISSION,
        consultation_percent=D("50"),
        procedure_percent=D("40"),
        lab_referral_percent=D("10"),
    )


@pytest.fixture
def procedure(org):
    cat = ServiceCategory.objects.create(organization=org, name="Procedures")
    return Service.objects.create(
        organization=org,
        category=cat,
        code="RCT",
        name="Root canal",
        price=D("1500"),
        is_procedure=True,
    )


def _bill(
    req, patient, doctor_user, *, kind=InvoiceItem.Kind.PROCEDURE, price="1500", service=None
):
    inv = billing.create_invoice(request=req, patient=patient)
    billing.add_item(
        inv,
        request=req,
        kind=kind,
        description="Line",
        unit_price=D(price),
        performed_by=doctor_user,
        service=service,
    )
    return billing.issue(inv, request=req)


# --- Attendance --------------------------------------------------------------------------------


def test_check_in_and_out(make_request, make_user):
    req = make_request(make_user("reception"))
    rec = attendance.check_in(request=req)
    assert rec.check_in and rec.self_marked
    with pytest.raises(attendance.AttendanceError, match="already checked in"):
        attendance.check_in(request=req)
    rec = attendance.check_out(request=req)
    assert rec.check_out
    with pytest.raises(attendance.AttendanceError, match="already checked out"):
        attendance.check_out(request=req)


def test_check_out_before_check_in_refused(make_request, make_user):
    with pytest.raises(attendance.AttendanceError, match="Check in first"):
        attendance.check_out(request=make_request(make_user("nurse")))


@pytest.mark.parametrize(
    "start, expected", [(time(0, 0), Status.LATE), (time(23, 59), Status.PRESENT)]
)
def test_late_detection_uses_work_start(make_request, make_user, org, branch, start, expected):
    user = make_user("lab")
    PayProfile.objects.create(
        organization=org, branch=branch, user=user, work_start=start, late_grace_minutes=0
    )
    if timezone.localtime().time() >= time(23, 59):
        pytest.skip("too close to midnight")
    assert attendance.check_in(request=make_request(user)).status == expected


def test_admin_marks_register_and_future_is_refused(admin_req, make_user):
    nurse = make_user("nurse")
    rec, changed = attendance.mark(
        request=admin_req, user=nurse, day=timezone.localdate(), status=Status.ABSENT
    )
    assert changed and rec.status == Status.ABSENT
    _, changed = attendance.mark(
        request=admin_req, user=nurse, day=timezone.localdate(), status=Status.ABSENT
    )
    assert not changed
    with pytest.raises(attendance.AttendanceError, match="future"):
        attendance.mark(
            request=admin_req,
            user=nurse,
            day=timezone.localdate() + timedelta(days=1),
            status=Status.PRESENT,
        )


def test_register_and_month_views(admin_client, make_user):
    nurse = make_user("nurse")
    url = reverse("hr:attendance-register")
    today = timezone.localdate().isoformat()
    assert admin_client.get(url).status_code == 200
    admin_client.post(url, {"date": today, f"status_{nurse.pk}": "late", f"note_{nurse.pk}": "bus"})
    rec = AttendanceRecord.objects.get(user=nurse)
    assert rec.status == Status.LATE and rec.note == "bus"
    page = admin_client.get(reverse("hr:attendance-month"))
    assert page.status_code == 200 and str(nurse).encode() in page.content


def test_self_check_in_view(client, make_user):
    user = make_user("pharmacy")
    client.force_login(user)
    assert client.get(reverse("hr:home")).status_code == 200
    client.post(reverse("hr:attendance-check"), {"action": "in"})
    assert AttendanceRecord.objects.filter(user=user, check_in__isnull=False).exists()


# --- Commission --------------------------------------------------------------------------------


def test_commission_earned_only_when_fully_paid(admin_req, patient, doctor, doctor_profile, cash):
    inv = _bill(admin_req, patient, doctor.user)
    billing.receive_payment(inv, request=admin_req, method=cash, amount=D("500"))
    assert not CommissionEarning.objects.exists()
    billing.receive_payment(inv, request=admin_req, method=cash, amount=D("1000"))
    e = CommissionEarning.objects.get()
    assert e.amount == D("600.00") and e.percent == D("40") and e.segment == "doctors"
    assert e.clinic_share == D("900.00")
    commission.sync_invoice(inv)  # idempotent
    assert CommissionEarning.objects.count() == 1


def test_refund_reverses_proportionally(admin_req, patient, doctor, doctor_profile, cash):
    inv = _bill(admin_req, patient, doctor.user)
    pay = billing.receive_payment(inv, request=admin_req, method=cash, amount=D("1500"))
    billing.refund(pay, request=admin_req, amount=D("750"), reason="Half not done")
    assert commission.unsettled_total(doctor.user) == D("300.00")
    billing.refund(pay, request=admin_req, amount=D("750"), reason="Rest")
    assert commission.unsettled_total(doctor.user) == D("0.00")
    assert CommissionEarning.objects.filter(kind="adjustment").count() == 2


def test_rate_change_does_not_alter_earned_rows(admin_req, patient, doctor, doctor_profile, cash):
    inv = _bill(admin_req, patient, doctor.user)
    pay = billing.receive_payment(inv, request=admin_req, method=cash, amount=D("1500"))
    doctor_profile.procedure_percent = D("10")
    doctor_profile.save()
    billing.refund(pay, request=admin_req, amount=D("1500"), reason="Cancelled")
    # Reversal uses the 40% the line was earned at, so the total nets to zero.
    assert commission.unsettled_total(doctor.user) == D("0.00")


def test_per_procedure_rule_and_kind_rates(
    admin_req, patient, doctor, doctor_profile, cash, procedure
):
    CommissionRule.objects.create(
        profile=doctor_profile, service=procedure, percent=D("20"), fixed_amount=D("100")
    )
    inv = _bill(admin_req, patient, doctor.user, service=procedure)
    billing.receive_payment(inv, request=admin_req, method=cash, amount=D("1500"))
    lab = _bill(admin_req, patient, doctor.user, kind=InvoiceItem.Kind.LAB, price="1000")
    billing.receive_payment(lab, request=admin_req, method=cash, amount=D("1000"))
    amounts = sorted(CommissionEarning.objects.values_list("amount", flat=True))
    assert amounts == [D("100.00"), D("400.00")]  # lab 10%; rule 20% + 100 fixed


def test_no_profile_no_commission(admin_req, patient, doctor, cash):
    inv = _bill(admin_req, patient, doctor.user)
    billing.receive_payment(inv, request=admin_req, method=cash, amount=D("1500"))
    assert not CommissionEarning.objects.exists()


def test_invoice_discount_is_shared(admin_req, patient, doctor, doctor_profile, cash):
    inv = billing.create_invoice(request=admin_req, patient=patient)
    billing.add_item(
        inv,
        request=admin_req,
        kind=InvoiceItem.Kind.PROCEDURE,
        description="P",
        unit_price=D("1000"),
        performed_by=doctor.user,
    )
    billing.add_item(
        inv, request=admin_req, kind=InvoiceItem.Kind.MISC, description="M", unit_price=D("1000")
    )
    billing.set_invoice_discount(inv, request=admin_req, amount=D("200"), reason="Staff")
    inv.refresh_from_db()
    billing.receive_payment(inv, request=admin_req, method=cash, amount=inv.grand_total)
    e = CommissionEarning.objects.get()
    assert e.basis == D("900.00") and e.amount == D("360.00")


# --- Payroll -----------------------------------------------------------------------------------


def test_monthly_payroll_with_absence_commission_and_finalize(
    admin_req, org, branch, make_user, patient, doctor, doctor_profile, cash
):
    year, month = _last_month()
    clerk = make_user("reception")
    PayProfile.objects.create(
        organization=org, branch=branch, user=clerk, monthly_salary=D("30000"), weekly_off=6
    )
    first = date(year, month, 1)
    days = [
        first + timedelta(days=i) for i in range(10) if (first + timedelta(days=i)).weekday() != 6
    ]
    attendance.mark(request=admin_req, user=clerk, day=days[0], status=Status.ABSENT)
    attendance.mark(request=admin_req, user=clerk, day=days[1], status=Status.ABSENT)
    attendance.mark(request=admin_req, user=clerk, day=days[2], status=Status.HALF_DAY)
    attendance.mark(request=admin_req, user=clerk, day=days[3], status=Status.LATE)

    inv = _bill(admin_req, patient, doctor.user)
    billing.receive_payment(inv, request=admin_req, method=cash, amount=D("1500"))
    CommissionEarning.objects.update(
        created_at=timezone.make_aware(timezone.datetime.combine(first, time(10)))
    )

    run = payroll.create_run(request=admin_req, year=year, month=month)
    slip = run.payslips.get(user=clerk)
    wd = attendance.working_days(year, month, 6)
    assert slip.working_days == wd and slip.absent_days == D("2.5") and slip.late_count == 1
    assert slip.absence_deduction == (D("30000") / wd * D("2.5")).quantize(D("0.01"))
    with pytest.raises(payroll.PayrollError, match="reason"):
        payroll.set_adjustments(slip, bonus=D("1000"), deduction=0, note="")
    payroll.set_adjustments(slip, bonus=D("1000"), deduction=D("200"), note="Eid bonus, fine")
    doc_slip = run.payslips.get(user=doctor.user)
    assert doc_slip.base_pay == 0 and doc_slip.commission == D("600.00")

    run = payroll.finalize(run, request=admin_req)
    assert run.status == PayrollRun.Status.FINALIZED
    slip.refresh_from_db()
    assert (
        payroll.balance(clerk) == slip.net_pay == (D("30000") - slip.absence_deduction + D("800"))
    )
    assert payroll.balance(doctor.user) == D("600.00")
    assert not commission.unsettled(doctor.user).exists()
    with pytest.raises(payroll.PayrollError):
        payroll.finalize(run, request=admin_req)
    with pytest.raises(attendance.AttendanceError, match="locked"):
        attendance.mark(request=admin_req, user=clerk, day=days[4], status=Status.ABSENT)


def test_daily_wage_counts_marked_days(admin_req, org, branch, make_user):
    year, month = _last_month()
    worker = make_user("nurse")
    PayProfile.objects.create(
        organization=org,
        branch=branch,
        user=worker,
        pay_type=PayProfile.PayType.DAILY,
        daily_rate=D("1000"),
        weekly_off=None,
    )
    first = date(year, month, 1)
    attendance.mark(request=admin_req, user=worker, day=first, status=Status.PRESENT)
    attendance.mark(
        request=admin_req, user=worker, day=first + timedelta(days=1), status=Status.HALF_DAY
    )
    run = payroll.create_run(request=admin_req, year=year, month=month)
    assert run.payslips.get().base_pay == D("1500.00")


def test_duplicate_or_future_payroll_refused(admin_req):
    year, month = _last_month()
    payroll.create_run(request=admin_req, year=year, month=month)
    with pytest.raises(payroll.PayrollError, match="already exists"):
        payroll.create_run(request=admin_req, year=year, month=month)
    nxt = timezone.localdate().replace(day=28) + timedelta(days=10)
    with pytest.raises(payroll.PayrollError, match="future"):
        payroll.create_run(request=admin_req, year=nxt.year, month=nxt.month)


def test_pay_staff_reduces_balance_once(admin_req, make_user, cash):
    user = make_user("lab")
    payroll.post_opening(request=admin_req, user=user, amount=D("5000"))
    payroll.pay_staff(
        request=admin_req, user=user, amount=D("3000"), method=cash, idempotency_key="k1"
    )
    payroll.pay_staff(
        request=admin_req, user=user, amount=D("3000"), method=cash, idempotency_key="k1"
    )
    payroll.pay_staff(
        request=admin_req,
        user=user,
        amount=D("4000"),
        method=cash,
        kind=StaffLedgerEntry.Kind.ADVANCE,
    )
    assert payroll.balance(user) == D("-2000.00")
    with pytest.raises(payroll.PayrollError):
        payroll.post_opening(request=admin_req, user=user, amount=D("1"))


def test_pay_views(admin_client, make_user, cash):
    user = make_user("lab")
    assert admin_client.get(reverse("hr:pay-list")).status_code == 200
    url = reverse("hr:pay-profile", args=[user.pk])
    assert admin_client.get(url).status_code == 200
    admin_client.post(
        url,
        {
            "pay_type": "monthly",
            "monthly_salary": "25000",
            "daily_rate": "0",
            "deduct_absences": "on",
            "work_start": "09:00",
            "late_grace_minutes": "10",
            "weekly_off": "6",
            "consultation_percent": "0",
            "procedure_percent": "0",
            "lab_referral_percent": "0",
            "is_active": "on",
        },
    )
    assert PayProfile.objects.get(user=user).monthly_salary == D("25000")
    ledger = reverse("hr:staff-ledger", args=[user.pk])
    assert admin_client.get(ledger).status_code == 200
    admin_client.post(
        reverse("hr:staff-pay", args=[user.pk]),
        {
            "kind": "advance",
            "amount": "2000",
            "method": cash.pk,
            "idempotency_key": "abc",
        },
    )
    assert payroll.balance(user) == D("-2000.00")
    year, month = _last_month()
    admin_client.post(reverse("hr:payroll-list"), {"month": f"{year}-{month:02d}"})
    run = PayrollRun.objects.get()
    assert admin_client.get(reverse("hr:payroll-detail", args=[run.pk])).status_code == 200
    slip = run.payslips.get()
    assert admin_client.get(reverse("hr:payslip-print", args=[slip.pk])).status_code == 200
    admin_client.post(reverse("hr:payroll-finalize", args=[run.pk]))
    run.refresh_from_db()
    assert run.status == PayrollRun.Status.FINALIZED


def test_doctor_sees_own_earnings_not_others_payslips(client, doctor, make_user, admin_req):
    year, month = _last_month()
    other = make_user("reception")
    PayProfile.objects.create(
        organization=admin_req.organization,
        branch=admin_req.branch,
        user=other,
        monthly_salary=D("1000"),
    )
    run = payroll.finalize(
        payroll.create_run(request=admin_req, year=year, month=month), request=admin_req
    )
    client.force_login(doctor.user)
    assert client.get(reverse("hr:earnings")).status_code == 200
    assert client.get(reverse("hr:payslip-print", args=[run.payslips.get().pk])).status_code == 403
    assert client.get(reverse("hr:pay-list")).status_code == 403
