"""Patient flow services: every state change of a PatientVisit and PatientBillItem goes through
here. Views call the request-taking functions; apps/flow/signals.py calls the on_* functions when
the queue, billing, prescriptions, lab or pharmacy save something.

    reception  open_visit (token issued) → admit (reception fees paid) → IN_QUEUE
    doctor     on_token_status: in consultation → WITH_DOCTOR; completed → doctor_next
               on_sent_on: Rx sent / test ordered → auto_next, only if the doctor need not click
    fail-safe  pull_by_token: pharmacy or lab takes a patient the doctor never sent on
    lab        lab_guard: refuses lab work on a visit while any lab fee is unpaid
    pharmacy   pharmacy_dues + bill_pharmacy_dues: one till invoice for medicine and fees
"""

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_fsm import can_proceed

from apps.audit import services as audit
from apps.billing import services as billing
from apps.billing.models import Invoice, InvoiceItem
from apps.core.dates import on_day
from apps.core.features import is_enabled
from apps.organizations.models import preferences_for

from .models import (
    CounterLocation,
    HospitalFlowSetting,
    PatientBillItem,
    PatientVisit,
    RxPrintAt,
    VisitState,
)

COLLECT = "billing.payment.collect"
# Who may take a patient by token at each counter.
PULL_PERMISSION = {
    CounterLocation.PHARMACY: "pharmacy.sale",
    CounterLocation.LAB: "lab.sample.collect",
}
PULL_STATE = {
    CounterLocation.PHARMACY: VisitState.PHARMACY_QUEUE,
    CounterLocation.LAB: VisitState.LAB_QUEUE,
}
PAID_STATUSES = (Invoice.Status.PAID, Invoice.Status.REFUNDED)


class FlowError(ValidationError):
    pass


# --- Settings ---------------------------------------------------------------------------------


def flow_settings_for(organization) -> HospitalFlowSetting:
    """The hospital's row, made on first use. A new row starts from the old Preferences switch,
    so a hospital that did not collect the fee up front keeps doing so."""
    cached = getattr(organization, "_sd_flow_settings", None)
    if cached is None:
        upfront = preferences_for(organization).opd_collect_fee_upfront
        cached, _ = HospitalFlowSetting.objects.get_or_create(
            organization=organization,
            defaults={
                "consultation_fee_billed_at": CounterLocation.RECEPTION
                if upfront
                else CounterLocation.CENTRAL_CASH
            },
        )
        organization._sd_flow_settings = cached
    return cached


@transaction.atomic
def save_settings(form, *, request, print_blank_rx: bool) -> HospitalFlowSetting:
    """Save Settings → Patient flow. The blank Rx and 'collect up front' switches stay on
    Preferences, where the visit papers read them; they are kept in step here."""
    if "admin.organization.manage" not in request.sd_perms:
        raise FlowError("You are not allowed to change hospital settings.")
    fields = list(form.Meta.fields)
    before = audit.snapshot(HospitalFlowSetting.objects.get(pk=form.instance.pk), fields)
    obj = form.save()
    prefs = preferences_for(request.organization)
    prefs.opd_print_blank_prescription = print_blank_rx
    prefs.opd_collect_fee_upfront = obj.consultation_fee_billed_at == CounterLocation.RECEPTION
    prefs.save(
        update_fields=["opd_print_blank_prescription", "opd_collect_fee_upfront", "updated_at"]
    )
    request.organization._sd_flow_settings = obj
    audit.record(request, "flow.settings", obj, changes=audit.diff(before, obj, fields))
    return obj


def fee_counter(organization) -> str:
    """Where the doctor's fee is paid."""
    return flow_settings_for(organization).consultation_fee_billed_at


# --- Lookups ----------------------------------------------------------------------------------


def visit_for_token(token) -> PatientVisit | None:
    try:
        return token.flow_visit
    except PatientVisit.DoesNotExist:
        return None


