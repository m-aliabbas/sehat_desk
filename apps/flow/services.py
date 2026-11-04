"""Patient flow services: every state change of a PatientVisit and PatientBillItem goes through
here. Views call the request-taking functions; apps/flow/signals.py calls the on_* functions when
the queue, billing, lab or pharmacy save something.

    reception  open_visit (token issued) → admit (reception fees paid) → IN_QUEUE
               a refund before the doctor → hold_again → TOKEN_ISSUED
    doctor     on_token_status: in consultation → WITH_DOCTOR; closed → auto_next, unless the
               hospital waits for the doctor's click (doctor_clicked_next)
    fail-safe  pull_by_token: pharmacy or lab takes a patient the doctor never sent on
    lab        lab_guard: refuses lab work on a visit while any lab fee is unpaid
    pharmacy   pharmacy_dues + bill_pharmacy_dues: one till invoice for medicine and fees
    leaving    anything still unpaid → PAYMENT_DUE until it is paid (settle)
    day end    close_stale_visits closes visits left open on earlier days
"""

import re
from collections import defaultdict
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.utils import timezone
from django_fsm import can_proceed

from apps.audit import services as audit
from apps.billing import services as billing
from apps.billing.models import Invoice, InvoiceItem
from apps.core.features import is_enabled
from apps.organizations.models import preferences_for

from .models import (
    OPEN_STATES,
    CounterLocation,
    HospitalFlowSetting,
    LabBilledAt,
    PatientBillItem,
    PatientVisit,
    PatientVisitStep,
    RxPrintAt,
    VisitState,
)

COLLECT = "billing.payment.collect"
ADD_FEE = "flow.charge.add"
# Who may take a patient by token at each counter.
PULL_PERMISSION = {
    CounterLocation.PHARMACY: "pharmacy.sale",
    CounterLocation.LAB: "lab.sample.collect",
}
PULL_STATE = {
    CounterLocation.PHARMACY: VisitState.PHARMACY_QUEUE,
    CounterLocation.LAB: VisitState.LAB_QUEUE,
}
# A refunded fee is not paid: refunding the doctor's fee puts an unseen patient back on hold.
PAID_STATUSES = (Invoice.Status.PAID,)
SETTING_FIELDS = (
    "consultation_fee_billed_at",
    "lab_tests_billed_at",
    "doctor_must_click_next",
    "doctor_generates_digital_rx",
    "rx_print_at",
    "pharmacy_handles_manual_rx",
)


class FlowError(ValidationError):
    pass


# --- Settings ---------------------------------------------------------------------------------


def _hospital_row(organization) -> HospitalFlowSetting:
    """The hospital's own row, made on first use. A new row starts from the old Preferences
    switch, so a hospital that did not collect the fee up front keeps doing so."""
    upfront = preferences_for(organization).opd_collect_fee_upfront
    row, _ = HospitalFlowSetting.objects.get_or_create(
        organization=organization,
        branch=None,
        defaults={
            "consultation_fee_billed_at": CounterLocation.RECEPTION
            if upfront
            else CounterLocation.CENTRAL_CASH
        },
    )
    return row


def flow_settings_for(organization, branch=None) -> HospitalFlowSetting:
    """The rules for this branch: its own row when it has one, else the hospital's."""
    cache = getattr(organization, "_sd_flow_settings", None)
    if not isinstance(cache, dict):
        cache = organization._sd_flow_settings = {}
    key = getattr(branch, "pk", branch)
    if key not in cache:
        row = None
        if key is not None:
            row = HospitalFlowSetting.objects.filter(
                organization=organization, branch_id=key
            ).first()
        cache[key] = row or _hospital_row(organization)
    return cache[key]


def branch_rows(organization) -> dict:
    """{branch id: its own row} for branches that override the hospital's rules."""
    return {
        row.branch_id: row
        for row in HospitalFlowSetting.objects.filter(
            organization=organization, branch__isnull=False
        )
    }


