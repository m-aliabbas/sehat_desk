from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import InvoiceItem
from apps.finance import segments, services, statements
from apps.finance.models import Expense, ExpenseCategory
from apps.hr.models import PayProfile

D = Decimal
K = InvoiceItem.Kind


@pytest.fixture
def req(make_request, admin_user):
    return make_request(admin_user)


@pytest.fixture
def categories(org):
    services.ensure_categories(org)
    return {c.name: c for c in ExpenseCategory.objects.filter(organization=org)}


def _paid(req, patient, cash, lines, *, doctor_user=None):
    inv = billing.create_invoice(request=req, patient=patient)
    for kind, price in lines:
        billing.add_item(
            inv,
            request=req,
            kind=kind,
            description=str(kind),
            unit_price=D(price),
            performed_by=doctor_user,
        )
    inv = billing.issue(inv, request=req)
    pay = billing.receive_payment(inv, request=req, method=cash, amount=inv.grand_total)
    return inv, pay


def _today():
    t = timezone.localdate()
    return t, t


def test_expense_record_void_and_idempotency(req, categories, cash):
    cat = categories["Lab reagents & kits"]
    e = services.record_expense(
        request=req, category=cat, amount="500", method=cash, idempotency_key="x1"
    )
    assert e.number.startswith("EXP-") and e.segment == segments.LAB
    assert (
        services.record_expense(
            request=req, category=cat, amount="500", method=cash, idempotency_key="x1"
        )
        == e
    )
    with pytest.raises(services.FinanceError, match="future"):
        services.record_expense(
            request=req,
            category=cat,
            amount="1",
            method=cash,
            day=timezone.localdate() + timedelta(days=1),
        )
    services.void_expense(e, request=req, reason="Duplicate bill")
    with pytest.raises(services.FinanceError, match="already void"):
        services.void_expense(e, request=req, reason="again")
    assert statements.expense_rows(req.organization, [req.branch], *_today()) == []


def test_pnl_by_segment(req, org, branch, patient, cash, categories, doctor):
    PayProfile.objects.create(
        organization=org,
        branch=branch,
        user=doctor.user,
        pay_type="commission",
        procedure_percent=D("40"),
    )
    _paid(req, patient, cash, [(K.PROCEDURE, "1500")], doctor_user=doctor.user)
    _paid(req, patient, cash, [(K.LAB, "1000"), (K.MISC, "200")])
    services.record_expense(
        request=req, category=categories["Lab reagents & kits"], amount="300", method=cash
    )
    services.record_expense(request=req, category=categories["Rent"], amount="100", method=cash)

    st = statements.statement(statements.pnl_rows(org, [branch], *_today()))
    assert st.income == {
        "doctors": D("1500.00"),
        "lab": D("1000.00"),
        "pharmacy": D("0.00"),
        "hospital": D("200.00"),
    }
    assert st.costs["doctors"] == D("600.00")  # 40% commission
    assert st.costs["lab"] == D("300.00") and st.costs["hospital"] == D("100.00")
    assert st.net_profit == D("2700.00") - D("1000.00")
    assert st.profit["doctors"] == D("900.00")
    assert "Procedure" in st.income_lines and "Doctor commission" in st.cost_lines


def test_refund_and_invoice_discount_reduce_income(req, org, branch, patient, cash):
    inv = billing.create_invoice(request=req, patient=patient)
    billing.add_item(inv, request=req, kind=K.LAB, description="CBC", unit_price=D("600"))
    billing.add_item(inv, request=req, kind=K.MISC, description="File", unit_price=D("400"))
    billing.set_invoice_discount(inv, request=req, amount=D("100"), reason="Staff")
    inv = billing.issue(inv, request=req)
    pay = billing.receive_payment(inv, request=req, method=cash, amount=D("900"))
    billing.refund(pay, request=req, amount=D("90"), reason="Partial")
    st = statements.statement(statements.pnl_rows(org, [branch], *_today()))
    # 600 − 60 discount − 54 refund; 400 − 40 − 36
    assert st.income["lab"] == D("486.00") and st.income["hospital"] == D("324.00")
    assert st.total_income == D("810.00")


def test_salaries_from_finalized_payroll(req, org, branch, make_user):
    from apps.hr import payroll

    user = make_user("reception")
    PayProfile.objects.create(organization=org, branch=branch, user=user, monthly_salary=D("20000"))
    today = timezone.localdate()
    run = payroll.create_run(request=req, year=today.year, month=today.month)
    st = statements.statement(statements.pnl_rows(org, [branch], *_today()))
    assert "Salaries & wages" not in st.cost_lines  # draft does not count
    payroll.finalize(run, request=req)
    st = statements.statement(statements.pnl_rows(org, [branch], today.replace(day=1), today))
    assert st.costs["hospital"] == D("20000.00")