def find_token_visit(request, label: str) -> PatientVisit:
    """Today's visit for a token number typed or scanned at a counter (A-012, a-12)."""
    label = (label or "").strip().upper()
    if not label:
        raise FlowError("Type the token number, e.g. A-012.")
    letters, _, digits = label.partition("-")
    if digits.isdigit():
        label = f"{letters}-{int(digits):03d}"
    visit = (
        PatientVisit.objects.filter(
            organization=request.organization,
            branch=request.branch,
            token__date=timezone.localdate(),
            token__label=label,
        )
        .select_related("token__doctor__user", "patient", "appointment")
        .first()
    )
    if visit is None:
        raise FlowError(f"No visit with token {label} today in this branch.")
    return visit


def _lock(visit) -> PatientVisit:
    # Not core.db.locked: a protected FSM field cannot be refreshed in place.
    return PatientVisit.objects.select_for_update(of=("self",)).get(pk=visit.pk)


def _save(visit) -> PatientVisit:
    visit.state_changed_at = timezone.now()
    visit.save()
    return visit


def _encounter(visit):
    try:
        return visit.token.encounter
    except ObjectDoesNotExist:
        return None


def _has_digital_rx(visit) -> bool:
    encounter = _encounter(visit)
    if encounter is None:
        return False
    try:
        return encounter.prescription.items.exists()
    except ObjectDoesNotExist:
        return False


def lab_orders(visit):
    """Lab orders for this visit: from its consultation, or made at the lab desk that day."""
    from apps.lab.models import LabOrder

    return LabOrder.objects.filter(
        Q(encounter__token=visit.token)
        | Q(
            encounter__isnull=True,
            patient=visit.patient,
            branch=visit.branch,
            **on_day("created_at", visit.token.date),
        )
    ).exclude(status=LabOrder.Status.CANCELLED)


def visit_for_lab_order(order) -> PatientVisit | None:
    if order.encounter_id and order.encounter.token_id:
        return visit_for_token(order.encounter.token)
    return (
        PatientVisit.objects.filter(
            patient=order.patient,
            branch=order.branch,
            token__date=timezone.localdate(),
        )
        .exclude(state=VisitState.CANCELLED)
        .order_by("-created_at")
        .first()
    )


# --- Reception --------------------------------------------------------------------------------


@transaction.atomic
def open_visit(token) -> PatientVisit:
    """A token was issued: start tracking the visit and route the doctor's fee to its counter.
    Called from the token's post_save signal, inside the same transaction as the token."""
    if visit := visit_for_token(token):
        return visit
    from apps.queue.models import QueueToken

    org = token.organization
    visit = PatientVisit.objects.create(
        organization=org,
        branch=token.branch,
        token=token,
        appointment=token.appointment,
        patient=token.patient,
        is_emergency=token.priority == QueueToken.Priority.EMERGENCY,
        print_blank_rx=flow_settings_for(org).reception_prints_blank_rx,
    )
    appt = token.appointment
    if appt is not None and appt.fee > 0:
        PatientBillItem.objects.create(
            organization=org,
            branch=token.branch,
            visit=visit,
            kind=InvoiceItem.Kind.CONSULTATION,
            description=f"Consultation · {appt.doctor}",
            unit_price=appt.fee,
            billed_at=fee_counter(org),
            performed_by=appt.doctor.user,
            added_by=token.issued_by,
        )
    _try_admit(visit)
    return visit


def _try_admit(visit) -> PatientVisit:
    if visit.state == VisitState.TOKEN_ISSUED and can_proceed(visit.admit_to_queue):
        visit.admit_to_queue()
        _save(visit)
    return visit


def is_held(token) -> bool:
    """The token cannot be called yet: something is still to pay at reception."""
    visit = visit_for_token(token)
    return visit is not None and visit.state == VisitState.TOKEN_ISSUED


@transaction.atomic
def add_charge(*, request, visit, service, quantity=1) -> PatientBillItem:
    """Add a service fee (injection, dressing...) to the visit. The service's billed_at decides
    which counter takes it."""
    if not {COLLECT, "queue.manage", "consultation.create"} & set(request.sd_perms):
        raise FlowError("You are not allowed to add charges to a visit.")
    if service.organization_id != request.organization.pk or not service.is_active:
        raise FlowError("That service is not available.")
    if visit.state in (VisitState.COMPLETED, VisitState.CANCELLED):
        raise FlowError(f"This visit is {visit.get_state_display().lower()}.")
    item = PatientBillItem.objects.create(
        organization=visit.organization,
        branch=visit.branch,
        visit=visit,
        kind=InvoiceItem.Kind.PROCEDURE if service.is_procedure else InvoiceItem.Kind.SERVICE,
        service=service,
        description=service.name,
        quantity=quantity,
        unit_price=service.price_for(visit.branch),
        billed_at=service.billed_at,
        added_by=request.user,
    )
    audit.record(request, "flow.charge", item, summary=f"{item.description} · {item.billed_at}")
    return item