def settings_row_for_edit(request, branch=None) -> HospitalFlowSetting:
    """The row the settings screen edits: the hospital's, or a branch's (copied from the
    hospital's the first time that branch is given its own rules)."""
    hospital = _hospital_row(request.organization)
    if branch is None:
        return hospital
    row = HospitalFlowSetting.objects.filter(
        organization=request.organization, branch=branch
    ).first()
    if row is None:
        row = HospitalFlowSetting(organization=request.organization, branch=branch)
        for field in SETTING_FIELDS:
            setattr(row, field, getattr(hospital, field))
    return row


@transaction.atomic
def save_settings(form, *, request, print_blank_rx: bool) -> HospitalFlowSetting:
    """Save Settings → Patient flow for the hospital or one branch. The blank Rx and 'collect
    up front' switches stay on Preferences, where the visit papers read them; the hospital's
    row keeps them in step."""
    if "admin.organization.manage" not in request.sd_perms:
        raise FlowError("You are not allowed to change hospital settings.")
    fields = list(form.Meta.fields)
    old = HospitalFlowSetting.objects.filter(pk=form.instance.pk).first()
    before = audit.snapshot(old, fields) if old else {}
    obj = form.save()
    prefs = preferences_for(request.organization)
    prefs.opd_print_blank_prescription = print_blank_rx
    if obj.branch_id is None:
        prefs.opd_collect_fee_upfront = obj.consultation_fee_billed_at == CounterLocation.RECEPTION
    prefs.save(
        update_fields=["opd_print_blank_prescription", "opd_collect_fee_upfront", "updated_at"]
    )
    request.organization._sd_flow_settings = None
    audit.record(request, "flow.settings", obj, changes=audit.diff(before, obj, fields))
    return obj


@transaction.atomic
def use_hospital_rules(*, request, branch) -> None:
    """Drop a branch's own rules; it follows the hospital's again."""
    if "admin.organization.manage" not in request.sd_perms:
        raise FlowError("You are not allowed to change hospital settings.")
    row = HospitalFlowSetting.objects.filter(organization=request.organization, branch=branch)
    if row.exists():
        audit.record(request, "flow.settings_branch_removed", row.first(), summary=str(branch))
        row.delete()
    request.organization._sd_flow_settings = None


def fee_counter(organization, branch=None) -> str:
    """Where the doctor's fee is paid."""
    return flow_settings_for(organization, branch).consultation_fee_billed_at


def _rules(visit) -> HospitalFlowSetting:
    return flow_settings_for(visit.organization, visit.branch_id)


# --- Lookups ----------------------------------------------------------------------------------


def visit_for_token(token) -> PatientVisit | None:
    try:
        return token.flow_visit
    except PatientVisit.DoesNotExist:
        return None


def visit_for_encounter(encounter) -> PatientVisit | None:
    if encounter is None or not encounter.token_id:
        return None
    return PatientVisit.objects.filter(token_id=encounter.token_id).first()


def normalize_token(label: str) -> str:
    """A-012 however it was typed or scanned: a-12, A12, A012, "a 12"."""
    label = (label or "").strip().upper()
    match = re.fullmatch(r"([A-Z]+)[\s-]*0*(\d+)", label)
    return f"{match[1]}-{int(match[2]):03d}" if match else label


def find_token_visit(request, label: str) -> PatientVisit:
    """Today's visit for a token number typed or scanned at a counter (A-012, a-12)."""
    label = normalize_token(label)
    if not label:
        raise FlowError("Type the token number, e.g. A-012.")
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
    """Save after a transition and note when the visit entered its new state."""
    now = timezone.now()
    visit.state_changed_at = now
    visit.save()
    last = visit.steps.order_by("-at", "-pk").values_list("state", flat=True).first()
    if last != visit.state:
        PatientVisitStep.objects.create(visit=visit, state=visit.state, at=now)
    return visit