def test_ledger_running_balance_and_segment(req, org, branch, patient, cash, categories, make_user):
    from apps.hr import payroll

    _paid(req, patient, cash, [(K.LAB, "1000"), (K.PHARMACY, "500")])
    services.record_expense(request=req, category=categories["Rent"], amount="300", method=cash)
    payroll.pay_staff(request=req, user=make_user("lab"), amount=D("200"), method=cash)
    data = statements.ledger(org, [branch], *_today())
    assert data["opening"] == 0
    assert data["money_in"] == D("1500.00") and data["money_out"] == D("500.00")
    assert data["closing"] == D("1000.00")
    assert data["rows"][-1]["running"] == data["closing"]
    lab = statements.ledger(org, [branch], *_today(), segment="lab")
    assert lab["closing"] == D("1000.00") and len(lab["rows"]) == 1
    tomorrow = timezone.localdate() + timedelta(days=1)
    later = statements.ledger(org, [branch], tomorrow, tomorrow)
    assert later["opening"] == D("1000.00") and not later["rows"]


def test_supplier_payment_hits_vendor_ledger_and_cash(req, org, branch, cash):
    from apps.pharmacy.models import Vendor

    vendor = Vendor.objects.create(organization=org, name="Medi Supplies")
    entry = services.pay_supplier(
        request=req, vendor=vendor, amount="2500", method=cash, idempotency_key="v1"
    )
    assert entry.amount == D("-2500.00")
    assert (
        services.pay_supplier(
            request=req, vendor=vendor, amount="2500", method=cash, idempotency_key="v1"
        )
        == entry
    )
    data = statements.ledger(org, [branch], *_today(), segment="pharmacy")
    assert data["money_out"] == D("2500.00")


def test_doctor_shares(req, org, branch, patient, cash, doctor):
    PayProfile.objects.create(
        organization=org,
        branch=branch,
        user=doctor.user,
        pay_type="commission",
        consultation_percent=D("50"),
    )
    _paid(req, patient, cash, [(K.CONSULTATION, "2000")], doctor_user=doctor.user)
    (row,) = statements.doctor_shares(org, [branch], *_today())
    assert row["income"] == D("2000.00") and row["commission"] == D("1000.00")
    assert row["clinic"] == D("1000.00")


def test_pages_render_for_admin(admin_client, req, patient, cash, categories):
    _paid(req, patient, cash, [(K.LAB, "1000")])
    for name in [
        "dashboard",
        "pnl",
        "ledger",
        "ledger-doctors",
        "expense-list",
        "expense-create",
        "category-list",
        "supplier-pay",
    ]:
        assert admin_client.get(reverse(f"finance:{name}")).status_code == 200, name
    for period in ["today", "last_month", "year"]:
        assert (
            admin_client.get(
                reverse("finance:dashboard"), {"period": period, "scope": "all"}
            ).status_code
            == 200
        )
    csv = admin_client.get(reverse("finance:pnl"), {"export": "csv"})
    assert csv["Content-Type"].startswith("text/csv") and b"Net profit" in csv.content
    csv = admin_client.get(reverse("finance:ledger"), {"export": "csv", "segment": "lab"})
    assert b"Opening balance" in csv.content
    resp = admin_client.post(
        reverse("finance:expense-create"),
        {
            "date": timezone.localdate().isoformat(),
            "category": categories["Rent"].pk,
            "amount": "750",
            "method": cash.pk,
            "idempotency_key": "form-1",
        },
    )
    assert resp.status_code == 302 and Expense.objects.get().amount == D("750")
    exp = Expense.objects.get()
    admin_client.post(reverse("finance:expense-void", args=[exp.pk]), {"reason": "Wrong"})
    exp.refresh_from_db()
    assert exp.is_void


def test_cashier_records_expenses_but_cannot_see_pnl(client, make_user):
    client.force_login(make_user("cashier"))
    resp = client.get(reverse("finance:dashboard"))
    assert resp.status_code == 302 and resp.url == reverse("finance:expense-list")
    assert client.get(reverse("finance:expense-create")).status_code == 200
    assert client.get(reverse("finance:pnl")).status_code == 403
    assert client.get(reverse("finance:ledger")).status_code == 403


def test_other_branch_figures_are_separate(
    req, org, branch, branch2, patient, cash, make_request, admin_user
):
    _paid(req, patient, cash, [(K.LAB, "1000")])
    other = statements.statement(statements.pnl_rows(org, [branch2], *_today()))
    assert other.total_income == 0
    both = statements.statement(statements.pnl_rows(org, [branch, branch2], *_today()))
    assert both.total_income == D("1000.00")