# --- Billing aggregation engine ---------------------------------------------------------------


def unbilled(visit_or_visits, counter):
    """Fees for this counter that no invoice carries yet."""
    qs = PatientBillItem.objects.filter(is_paid=False, invoice__isnull=True, billed_at=counter)
    if isinstance(visit_or_visits, PatientVisit):
        return qs.filter(visit=visit_or_visits)
    return qs.filter(visit__in=visit_or_visits)


def collect_lines(invoice, *, request, items, counter) -> list[InvoiceItem]:
    """Put each fee on a draft invoice and remember which counter took it."""
    lines = []
    for item in items:
        line = billing.add_item(
            invoice,
            request=request,
            service=item.service,
            description=item.description,
            unit_price=item.unit_price,
            quantity=item.quantity,
            kind=item.kind,
            performed_by=item.performed_by,
        )
        item.invoice = invoice
        item.invoice_item = line
        item.collected_by_counter = counter
        item.save(update_fields=["invoice", "invoice_item", "collected_by_counter", "updated_at"])
        lines.append(line)
    return lines


@transaction.atomic
def counter_invoice(*, request, visit, counter) -> Invoice:
    """One issued invoice for everything this visit owes at this counter (reception, lab,
    central cash). It is paid on the normal Collect payment screen."""
    if COLLECT not in request.sd_perms:
        raise FlowError("You are not allowed to collect payments.")
    items = list(unbilled(visit, counter).select_related("service", "performed_by"))
    if not items:
        raise FlowError(f"Nothing is waiting to be paid at {CounterLocation(counter).label}.")
    try:
        invoice = billing.create_invoice(
            request=request,
            patient=visit.patient,
            notes=f"{CounterLocation(counter).label} · token {visit.token.label}",
        )
        collect_lines(invoice, request=request, items=items, counter=counter)
        invoice = billing.issue(invoice, request=request)
    except billing.BillingError as err:
        raise FlowError(err.message) from err
    audit.record(request, "flow.counter_invoice", invoice, summary=f"{counter} · {len(items)}")
    return invoice


def _today_visits(patient, branch):
    return PatientVisit.objects.filter(
        patient=patient, branch=branch, token__date=timezone.localdate()
    ).exclude(state=VisitState.CANCELLED)


def pharmacy_dues(sale) -> list[PatientBillItem]:
    """Fees the till must take with this sale: today's visits of the sale's patient, billed at
    the pharmacy and on no invoice yet (doctor's fee, injection fee...)."""
    if not sale or not sale.patient_id:
        return []
    visits = _today_visits(sale.patient, sale.branch)
    return list(
        unbilled(visits, CounterLocation.PHARMACY).select_related(
            "service", "performed_by", "visit__token"
        )
    )


def bill_pharmacy_dues(invoice, *, request, sale) -> list[InvoiceItem]:
    """Called by pharmacy.pos.checkout on its draft invoice: medicine and fees, one bill."""
    return collect_lines(
        invoice, request=request, items=pharmacy_dues(sale), counter=CounterLocation.PHARMACY
    )