def visited(visit, state) -> bool:
    return visit.steps.filter(state=state).exists()


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
    """Lab orders written in this visit's consultation. Orders made at the lab desk belong to no
    visit: they keep the lab's own payment rules."""
    from apps.lab.models import LabOrder

    return LabOrder.objects.filter(encounter__token=visit.token).exclude(
        status=LabOrder.Status.CANCELLED
    )


def visit_for_lab_order(order) -> PatientVisit | None:
    if order.encounter_id and order.encounter.token_id:
        return visit_for_token(order.encounter.token)
    return None


# --- Reception --------------------------------------------------------------------------------


@transaction.atomic
def open_visit(token) -> PatientVisit:
    """A token was issued: start tracking the visit and route the doctor's fee to its counter.
    Called from the token's post_save signal, inside the same transaction as the token."""
    if visit := visit_for_token(token):
        return visit
    from apps.queue.models import QueueToken

    org = token.organization
    rules = flow_settings_for(org, token.branch_id)
    visit = PatientVisit.objects.create(
        organization=org,
        branch=token.branch,
        token=token,
        appointment=token.appointment,
        patient=token.patient,
        is_emergency=token.priority == QueueToken.Priority.EMERGENCY,
        print_blank_rx=rules.reception_prints_blank_rx,
    )
    PatientVisitStep.objects.create(visit=visit, state=visit.state, at=timezone.now())
    appt = token.appointment
    if appt is not None and appt.fee > 0:
        PatientBillItem.objects.create(
            organization=org,
            branch=token.branch,
            visit=visit,
            kind=InvoiceItem.Kind.CONSULTATION,
            description=f"Consultation · {appt.doctor}",
            unit_price=appt.fee,
            billed_at=rules.consultation_fee_billed_at,
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


def reception_pay_link(visit) -> tuple[str, int] | None:
    """How reception takes what holds this token: ("invoice", pk) of an issued, unpaid bill
    already carrying the fee, or ("bill", visit pk) to make one."""
    item = (
        visit.bill_items.filter(is_paid=False, billed_at=CounterLocation.RECEPTION)
        .select_related("invoice")
        .first()
    )
    if item is None:
        return None
    if item.invoice_id and item.invoice.is_outstanding:
        return ("invoice", item.invoice_id)
    return ("bill", visit.pk)


def held_counts(branch, *, doctor=None) -> int:
    """Tokens waiting to pay at reception (for a doctor's queue, theirs and their department's)."""
    from apps.queue import services as queue_services

    return queue_services.held(queue_services.queue_for(branch, doctor=doctor)).count()


@transaction.atomic
def add_fee(
    *,
    request,
    visit,
    kind,
    description,
    unit_price,
    billed_at,
    quantity=1,
    service=None,
    performed_by=None,
    procedure=None,
    lab_order=None,
    invoice=None,
) -> PatientBillItem:
    """Record a fee on the visit. With an invoice, that invoice already carries it (the item
    follows its payment); without, the counter in billed_at bills it later."""
    item = PatientBillItem.objects.create(
        organization=visit.organization,
        branch=visit.branch,
        visit=visit,
        kind=kind,
        service=service,
        description=description[:160],
        quantity=quantity,
        unit_price=unit_price,
        billed_at=billed_at,
        performed_by=performed_by,
        procedure=procedure,
        lab_order=lab_order,
        invoice=invoice,
        invoice_item=None,
        collected_by_counter=billed_at if invoice else "",
        is_paid=bool(invoice and invoice.status in PAID_STATUSES),
        added_by=request.user,
    )
    return item


@transaction.atomic
def add_charge(*, request, visit, service, quantity=1) -> PatientBillItem:
    """Add a service fee (injection, dressing...) to the visit. The service's billed_at decides
    which counter takes it."""
    if ADD_FEE not in request.sd_perms:
        raise FlowError("You are not allowed to add fees to a visit.")
    if service.organization_id != request.organization.pk or not service.is_active:
        raise FlowError("That service is not available.")
    if visit.state in (VisitState.COMPLETED, VisitState.CANCELLED):
        raise FlowError(f"This visit is {visit.get_state_display().lower()}.")
    item = add_fee(
        request=request,
        visit=visit,
        kind=InvoiceItem.Kind.PROCEDURE if service.is_procedure else InvoiceItem.Kind.SERVICE,
        service=service,
        description=service.name,
        quantity=quantity,
        unit_price=service.price_for(visit.branch),
        billed_at=service.billed_at,
    )
    audit.record(request, "flow.charge", item, summary=f"{item.description} · {item.billed_at}")
    return item


# --- Fees from procedures and lab orders -----------------------------------------------------


def procedure_to_pharmacy(encounter, service) -> PatientVisit | None:
    """The visit whose pharmacy bill takes this procedure's fee, or None to invoice it as usual."""
    visit = visit_for_encounter(encounter)
    if visit is None or service.billed_at != CounterLocation.PHARMACY:
        return None
    if not is_enabled(visit.organization, "pharmacy"):
        return None
    return visit


def lab_to_pharmacy(order) -> PatientVisit | None:
    """The visit whose pharmacy bill takes this order's tests, or None for the lab's invoice."""
    visit = visit_for_lab_order(order)
    if visit is None or _rules(visit).lab_tests_billed_at != LabBilledAt.PHARMACY:
        return None
    if not is_enabled(visit.organization, "pharmacy"):
        return None
    return visit


def bill_lab_at_pharmacy(*, request, order, lines) -> bool:
    """Put an order's test fees on the visit's pharmacy bill instead of a lab invoice. Called by
    lab.create_order / edit_order with (description, price) lines. False = invoice as usual."""
    visit = lab_to_pharmacy(order)
    if visit is None:
        return False
    drop_unbilled(lab_order=order)
    for description, price in lines:
        if price > 0:
            add_fee(
                request=request,
                visit=visit,
                kind=InvoiceItem.Kind.LAB,
                description=description,
                unit_price=price,
                billed_at=CounterLocation.PHARMACY,
                performed_by=order.doctor.user if order.doctor_id else None,
                lab_order=order,
            )
    return True


def track_invoiced_fee(*, request, encounter, invoice, kind, billed_at, **source) -> None:
    """A procedure billed on its own invoice: note it on the visit too, so the board, the lab
    guard and 'Pay before leaving' see it."""
    visit = visit_for_encounter(encounter)
    if visit is None or invoice is None:
        return
    for line in invoice.items.all():
        item = add_fee(
            request=request,
            visit=visit,
            kind=kind,
            service=line.service,
            description=line.description,
            unit_price=line.unit_price,
            quantity=line.quantity,
            billed_at=billed_at,
            performed_by=line.performed_by,
            invoice=invoice,
            **source,
        )
        item.invoice_item = line
        item.save(update_fields=["invoice_item", "updated_at"])


def drop_unbilled(*, procedure=None, lab_order=None) -> int:
    """A cancelled procedure or edited lab order: fees not yet on any bill are removed. They
    were never money; billed fees stay with their invoice."""
    qs = PatientBillItem.objects.filter(invoice__isnull=True, is_paid=False)
    if procedure is not None:
        qs = qs.filter(procedure=procedure)
    elif lab_order is not None:
        qs = qs.filter(lab_order=lab_order)
    else:
        return 0
    return qs.delete()[0]


# --- Billing aggregation engine ---------------------------------------------------------------


def unbilled(visit_or_visits, counter):
    """Fees for this counter that no invoice carries yet."""
    qs = PatientBillItem.objects.filter(is_paid=False, invoice__isnull=True, billed_at=counter)
    if isinstance(visit_or_visits, PatientVisit):
        return qs.filter(visit=visit_or_visits)
    return qs.filter(visit__in=visit_or_visits)


def collect_lines(invoice, *, request, items, counter) -> list[InvoiceItem]:
    """Put each fee on a draft invoice and remember which counter took it. A procedure or lab
    order whose fee this is now points at this invoice, so its own screens show it paid."""
    from apps.lab.models import LabOrder
    from apps.procedures.models import PatientProcedure

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
        if item.procedure_id:
            PatientProcedure.objects.filter(pk=item.procedure_id).update(invoice=invoice)
        if item.lab_order_id:
            LabOrder.objects.filter(pk=item.lab_order_id).update(invoice=invoice)
        lines.append(line)
    return lines


@transaction.atomic
def counter_invoice(*, request, visit, counter) -> Invoice:
    """One issued invoice for everything this visit owes at this counter (reception, lab,
    central cash). It is paid on the normal Collect payment screen."""
    if COLLECT not in request.sd_perms:
        raise FlowError("You are not allowed to collect payments.")
    if counter not in CounterLocation.values:
        raise FlowError("Choose a counter.")
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
    the pharmacy and on no invoice yet (doctor's fee, procedure, injection, lab tests...)."""
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
    """An invoice was saved (signal). Its fees follow it: paid, unpaid again after a refund, or
    free again if cancelled. The visit then moves if the payment was what it waited for."""
    if invoice.appointment_id:
        _link_fee_invoice(invoice)
    items = list(PatientBillItem.objects.filter(invoice=invoice).select_related("visit"))
    if not items:
        return
    now = timezone.now()
    paid = invoice.status in PAID_STATUSES
    for item in items:
        if invoice.status == Invoice.Status.CANCELLED and item.visit.state == VisitState.CANCELLED:
            item.delete()  # the visit was cancelled: it owes nothing (fees are not money)
            continue
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
    at_pharmacy = {i.visit_id for i in items if i.collected_by_counter == CounterLocation.PHARMACY}
    for visit in {item.visit for item in items}:
        _after_payment(_lock(visit), pharmacy_bill=visit.pk in at_pharmacy)


def _after_payment(visit, *, pharmacy_bill=False) -> None:
    if visit.state == VisitState.IN_QUEUE and can_proceed(visit.hold_again):
        visit.hold_again()
        _save(visit)
    _try_admit(visit)
    if (
        pharmacy_bill
        and visit.state == VisitState.PHARMACY_QUEUE
        and can_proceed(visit.pharmacy_done)
    ):
        if not unbilled(visit, CounterLocation.PHARMACY).exists():
            visit.pharmacy_done(route_after_pharmacy(visit))
            _save(visit)
    if visit.state == VisitState.PAYMENT_DUE and can_proceed(visit.settle):
        visit.settle()
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


# --- Routing ----------------------------------------------------------------------------------


def _lab_pending(visit) -> bool:
    from apps.lab.models import LabOrder

    if not is_enabled(visit.organization, "lab"):
        return False
    return (
        lab_orders(visit)
        .filter(status__in=(LabOrder.Status.PAYMENT_PENDING, LabOrder.Status.SAMPLE_PENDING))
        .exists()
    )


def _pharmacy_needed(visit) -> bool:
    """Something waits at the pharmacy: a fee to pay there, or medicine not yet collected."""
    if not is_enabled(visit.organization, "pharmacy"):
        return False
    if unbilled(visit, CounterLocation.PHARMACY).exists():
        return True
    if visited(visit, VisitState.PHARMACY_QUEUE):
        return False
    if visit.digital_rx_generated:
        return True
    return visit.is_manual_rx_flagged and _rules(visit).pharmacy_handles_manual_rx


def _finish(visit) -> str:
    """Nothing left to do anywhere: leave, or pay first if anything is still owed."""
    return VisitState.PAYMENT_DUE if visit.unpaid().exists() else VisitState.COMPLETED


def _note_rx(visit) -> None:
    """Digital or paper. No consultation note at all means the doctor wrote on paper; a note
    with no medicines is a paper prescription only where doctors do not write in SehatDesk."""
    visit.digital_rx_generated = _has_digital_rx(visit)
    visit.is_manual_rx_flagged = not visit.digital_rx_generated and (
        _encounter(visit) is None or not _rules(visit).doctor_generates_digital_rx
    )


def route_after_doctor(visit) -> str:
    """Lab first, unless its tests are paid at the pharmacy: then the pharmacy first."""
    _note_rx(visit)
    if _lab_pending(visit):
        if unbilled(visit, CounterLocation.PHARMACY).filter(lab_order__isnull=False).exists():
            return VisitState.PHARMACY_QUEUE
        return VisitState.LAB_QUEUE
    if _pharmacy_needed(visit):
        return VisitState.PHARMACY_QUEUE
    return _finish(visit)


def route_after_lab(visit) -> str:
    return VisitState.PHARMACY_QUEUE if _pharmacy_needed(visit) else _finish(visit)


def route_after_pharmacy(visit) -> str:
    return VisitState.LAB_QUEUE if _lab_pending(visit) else _finish(visit)


# --- Doctor -----------------------------------------------------------------------------------


@transaction.atomic
def on_token_status(token) -> None:
    """The queue moved the token (signal). A closed token sends the patient on, unless the
    hospital waits for the doctor's own click (doctor_clicked_next)."""
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
        if can_proceed(visit.auto_next):
            visit.auto_next(route_after_doctor(visit))
            _save(visit)


@transaction.atomic
def doctor_clicked_next(request, token) -> PatientVisit | None:
    """The doctor pressed Checked · Next (or Sign & next): the patient goes on."""
    visit = visit_for_token(token) if token is not None else None
    if visit is None:
        return None
    visit = _lock(visit)
    if visit.state == VisitState.IN_QUEUE:
        visit.start_consult()
        _save(visit)
    if visit.state == VisitState.WITH_DOCTOR:
        visit.doctor_next(route_after_doctor(visit))
        _save(visit)
    return visit


def _close_token(token) -> None:
    from apps.queue import services as queue_services

    queue_services.close_elsewhere(token)


# --- Printing the doctor's prescription -------------------------------------------------------

PRINT_SESSION_KEY = "sd_print_rx"


def _prints_at(request, place) -> bool:
    return flow_settings_for(request.organization, request.branch).rx_print_at in (
        place,
        RxPrintAt.BOTH,
    )


def _queue_print(request, rx) -> None:
    """The next full page (or till cart) this person sees prints it ({% pending_print %})."""
    request.session[PRINT_SESSION_KEY] = rx.pk


def print_at_doctor(request, rx) -> bool:
    """The doctor signed: print on this desk when the hospital wants it."""
    if rx is None or rx.is_draft or not rx.items.exists():
        return False
    if not _prints_at(request, RxPrintAt.DOCTOR):
        return False
    _queue_print(request, rx)
    return True


def print_at_pharmacy(request, rx) -> bool:
    """The pharmacy opened or attached a sent prescription: print it there, once."""
    if rx is None or not rx.sent_to_pharmacy_at or rx.pharmacy_printed_at:
        return False
    if not _prints_at(request, RxPrintAt.PHARMACY):
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
    _note_rx(visit)
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

URGENT = ("urgent", "stat")


def unpaid_lab_lines(visit) -> list[str]:
    """Every lab fee of the visit still unpaid, whichever counter was meant to take it."""
    from django.db.models import Q

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


def lab_guard(order, *, collecting=False, reason="", request=None) -> None:
    """Called before lab work on an order. Refused while the visit owes any lab fee, paid at
    the lab, central cash or pharmacy. Emergencies and lab-desk orders keep the lab's own rules.
    An urgent or STAT order may go ahead: at collection with a written reason (audit-logged),
    and after that without asking again."""
    visit = visit_for_lab_order(order)
    if visit is None or visit.is_emergency:
        return
    lines = unpaid_lab_lines(visit)
    if not lines:
        return
    if order.priority in URGENT:
        if not collecting:
            return
        if reason.strip():
            if request is not None:
                audit.record(
                    request,
                    "lab.unpaid_override",
                    order,
                    summary=f"{order.number} · {reason.strip()[:120]}",
                )
            return
    message = (
        f"Token {visit.token.label}: lab work is blocked until every lab fee is paid. "
        f"Unpaid: {'; '.join(lines)}."
    )
    if order.priority in URGENT:
        message += " For this urgent order, give a reason to collect anyway."
    raise FlowError(message)


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
    """'Done here' at the lab or pharmacy, refused while that counter is owed money."""
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
        if (
            not can_proceed(visit.pharmacy_done)
            or unbilled(visit, CounterLocation.PHARMACY).exists()
        ):
            raise FlowError("Something is still to pay at the pharmacy.")
        visit.pharmacy_done(route_after_pharmacy(visit))
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
        if (
            can_proceed(visit.pharmacy_done)
            and not unbilled(visit, CounterLocation.PHARMACY).exists()
        ):
            visit.pharmacy_done(route_after_pharmacy(visit))
            _save(visit)


# --- Day end ----------------------------------------------------------------------------------


@transaction.atomic
def close_stale_visits(*, organization=None, today=None) -> int:
    """Visits still open from an earlier day are closed, so every queue starts the day clean.
    Unpaid fees stay on record (board, Patient flow times report). Runs on the first board view
    of a day and from `manage.py close_flow_day`."""
    today = today or timezone.localdate()
    qs = PatientVisit.objects.filter(state__in=OPEN_STATES, token__date__lt=today)
    if organization is not None:
        qs = qs.filter(organization=organization)
    closed = 0
    for visit in qs.select_for_update(of=("self",)):
        visit.close_day()
        visit.closed_at_day_end = True
        _save(visit)
        closed += 1
    return closed


# --- Screens ----------------------------------------------------------------------------------


def token_payments(request, label: str) -> dict:
    """Everything a token's patient was billed today in this branch and what is still owed:
    issued invoices (doctor's fee, lab, procedures, pharmacy) and fees no invoice carries yet.
    Pharmacy, lab and doctors look a patient up by the token on the slip (M-2, M002...)."""
    from apps.core.dates import on_day

    visit = find_token_visit(request, label)
    invoices = list(
        Invoice.objects.filter(
            organization=request.organization,
            branch=request.branch,
            patient=visit.patient,
            **on_day("created_at", timezone.localdate()),
        )
        .exclude(status__in=(Invoice.Status.DRAFT, Invoice.Status.CANCELLED))
        .prefetch_related("items")
        .order_by("created_at", "pk")
    )
    for inv in invoices:
        inv.kinds = sorted({i.get_kind_display() for i in inv.items.all()})
    waiting = list(visit.bill_items.filter(is_paid=False, invoice__isnull=True).order_by("pk"))
    billed = sum((i.grand_total for i in invoices), 0)
    paid = sum((i.amount_paid for i in invoices), 0)
    due = sum((i.balance_due for i in invoices), 0) + sum((b.amount for b in waiting), 0)
    return {
        "visit": visit,
        "invoices": invoices,
        "waiting": waiting,
        "billed": billed,
        "paid": paid,
        "due": due,
        "lab_orders": list(lab_orders(visit).only("pk", "number", "status")),
    }


def _counters_due(visit) -> list[tuple[str, str]]:
    """Counters with fees on no invoice yet; the pharmacy bills its own at the till."""
    return [
        (c, CounterLocation(c).label)
        for c in sorted(
            {
                b.billed_at
                for b in visit.bill_items.all()
                if not b.is_paid and not b.invoice_id and b.billed_at != CounterLocation.PHARMACY
            }
        )
    ]


def attach_lab_dues(visits) -> None:
    """Set `lab_dues` on each visit: (order number, invoice) for lab orders billed on their own
    invoice and not yet paid. They are money the visit owes but carry no PatientBillItem, so
    screens add them to what is still to pay. One query for the whole list."""
    from apps.lab.models import LabOrder

    by_token = {v.token_id: v for v in visits}
    for v in visits:
        v.lab_dues = []
    if not by_token:
        return
    orders = (
        LabOrder.objects.filter(encounter__token_id__in=by_token, invoice__isnull=False)
        .exclude(status=LabOrder.Status.CANCELLED)
        .select_related("invoice", "encounter")
        .order_by("pk")
    )
    for order in orders:
        visit = by_token[order.encounter.token_id]
        if not order.invoice.is_outstanding:
            continue
        if any(b.invoice_id == order.invoice_id for b in visit.bill_items.all()):
            continue  # already listed as a fee
        visit.lab_dues.append((order.number, order.invoice))


def todays_visits(request, *states):
    qs = (
        PatientVisit.objects.filter(
            organization=request.organization,
            branch=request.branch,
            token__date=timezone.localdate(),
        )
        .select_related("token__doctor__user", "patient")
        .prefetch_related("bill_items")
        .order_by("token__queued_at")
    )
    return qs.filter(state__in=states) if states else qs


def board(request) -> dict:
    """Today's visits in this branch, grouped by where they are."""
    groups = {state: [] for state in VisitState.values}
    visits = list(todays_visits(request))
    attach_lab_dues(visits)
    for visit in visits:
        visit.counters_due = _counters_due(visit)
        groups[visit.state].append(visit)
    return groups


def counter_queue(request, state) -> list[PatientVisit]:
    """The pharmacy's or lab's own 'Waiting here' list."""
    visits = list(todays_visits(request, state))
    attach_lab_dues(visits)
    for visit in visits:
        visit.counters_due = _counters_due(visit)
        visit.unpaid_total = sum((b.amount for b in visit.bill_items.all() if not b.is_paid), 0)
        visit.unpaid_total += sum((inv.balance_due for _, inv in visit.lab_dues), 0)
        if state == VisitState.LAB_QUEUE:
            visit.lab_orders = list(lab_orders(visit).only("pk", "number"))
    return visits


def flow_times(organization, branches, start_dt, end_dt, *, doctor=None) -> list[dict]:
    """Patient flow times report: average minutes in each state, per day and doctor."""
    visits = PatientVisit.objects.filter(
        organization=organization,
        branch__in=branches,
        created_at__gte=start_dt,
        created_at__lt=end_dt,
    )
    if doctor is not None:
        visits = visits.filter(token__doctor=doctor)
    steps = defaultdict(list)
    for visit_id, state, at in PatientVisitStep.objects.filter(visit__in=visits).values_list(
        "visit_id", "state", "at"
    ):
        steps[visit_id].append((at, state))
    unpaid_ids = set(
        PatientBillItem.objects.filter(visit__in=visits, is_paid=False).values_list(
            "visit_id", flat=True
        )
    )
    rows = {}
    for visit in visits.select_related("token__doctor__user"):
        day = timezone.localtime(visit.created_at).date()
        name = str(visit.token.doctor) if visit.token.doctor_id else "Any doctor"
        row = rows.setdefault(
            (day, name),
            {"day": day, "doctor": name, "visits": 0, "unpaid_closed": 0,
             "sums": defaultdict(float), "counts": defaultdict(int)},
        )  # fmt: skip
        row["visits"] += 1
        row["unpaid_closed"] += int(visit.closed_at_day_end and visit.pk in unpaid_ids)
        timeline = sorted(steps.get(visit.pk, []))
        for (at, state), (next_at, _) in zip(timeline, timeline[1:], strict=False):
            # Waiting for the doctor counts from the token, whether or not it paid first.
            key = VisitState.IN_QUEUE if state == VisitState.TOKEN_ISSUED else state
            row["sums"][key] += (next_at - at) / timedelta(minutes=1)
            row["counts"][key] += 1
    out = []
    for row in sorted(rows.values(), key=lambda r: (r["day"], r["doctor"])):
        for state in (
            VisitState.IN_QUEUE,
            VisitState.WITH_DOCTOR,
            VisitState.LAB_QUEUE,
            VisitState.PHARMACY_QUEUE,
            VisitState.PAYMENT_DUE,
        ):
            n = row["counts"][state]
            row[state.lower()] = round(row["sums"][state] / n) if n else None
        out.append(row)
    return out
