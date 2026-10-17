"""Invoice register, invoice editing, payments, refunds, prints and billing settings."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import F, Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import HttpResponseClientRedirect

from apps.accounts.decorators import has_perm, permission_required
from apps.appointments.models import Appointment
from apps.audit import services as audit
from apps.core.forms import StyledFormMixin
from apps.core.scoping import get_scoped_or_404, scoped
from apps.ipd.models import AdmissionDeposit
from apps.masterdata.models import Service
from apps.patients.models import Patient

from . import services
from .models import ZERO, BillingSettings, Invoice, InvoiceItem, Payment, PaymentMethod, Refund

Status = Invoice.Status
VIEW, COLLECT, DISCOUNT, REFUND = (
    "billing.view",
    "billing.payment.collect",
    "billing.discount",
    "billing.refund",
)


def _invoices(request):
    return scoped(Invoice, request).select_related("patient", "appointment__doctor__user")


def _get_invoice(request, pk):
    return get_scoped_or_404(_invoices(request), request, pk=pk)


def _crumbs(*extra):
    return [{"label": "Billing", "url": reverse("billing:invoice-list")}, *extra]


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return timezone.localdate()


def _money_changes(before: Invoice, after: Invoice) -> dict:
    fields = ["status", "grand_total", "amount_paid", "amount_refunded", "invoice_discount"]
    return {
        f: [str(getattr(before, f)), str(getattr(after, f))]
        for f in fields
        if getattr(before, f) != getattr(after, f)
    }


# --- Register ----------------------------------------------------------------------------------


@require_GET
@permission_required(VIEW)
def invoice_list(request):
    """Invoice register (design screen 31). ?view=outstanding lists unpaid balances."""
    view = request.GET.get("view", "all")
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    invoices = _invoices(request)
    if view == "outstanding":
        invoices = invoices.filter(status__in=Invoice.OUTSTANDING_STATUSES).order_by("created_at")
    if status in Status.values:
        invoices = invoices.filter(status=status)
    if q:
        invoices = invoices.filter(
            Q(number__icontains=q)
            | Q(patient__mrn__icontains=q)
            | Q(customer_name__icontains=q)
            | Q(patient__name_search__icontains=" ".join(q.lower().split()))
        )
    outstanding = scoped(Invoice, request).filter(status__in=Invoice.OUTSTANDING_STATUSES)
    page = Paginator(invoices, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "billing/invoice_list.html",
        {
            "page_obj": page,
            "invoices": page.object_list,
            "view": view,
            "status": status,
            "q": q,
            "statuses": Status.choices,
            "outstanding_count": outstanding.count(),
            "outstanding_total": outstanding.aggregate(s=Sum(F("grand_total") - F("amount_paid")))[
                "s"
            ]
            or ZERO,
            "today": services.day_totals(request.branch),
            "can_collect": has_perm(request, COLLECT),
        },
    )


# --- Create ------------------------------------------------------------------------------------


class InvoiceCreateForm(StyledFormMixin, forms.Form):
    patient = forms.ModelChoiceField(queryset=Patient.objects.none(), widget=forms.HiddenInput)
    appointment = forms.ModelChoiceField(
        queryset=Appointment.objects.none(), required=False, widget=forms.HiddenInput
    )
    notes = forms.CharField(max_length=300, required=False, label="Note (optional)")

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["patient"].queryset = scoped(Patient, request).filter(is_active=True)
        self.fields["appointment"].queryset = scoped(Appointment, request)


@permission_required(COLLECT)
def invoice_create(request):
    """New invoice for a patient, or for an appointment (its consultation fee is added)."""
    initial = {}
    appt = None
    if request.GET.get("appointment", "").isdigit():
        appt = get_scoped_or_404(
            Appointment.objects.select_related("patient", "doctor__user"),
            request,
            pk=request.GET["appointment"],
        )
        initial = {"patient": appt.patient_id, "appointment": appt.pk}
    elif request.GET.get("patient", "").isdigit():
        initial = {"patient": request.GET["patient"]}
    form = InvoiceCreateForm(request.POST or None, initial=initial, request=request)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            invoice = services.create_invoice(
                request=request,
                patient=c["patient"],
                appointment=c["appointment"],
                notes=c["notes"],
            )
        except services.BillingError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "invoice.create",
                invoice,
                summary=f"{invoice.number} for {invoice.patient.mrn}",
            )
            messages.success(request, f"Invoice {invoice.number} started. Add items, then collect.")
            return redirect("billing:invoice-detail", pk=invoice.pk)
    patient = None
    pid = form["patient"].value()
    if pid and str(pid).isdigit():
        patient = scoped(Patient, request).filter(pk=pid).first()
    aid = form["appointment"].value()
    if appt is None and aid and str(aid).isdigit():
        appt = scoped(Appointment, request).filter(pk=aid).first()
    return render(
        request,
        "billing/invoice_create.html",
        {
            "form": form,
            "patient": patient,
            "appointment": appt,
            "breadcrumbs": _crumbs({"label": "New invoice"}),
        },
    )


# --- Detail and draft editing ------------------------------------------------------------------


class ItemForm(StyledFormMixin, forms.Form):
    service = forms.ModelChoiceField(
        queryset=Service.objects.none(), required=False, empty_label="Custom item"
    )
    description = forms.CharField(max_length=160, required=False)
    unit_price = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, label="Unit price"
    )
    quantity = forms.IntegerField(min_value=1, max_value=999, initial=1)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = scoped(Service, request).filter(is_active=True)

    def clean(self):
        c = super().clean()
        if not c.get("service"):
            if not (c.get("description") or "").strip():
                self.add_error("description", "Describe the custom item.")
            if c.get("unit_price") is None:
                self.add_error("unit_price", "Give a price for the custom item.")
        return c


class PaymentForm(StyledFormMixin, forms.Form):
    method = forms.ModelChoiceField(queryset=PaymentMethod.objects.none(), empty_label=None)
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), label="Amount received"
    )
    reference = forms.CharField(max_length=80, required=False, label="Reference (optional)")
    note = forms.CharField(max_length=200, required=False)
    key = forms.CharField(max_length=40, widget=forms.HiddenInput)

    def __init__(self, *args, request, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["method"].queryset = services.active_methods(request.organization)


def _pay_form(request, invoice, data=None):
    return PaymentForm(
        data,
        request=request,
        initial={"amount": invoice.balance_due or invoice.grand_total, "key": uuid.uuid4().hex},
    )


def _detail_context(request, invoice, **extra):
    payments = invoice.payments.select_related("method", "received_by").order_by("created_at")
    refunds = invoice.refunds.select_related("method", "refunded_by", "payment").order_by(
        "created_at"
    )
    trail = [{"when": invoice.created_at, "text": f"Invoice created by {invoice.created_by}"}]
    if invoice.issued_at:
        trail.append({"when": invoice.issued_at, "text": f"Issued by {invoice.issued_by}"})
    trail += [
        {
            "when": p.created_at,
            "text": f"{p.number} · {p.method} {p.amount:,.2f} received by {p.received_by}",
            "payment": p,
        }
        for p in payments
    ]
    trail += [
        {
            "when": r.created_at,
            "text": f"{r.number} · refund {r.amount:,.2f} by {r.refunded_by} — {r.reason}",
            "refund": r,
        }
        for r in refunds
    ]
    if invoice.cancelled_at:
        trail.append(
            {
                "when": invoice.cancelled_at,
                "text": f"Cancelled by {invoice.cancelled_by} — {invoice.cancel_reason}",
            }
        )
    can_collect = has_perm(request, COLLECT)
    ctx = {
        "invoice": invoice,
        "items": invoice.items.select_related("service"),
        "payments": payments,
        "refunds": refunds,
        "trail": sorted(trail, key=lambda t: t["when"]),
        "can_collect": can_collect,
        "can_discount": has_perm(request, DISCOUNT),
        "can_refund": has_perm(request, REFUND),
        "can_cancel": invoice.status != Status.CANCELLED
        and invoice.net_paid == ZERO
        and (has_perm(request, REFUND) or (invoice.is_draft and can_collect)),
        "item_form": ItemForm(request=request) if invoice.is_draft and can_collect else None,
        "billing_settings": services.settings_for(request.organization),
        "breadcrumbs": _crumbs({"label": invoice.number}),
        "new_receipt": None,
    }
    if request.GET.get("receipt", "").isdigit():
        ctx["new_receipt"] = payments.filter(pk=request.GET["receipt"]).first()
    ctx.update(extra)
    return ctx


@require_GET
@permission_required(VIEW)
def invoice_detail(request, pk):
    """Invoice detail with payment trail (design screen 10)."""
    invoice = _get_invoice(request, pk)
    return render(request, "billing/invoice_detail.html", _detail_context(request, invoice))


def _draft_action(request, pk):
    invoice = _get_invoice(request, pk)
    if not invoice.is_draft:
        messages.error(request, f"{invoice.number} is issued; its items are locked.")
        return invoice, False
    return invoice, True


@require_POST
@permission_required(COLLECT)
def item_add(request, pk):
    invoice, ok = _draft_action(request, pk)
    if not ok:
        return redirect("billing:invoice-detail", pk=pk)
    form = ItemForm(request.POST, request=request)
    if form.is_valid():
        c = form.cleaned_data
        try:
            item = services.add_item(
                invoice,
                request=request,
                service=c["service"],
                description="" if c["service"] else c["description"],
                unit_price=None if c["service"] else c["unit_price"],
                quantity=c["quantity"],
                kind=None if c["service"] else InvoiceItem.Kind.MISC,
            )
        except services.BillingError as err:
            form.add_error(None, err.message)
        else:
            audit.record(
                request,
                "invoice.item_add",
                invoice,
                summary=(
                    f"{invoice.number}: {item.description} × {item.quantity} @ {item.unit_price}"
                ),
            )
            return redirect("billing:invoice-detail", pk=pk)
    invoice.refresh_from_db()
    return render(
        request, "billing/invoice_detail.html", _detail_context(request, invoice, item_form=form)
    )


@require_POST
@permission_required(COLLECT)
def item_remove(request, pk, item_pk):
    invoice, ok = _draft_action(request, pk)
    if ok:
        item = invoice.items.filter(pk=item_pk).first()
        if item is None:
            raise Http404
        services.remove_item(item)
        audit.record(
            request,
            "invoice.item_remove",
            invoice,
            summary=f"{invoice.number}: {item.description} × {item.quantity}",
        )
    return redirect("billing:invoice-detail", pk=pk)


@require_POST
@permission_required(DISCOUNT)
def discount(request, pk):
    """Line discount (with item=<id>) or invoice discount. Needs billing.discount and a reason."""
    invoice, ok = _draft_action(request, pk)
    if not ok:
        return redirect("billing:invoice-detail", pk=pk)
    before = Invoice.objects.get(pk=invoice.pk)
    reason = request.POST.get("reason", "")
    try:
        amount = services.to_money(request.POST.get("amount") or 0)
    except ArithmeticError:
        messages.error(request, "Enter the discount as a number.")
        return redirect("billing:invoice-detail", pk=pk)
    item_id = request.POST.get("item", "")
    try:
        if item_id:
            item = invoice.items.filter(pk=item_id).first() if item_id.isdigit() else None
            if item is None:
                raise Http404
            services.set_line_discount(item, request=request, amount=amount, reason=reason)
            target = item.description
        else:
            services.set_invoice_discount(invoice, request=request, amount=amount, reason=reason)
            target = "invoice"
    except services.BillingError as err:
        messages.error(request, err.message)
    else:
        invoice.refresh_from_db()
        audit.record(
            request,
            "invoice.discount",
            invoice,
            changes=_money_changes(before, invoice),
            summary=f"{invoice.number}: {amount} off {target} — {reason.strip()[:120]}",
        )
        messages.success(request, "Discount applied." if amount else "Discount removed.")
    return redirect("billing:invoice-detail", pk=pk)


@require_POST
@permission_required(COLLECT)
def issue(request, pk):
    invoice = _get_invoice(request, pk)
    try:
        invoice = services.issue(invoice, request=request)
    except services.BillingError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "invoice.issue",
            invoice,
            changes={"status": ["draft", invoice.status]},
            summary=f"{invoice.number} total {invoice.grand_total}",
        )
        messages.success(request, f"{invoice.number} issued. Items are now locked.")
    return redirect("billing:invoice-detail", pk=pk)


@require_POST
@permission_required(COLLECT, REFUND, any_of=True)
def cancel(request, pk):
    invoice = _get_invoice(request, pk)
    before = invoice.status
    reason = request.POST.get("reason", "")
    if not invoice.is_draft and not has_perm(request, REFUND):
        raise PermissionDenied
    try:
        invoice = services.cancel(invoice, request=request, reason=reason)
    except services.BillingError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "invoice.cancel",
            invoice,
            changes={"status": [before, invoice.status]},
            summary=f"{invoice.number} — {invoice.cancel_reason}",
        )
        messages.success(request, f"{invoice.number} cancelled.")
    return redirect("billing:invoice-detail", pk=pk)


# --- Payment -----------------------------------------------------------------------------------


@permission_required(COLLECT)
def pay(request, pk):
    """Payment modal (htmx) or page. Success redirects to the invoice with the receipt ready."""
    invoice = _get_invoice(request, pk)
    if not (invoice.is_draft or invoice.is_outstanding):
        messages.error(request, f"{invoice.number} has nothing left to pay.")
        return redirect("billing:invoice-detail", pk=pk)
    form = _pay_form(request, invoice, request.POST or None)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        before = Invoice.objects.get(pk=invoice.pk)
        try:
            payment = services.receive_payment(
                invoice,
                request=request,
                method=c["method"],
                amount=c["amount"],
                reference=c["reference"],
                note=c["note"],
                idempotency_key=c["key"],
            )
        except services.BillingError as err:
            form.add_error(None, err.message)
        else:
            invoice.refresh_from_db()
            if invoice.amount_paid != before.amount_paid:  # not a replayed double-submit
                audit.record(
                    request,
                    "payment.create",
                    payment,
                    changes=_money_changes(before, invoice),
                    summary=(
                        f"{payment.number} {payment.method} {payment.amount} on {invoice.number}"
                    ),
                )
            messages.success(
                request, f"{payment.number}: {payment.amount:,.2f} received ({payment.method})."
            )
            url = f"{reverse('billing:invoice-detail', args=[pk])}?receipt={payment.pk}"
            return HttpResponseClientRedirect(url) if request.htmx else redirect(url)
    ctx = {"invoice": invoice, "form": form, "breadcrumbs": _crumbs(
        {"label": invoice.number, "url": reverse("billing:invoice-detail", args=[pk])},
        {"label": "Collect payment"},
    )}  # fmt: skip
    template = "billing/_pay_modal.html" if request.htmx else "billing/pay.html"
    return render(request, template, ctx)


# --- Refund ------------------------------------------------------------------------------------


class RefundForm(StyledFormMixin, forms.Form):
    payment = forms.ModelChoiceField(
        queryset=Payment.objects.none(), empty_label=None, label="Original payment"
    )
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), label="Refund amount"
    )
    method = forms.ModelChoiceField(
        queryset=PaymentMethod.objects.none(),
        required=False,
        empty_label="Original payment method",
        label="Refund method",
    )
    reason = forms.CharField(max_length=200, label="Refund reason")
    reference = forms.CharField(max_length=80, required=False, label="Reference (optional)")
    key = forms.CharField(max_length=40, widget=forms.HiddenInput)

    def __init__(self, *args, request, invoice, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment"].queryset = invoice.payments.select_related("method")
        self.fields["payment"].label_from_instance = lambda p: (
            f"{p.number} · {p.method} · {p.amount:,.2f} (refundable {p.refundable:,.2f})"
        )
        self.fields["method"].queryset = services.active_methods(request.organization)


@permission_required(REFUND)
def refund(request, pk):
    """Refund against an original payment (design screen 41). Needs billing.refund."""
    invoice = _get_invoice(request, pk)
    if invoice.refundable <= ZERO:
        messages.error(request, f"{invoice.number} has no paid amount left to refund.")
        return redirect("billing:invoice-detail", pk=pk)
    first = next((p for p in invoice.payments.order_by("created_at") if p.refundable > 0), None)
    form = RefundForm(
        request.POST or None,
        request=request,
        invoice=invoice,
        initial={
            "payment": first.pk if first else None,
            "amount": first.refundable if first else None,
            "key": uuid.uuid4().hex,
        },
    )
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        before = Invoice.objects.get(pk=invoice.pk)
        try:
            row = services.refund(
                c["payment"],
                request=request,
                amount=c["amount"],
                reason=c["reason"],
                method=c["method"],
                reference=c["reference"],
                idempotency_key=c["key"],
            )
        except services.BillingError as err:
            form.add_error(None, err.message)
        else:
            invoice.refresh_from_db()
            audit.record(
                request,
                "refund.create",
                row,
                changes=_money_changes(before, invoice),
                summary=(
                    f"{row.number} {row.amount} from {row.payment.number} on {invoice.number}"
                    f" — {row.reason}"
                ),
            )
            messages.success(request, f"{row.number}: {row.amount:,.2f} refunded.")
            return redirect("billing:invoice-detail", pk=pk)
    return render(
        request,
        "billing/refund.html",
        {
            "invoice": invoice,
            "form": form,
            "breadcrumbs": _crumbs(
                {"label": invoice.number, "url": reverse("billing:invoice-detail", args=[pk])},
                {"label": "Refund"},
            ),
        },
    )


# --- Prints ------------------------------------------------------------------------------------


@require_GET
@permission_required(VIEW)
def invoice_print(request, pk):
    invoice = _get_invoice(request, pk)
    if invoice.is_draft:
        messages.error(request, "Issue the invoice before printing it.")
        return redirect("billing:invoice-detail", pk=pk)
    return render(
        request,
        "billing/invoice_print.html",
        {
            "invoice": invoice,
            "items": invoice.items.all(),
            "payments": invoice.payments.select_related("method"),
            "refunds": invoice.refunds.select_related("method"),
            "billing_settings": services.settings_for(request.organization),
        },
    )


@require_GET
@permission_required(VIEW)
def receipt_print(request, pk):
    payment = get_scoped_or_404(
        Payment.objects.select_related("invoice__patient", "method", "received_by"), request, pk=pk
    )
    return render(
        request,
        "billing/receipt_print.html",
        {
            "payment": payment,
            "inv": payment.invoice,
            "billing_settings": services.settings_for(request.organization),
        },
    )


# --- Daily payment list ------------------------------------------------------------------------


@require_GET
@permission_required(VIEW)
def payment_list(request):
    """Payments, refunds and inpatient deposits in this branch on one day, by method.

    Deposits count on the day they are received; their later application to the final bill
    is listed but not counted again. Deposit balances handed back count as money out.
    """
    day = _parse_date(request.GET.get("date"))
    method = request.GET.get("method", "")
    payments = (
        scoped(Payment, request)
        .filter(created_at__date=day)
        .select_related("invoice__patient", "method", "received_by")
    )
    refunds = (
        scoped(Refund, request)
        .filter(created_at__date=day)
        .select_related("invoice__patient", "method", "refunded_by", "payment")
    )
    deposits = (
        scoped(AdmissionDeposit, request)
        .filter(created_at__date=day)
        .select_related("admission__patient", "method", "received_by")
    )
    deposit_returns = (
        scoped(AdmissionDeposit, request)
        .filter(returned_at__date=day, returned_amount__gt=0)
        .select_related("admission__patient", "method", "returned_by")
    )
    if method.isdigit():
        payments, refunds = payments.filter(method_id=method), refunds.filter(method_id=method)
        deposits = deposits.filter(method_id=method)
        deposit_returns = deposit_returns.filter(method_id=method)
    applied = set(services.applied_deposit_payments(payments).values_list("pk", flat=True))
    return render(
        request,
        "billing/payment_list.html",
        {
            "day": day,
            "today": timezone.localdate(),
            "prev_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "payments": payments,
            "refunds": refunds,
            "deposits": deposits,
            "deposit_returns": deposit_returns,
            "applied_deposit_ids": applied,
            "totals": services.day_totals(request.branch, day),
            "methods": services.active_methods(request.organization),
            "method": method,
        },
    )


# --- Settings → Billing ------------------------------------------------------------------------


class BillingSettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BillingSettings
        fields = ["tax_label", "default_tax_percent", "max_discount_percent", "receipt_message"]


class PaymentMethodForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PaymentMethod
        fields = ["name", "requires_reference"]


SETTINGS = "admin.organization.manage"


@permission_required(SETTINGS)
def billing_settings(request):
    """Payment methods and tax (PRD §21 Settings → Billing). Invoice prefix lives on Hospital."""
    obj = services.settings_for(request.organization)
    services.ensure_payment_methods(request.organization)
    form = BillingSettingsForm(
        request.POST if request.POST.get("form") == "settings" else None, instance=obj
    )
    method_form = PaymentMethodForm(
        request.POST if request.POST.get("form") == "method" else None,
        instance=PaymentMethod(organization=request.organization),
    )
    if request.method == "POST":
        if form.is_bound and form.is_valid():
            before = audit.snapshot(BillingSettings.objects.get(pk=obj.pk), form.Meta.fields)
            form.save()
            audit.record(
                request, "billing.settings", obj, changes=audit.diff(before, obj, form.Meta.fields)
            )
            messages.success(request, "Billing settings saved. New invoice items use them.")
            return redirect("billing:settings")
        if method_form.is_bound and method_form.is_valid():
            m = method_form.save(commit=False)
            base = "-".join(m.name.lower().split())[:24] or "method"
            code, n = base, 1
            while PaymentMethod.objects.filter(
                organization=request.organization, code=code
            ).exists():
                n += 1
                code = f"{base}-{n}"
            m.code = code
            m.sort_order = PaymentMethod.objects.filter(organization=request.organization).count()
            m.save()
            audit.record(request, "payment_method.create", m, summary=m.name)
            messages.success(request, f"Payment method {m.name} added.")
            return redirect("billing:settings")
    return render(
        request,
        "billing/settings.html",
        {
            "form": form,
            "method_form": method_form,
            "methods": PaymentMethod.objects.filter(organization=request.organization),
            "settings_tab": "billing",
        },
    )


@require_POST
@permission_required(SETTINGS)
def method_toggle(request, pk):
    method = get_scoped_or_404(PaymentMethod, request, pk=pk)
    if (
        method.is_active
        and not services.active_methods(request.organization).exclude(pk=method.pk).exists()
    ):
        messages.error(request, "Keep at least one payment method active.")
        return redirect("billing:settings")
    method.is_active = not method.is_active
    method.save(update_fields=["is_active", "updated_at"])
    audit.record(
        request, "payment_method.enable" if method.is_active else "payment_method.disable", method
    )
    messages.success(
        request, f"{method.name} {'enabled' if method.is_active else 'disabled'} for new payments."
    )
    return redirect("billing:settings")