@transaction.atomic
def sync_invoice(invoice) -> None:
    """An invoice was saved (signal). Its fees follow it: paid, or free again if cancelled.
    The reception fee invoice is matched to the visit's consultation fee here."""
    if invoice.appointment_id:
        _link_fee_invoice(invoice)
    items = list(PatientBillItem.objects.filter(invoice=invoice).select_related("visit"))
    if not items:
        return
    now = timezone.now()
    paid = invoice.status in PAID_STATUSES
    for item in items:
        if invoice.status == Invoice.Status.CANCELLED:
            item.invoice = item.invoice_item = None
            item.is_paid, item.paid_at, item.collected_by_counter = False, None, ""
        elif paid != item.is_paid:
            item.is_paid, item.paid_at = paid, now if paid else None
        else:
            continue
        item.save(
            update_fields=[
                "invoice", "invoice_item", "is_paid", "paid_at", "collected_by_counter",
                "updated_at",
            ]
        )  # fmt: skip
    for visit in {item.visit for item in items}:
        visit = _lock(visit)
        _try_admit(visit)
        if visit.state == VisitState.PHARMACY_QUEUE and paid and can_proceed(visit.pharmacy_done):
            visit.pharmacy_done()
            _save(visit)


def _link_fee_invoice(invoice) -> None:
    """Reception's fee invoice (appointments.visits) carries the consultation line."""
    if invoice.status == Invoice.Status.CANCELLED:
        return
    item = (
        PatientBillItem.objects.filter(
            visit__appointment_id=invoice.appointment_id,
            kind=InvoiceItem.Kind.CONSULTATION,
            invoice__isnull=True,
        )
        .order_by("pk")
        .first()
    )
    if item is None:
        return
    line = invoice.items.filter(kind=InvoiceItem.Kind.CONSULTATION).first()
    item.invoice = invoice
    item.invoice_item = line if line and not hasattr(line, "flow_bill_item") else None
    item.collected_by_counter = CounterLocation.RECEPTION
    item.save(update_fields=["invoice", "invoice_item", "collected_by_counter", "updated_at"])


# --- Doctor -----------------------------------------------------------------------------------


def _pharmacy_needed(visit, settings_row) -> bool:
    if not is_enabled(visit.organization, "pharmacy"):
        return False
    if visit.digital_rx_generated or unbilled(visit, CounterLocation.PHARMACY).exists():
        return True
    return visit.is_manual_rx_flagged and settings_row.pharmacy_handles_manual_rx


def _lab_pending(visit) -> bool:
    from apps.lab.models import LabOrder

    if not is_enabled(visit.organization, "lab"):
        return False
    return (
        lab_orders(visit)
        .filter(status__in=(LabOrder.Status.PAYMENT_PENDING, LabOrder.Status.SAMPLE_PENDING))
        .exists()
    )


def _note_rx(visit, settings_row) -> None:
    """Digital or paper. No consultation note at all means the doctor wrote on paper; a note
    with no medicines is a paper prescription only where doctors do not write in SehatDesk."""
    visit.digital_rx_generated = _has_digital_rx(visit)
    visit.is_manual_rx_flagged = not visit.digital_rx_generated and (
        _encounter(visit) is None or not settings_row.doctor_generates_digital_rx
    )


def route_after_doctor(visit) -> str:
    settings_row = flow_settings_for(visit.organization)
    _note_rx(visit, settings_row)
    if _lab_pending(visit):
        return VisitState.LAB_QUEUE
    if _pharmacy_needed(visit, settings_row):
        return VisitState.PHARMACY_QUEUE
    return VisitState.COMPLETED


def route_after_lab(visit) -> str:
    settings_row = flow_settings_for(visit.organization)
    if _pharmacy_needed(visit, settings_row):
        return VisitState.PHARMACY_QUEUE
    return VisitState.COMPLETED


@transaction.atomic
def on_token_status(token) -> None:
    """The queue moved the token (signal). Calling and seeing follow the doctor's own buttons."""
    from apps.queue.models import QueueToken

    visit = visit_for_token(token)
    if visit is None:
        return
    visit = _lock(visit)
    status = token.status
    if status == QueueToken.Status.CANCELLED:
        if can_proceed(visit.cancel):
            visit.cancel()
            _save(visit)
        return
    if status in (QueueToken.Status.IN_CONSULTATION, QueueToken.Status.COMPLETED):
        if visit.state == VisitState.IN_QUEUE:
            visit.start_consult()
            _save(visit)
    if status == QueueToken.Status.COMPLETED and visit.state == VisitState.WITH_DOCTOR:
        visit.doctor_next(route_after_doctor(visit))
        _save(visit)


@transaction.atomic
def on_sent_on(token) -> None:
    """The doctor sent a prescription or ordered a test (signal). Where the doctor need not
    press Checked · Next, the patient goes on now and the doctor's token closes."""
    visit = visit_for_token(token)
    if visit is None:
        return
    visit = _lock(visit)
    if visit.state == VisitState.IN_QUEUE:
        visit.start_consult()
        _save(visit)
    if visit.state != VisitState.WITH_DOCTOR or not can_proceed(visit.auto_next):
        return
    destination = route_after_doctor(visit)
    if destination == VisitState.COMPLETED:
        return  # nothing for another counter yet; the doctor closes the visit
    visit.auto_next(destination)
    _save(visit)
    _close_token(token)


def _close_token(token) -> None:
    from apps.queue import services as queue_services

    queue_services.close_elsewhere(token)


# --- Printing the doctor's prescription -------------------------------------------------------

PRINT_SESSION_KEY = "sd_print_rx"


def _prints_at(organization, place) -> bool:
    return flow_settings_for(organization).rx_print_at in (place, RxPrintAt.BOTH)


def _queue_print(request, rx) -> None:
    """The next full page (or till cart) this person sees prints it ({% pending_print %})."""
    request.session[PRINT_SESSION_KEY] = rx.pk


def print_at_doctor(request, rx) -> bool:
    """The doctor signed: print on this desk when the hospital wants it."""
    if rx is None or rx.is_draft or not rx.items.exists():
        return False
    if not _prints_at(request.organization, RxPrintAt.DOCTOR):
        return False
    _queue_print(request, rx)
    return True


def print_at_pharmacy(request, rx) -> bool:
    """The pharmacy opened or attached a sent prescription: print it there, once."""
    if rx is None or not rx.sent_to_pharmacy_at or rx.pharmacy_printed_at:
        return False
    if not _prints_at(request.organization, RxPrintAt.PHARMACY):
        return False
    from apps.prescriptions.models import Prescription

    marked = Prescription.objects.filter(pk=rx.pk, pharmacy_printed_at__isnull=True).update(
        pharmacy_printed_at=timezone.now()
    )
    if marked:  # two pharmacists opening it at once print it once
        _queue_print(request, rx)
    return bool(marked)


def pop_pending_print(request):
    session = getattr(request, "session", None)
    return session.pop(PRINT_SESSION_KEY, None) if session is not None else None


# --- Fail-safe: counters take a patient by token ----------------------------------------------


@transaction.atomic
def pull_by_token(*, request, label, to) -> PatientVisit:
    """The doctor saw the patient but never sent them on. Pharmacy or lab takes them by token;
    the doctor's queue is closed for them and the take is audit-logged."""
    if to not in PULL_PERMISSION:
        raise FlowError("Only the pharmacy or the lab can take a patient by token.")
    if PULL_PERMISSION[to] not in request.sd_perms:
        raise FlowError(f"You are not allowed to take patients at {CounterLocation(to).label}.")
    visit = _lock(find_token_visit(request, label))
    if visit.state == VisitState.TOKEN_ISSUED:
        raise FlowError(
            f"Token {visit.token.label} has not paid at reception yet, so it has not seen the "
            "doctor."
        )
    if visit.state == PULL_STATE[to]:
        return visit
    if not can_proceed(visit.pull_by_token):
        raise FlowError(f"Token {visit.token.label} is {visit.get_state_display().lower()}.")
    _note_rx(visit, flow_settings_for(visit.organization))
    before = visit.state
    visit.pull_by_token(PULL_STATE[to])
    visit.pulled_by, visit.pulled_at, visit.pulled_to = request.user, timezone.now(), to
    _save(visit)
    _close_token(visit.token)
    audit.record(
        request,
        "flow.pull",
        visit,
        changes={"state": [before, visit.state]},
        summary=f"{visit.token.label} taken by {to}",
    )
    return visit


# --- Lab --------------------------------------------------------------------------------------


def unpaid_lab_lines(visit) -> list[str]:
    """Every lab fee of the visit still unpaid, whichever counter was meant to take it."""
    lines = [
        f"{item.description} ({item.get_billed_at_display()})"
        for item in visit.bill_items.filter(is_paid=False).filter(
            Q(kind=InvoiceItem.Kind.LAB) | Q(billed_at=CounterLocation.LAB)
        )
    ]
    for order in lab_orders(visit).select_related("invoice"):
        if order.invoice_id and order.invoice.is_outstanding:
            lines.append(f"Lab order {order.number} · {order.invoice.balance_due} due")
    return lines


def lab_guard(order) -> None:
    """Called before any lab action on an order. Refused while the visit owes any lab fee, paid
    at the lab or at central cash. Emergencies and orders with no OPD visit keep the lab's own
    rules (apps/lab/services.collect_sample)."""
    visit = visit_for_lab_order(order)
    if visit is None or visit.is_emergency:
        return
    lines = unpaid_lab_lines(visit)
    if lines:
        raise FlowError(
            f"Token {visit.token.label}: lab work is blocked until every lab fee is paid. "
            f"Unpaid: {'; '.join(lines)}."
        )


@transaction.atomic
def on_lab_order_saved(order) -> None:
    """Samples taken for every order of the visit: the lab is done with this patient."""
    from apps.lab.models import LabOrder

    visit = visit_for_lab_order(order)
    if visit is None or visit.state != VisitState.LAB_QUEUE:
        return
    if (
        lab_orders(visit)
        .filter(status__in=(LabOrder.Status.PAYMENT_PENDING, LabOrder.Status.SAMPLE_PENDING))
        .exists()
    ):
        return
    visit = _lock(visit)
    if can_proceed(visit.lab_done):
        visit.lab_done(route_after_lab(visit))
        _save(visit)


# --- Counter buttons --------------------------------------------------------------------------


@transaction.atomic
def send_on(*, request, visit) -> PatientVisit:
    """'Done here' at the lab or pharmacy desk, refused while that counter is owed money."""
    visit = _lock(visit)
    before = visit.state
    if visit.state == VisitState.LAB_QUEUE:
        if PULL_PERMISSION[CounterLocation.LAB] not in request.sd_perms:
            raise FlowError("You are not allowed to work at the lab.")
        if not can_proceed(visit.lab_done):
            raise FlowError("Lab fees are still unpaid: " + "; ".join(unpaid_lab_lines(visit)))
        visit.lab_done(route_after_lab(visit))
    elif visit.state == VisitState.PHARMACY_QUEUE:
        if PULL_PERMISSION[CounterLocation.PHARMACY] not in request.sd_perms:
            raise FlowError("You are not allowed to work at the pharmacy.")
        if not can_proceed(visit.pharmacy_done):
            raise FlowError("Something is still to pay at the pharmacy.")
        visit.pharmacy_done()
    else:
        raise FlowError(f"Token {visit.token.label} is {visit.get_state_display().lower()}.")
    _save(visit)
    audit.record(request, "flow.send_on", visit, changes={"state": [before, visit.state]})
    return visit


@transaction.atomic
def on_pharmacy_sale(sale) -> None:
    """A till sale for the patient was paid: the pharmacy queue is done for today's visits."""
    if not sale.patient_id:
        return
    for visit in _today_visits(sale.patient, sale.branch).filter(state=VisitState.PHARMACY_QUEUE):
        visit = _lock(visit)
        if can_proceed(visit.pharmacy_done):
            visit.pharmacy_done()
            _save(visit)


def board(request) -> dict:
    """Today's visits in this branch, grouped by where they are."""
    visits = (
        PatientVisit.objects.filter(
            organization=request.organization,
            branch=request.branch,
            token__date=timezone.localdate(),
        )
        .select_related("token__doctor__user", "patient")
        .prefetch_related("bill_items")
        .order_by("token__queued_at")
    )
    groups = {state: [] for state in VisitState.values}
    for visit in visits:
        # Counters with fees on no invoice yet; the pharmacy bills its own at the till.
        visit.counters_due = [
            (c, CounterLocation(c).label)
            for c in sorted(
                {
                    b.billed_at
                    for b in visit.bill_items.all()
                    if not b.is_paid
                    and not b.invoice_id
                    and b.billed_at != CounterLocation.PHARMACY
                }
            )
        ]
        groups[visit.state].append(visit)
    return groups
