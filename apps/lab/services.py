"""Laboratory workflow. Views call these; they never change statuses or results directly.

Billing: creating an order issues an invoice with one line per test or panel (kind "lab"), so
the patient pays at any billing desk. Samples are collected once the invoice is paid; urgent
and STAT orders, or a recorded reason, allow collection before payment.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Invoice, InvoiceItem
from apps.core.models import next_number

from .models import LabOrder, LabOrderItem, LabResult, LabSample

Status = LabOrder.Status
ItemStatus = LabOrderItem.Status
Flag = LabResult.Flag


class LabError(ValidationError):
    pass


def _need(request, code, action):
    if code not in request.sd_perms:
        raise LabError(f"You are not allowed to {action}.")


# --- Orders ------------------------------------------------------------------------------------


@transaction.atomic
def create_order(
    *,
    request,
    patient,
    tests=(),
    panels=(),
    priority=LabOrder.Priority.ROUTINE,
    clinical_notes="",
    encounter=None,
    doctor=None,
    source=LabOrder.Source.DESK,
) -> LabOrder:
    _need(request, "lab.order.create", "create lab orders")
    org = request.organization
    tests, panels = list(tests), list(panels)
    if not tests and not panels:
        raise LabError("Choose at least one test or panel.")
    for obj in [*tests, *panels]:
        if obj.organization_id != org.id or not obj.is_active:
            raise LabError(f"{obj} is not available.")
    if encounter is not None:
        if encounter.patient_id != patient.pk:
            raise LabError("That consultation belongs to another patient.")
        doctor = doctor or encounter.doctor
    year = timezone.localdate().year
    order = LabOrder.objects.create(
        organization=org,
        branch=request.branch,
        number=f"LAB-{year}-{next_number(org, f'lab:{year}'):05d}",
        patient=patient,
        encounter=encounter,
        doctor=doctor,
        source=source,
        priority=priority,
        clinical_notes=clinical_notes.strip()[:300],
        created_by=request.user,
    )
    seen = set()
    lines = []  # (description, price) for the invoice
    for panel in panels:
        lines.append((f"{panel.name} (panel)", panel.price))
        for test in panel.tests.filter(is_active=True):
            if test.pk not in seen:
                seen.add(test.pk)
                _add_item(order, test, panel=panel, price=Decimal("0"))
    for test in tests:
        if test.pk in seen:
            continue  # already ordered as part of a panel
        seen.add(test.pk)
        _add_item(order, test, price=test.price)
        lines.append((f"{test.name} ({test.code})", test.price))
    if not seen:
        raise LabError("The chosen panels have no active tests.")
    _create_samples(order)
    if any(price > 0 for _, price in lines):
        invoice = billing.create_invoice(
            request=request, patient=patient, notes=f"Laboratory order {order.number}"
        )
        for description, price in lines:
            billing.add_item(
                invoice,
                request=request,
                kind=InvoiceItem.Kind.LAB,
                description=description,
                unit_price=price,
                performed_by=doctor.user if doctor else None,  # referring doctor
            )
        billing.issue(invoice, request=request)
        order.invoice = invoice
        order.save(update_fields=["invoice", "updated_at"])
    return refresh_status(order)


def _add_item(order, test, *, panel=None, price):
    return LabOrderItem.objects.create(
        order=order, test=test, panel=panel, test_name=test.name, unit=test.unit, price=price
    )


def _accession(org) -> str:
    day = timezone.localdate()
    return f"S{day:%y%m%d}-{next_number(org, f'sample:{day:%Y%m%d}'):04d}"


def _new_sample(order, sample_type, recollection_of=None) -> LabSample:
    return LabSample.objects.create(
        organization=order.organization,
        branch=order.branch,
        order=order,
        sample_type=sample_type,
        accession=_accession(order.organization),
        recollection_of=recollection_of,
    )


def _create_samples(order):
    """One tube/container per sample type."""
    by_type = {}
    for item in order.items.select_related("test"):
        sample = by_type.get(item.test.sample_type)
        if sample is None:
            sample = by_type[item.test.sample_type] = _new_sample(order, item.test.sample_type)
        item.sample = sample
        item.save(update_fields=["sample", "updated_at"])


def _active_items(order):
    return order.items.exclude(status=ItemStatus.CANCELLED)


def refresh_status(order) -> LabOrder:
    """Derive the order status from its items, samples and invoice."""
    order = LabOrder.objects.select_related("invoice").get(pk=order.pk)
    if order.cancelled_at:
        status = Status.CANCELLED
    elif order.released_at:
        status = Status.RELEASED
    else:
        states = set(_active_items(order).values_list("status", flat=True))
        if states and states == {ItemStatus.VERIFIED}:
            status = Status.VERIFIED
        elif states and states <= {ItemStatus.RESULT_ENTERED, ItemStatus.VERIFIED}:
            status = Status.RESULT_ENTERED
        elif states & {ItemStatus.PROCESSING, ItemStatus.RESULT_ENTERED, ItemStatus.VERIFIED}:
            status = Status.PROCESSING
        elif states and ItemStatus.PENDING not in states:
            status = Status.SAMPLE_COLLECTED
        elif not order.is_paid and order.invoice.status in Invoice.OUTSTANDING_STATUSES:
            status = Status.PAYMENT_PENDING
        else:
            status = Status.SAMPLE_PENDING
    has_critical = LabResult.objects.filter(
        item__order=order, is_current=True, flag__in=LabResult.CRITICAL_FLAGS
    ).exists()
    if (status, has_critical) != (order.status, order.has_critical):
        order.status, order.has_critical = status, has_critical
        order.save(update_fields=["status", "has_critical", "updated_at"])
    return order


def refresh_for_invoice(invoice) -> None:
    """Called after a payment, so lab orders leave "payment pending"."""
    for order in LabOrder.objects.filter(invoice=invoice, status=Status.PAYMENT_PENDING):
        refresh_status(order)


@transaction.atomic
def cancel_order(order, *, request, reason: str) -> LabOrder:
    _need(request, "lab.order.create", "cancel lab orders")
    if not reason.strip():
        raise LabError("Give the reason for cancelling.")
    order = LabOrder.objects.select_for_update(of=("self",)).get(pk=order.pk)
    if not order.is_open:
        raise LabError(f"{order.number} is {order.get_status_display().lower()}.")
    if order.items.filter(status=ItemStatus.VERIFIED).exists():
        raise LabError("Verified results cannot be cancelled. Amend them instead.")
    if order.invoice_id:
        invoice = order.invoice
        if invoice.net_paid > 0:
            raise LabError(
                f"{invoice.number} has payments. Refund them in Billing first, then cancel."
            )
        if invoice.status != Invoice.Status.CANCELLED:
            billing.void_unpaid(invoice, user=request.user, reason=f"{order.number}: {reason}")
    order.items.update(status=ItemStatus.CANCELLED, updated_at=timezone.now())
    order.cancelled_at = timezone.now()
    order.cancelled_by = request.user
    order.cancel_reason = reason.strip()[:200]
    order.save(update_fields=["cancelled_at", "cancelled_by", "cancel_reason", "updated_at"])
    return refresh_status(order)


# --- Samples -----------------------------------------------------------------------------------


@transaction.atomic
def collect_sample(sample, *, request, unpaid_reason="") -> LabSample:
    _need(request, "lab.sample.collect", "collect samples")
    sample = (
        LabSample.objects.select_for_update(of=("self",))
        .select_related("order__invoice")
        .get(pk=sample.pk)
    )
    order = sample.order
    if not order.is_open:
        raise LabError(f"{order.number} is {order.get_status_display().lower()}.")
    if sample.status != LabSample.Status.PENDING:
        raise LabError(
            f"Sample {sample.accession} is already {sample.get_status_display().lower()}."
        )
    urgent = order.priority in (LabOrder.Priority.URGENT, LabOrder.Priority.STAT)
    if not order.is_paid and not urgent and not unpaid_reason.strip():
        raise LabError("The invoice is not paid. Collect payment first, or give a reason.")
    sample.status = LabSample.Status.COLLECTED
    sample.collected_at = timezone.now()
    sample.collected_by = request.user
    sample.save(update_fields=["status", "collected_at", "collected_by", "updated_at"])
    sample.items.filter(status=ItemStatus.PENDING).update(
        status=ItemStatus.COLLECTED, updated_at=timezone.now()
    )
    refresh_status(order)
    return sample


@transaction.atomic
def reject_sample(sample, *, request, reason: str) -> LabSample:
    """Reject a collected (or unusable) sample and open a new one for recollection."""
    _need(request, "lab.sample.collect", "reject samples")
    if not reason.strip():
        raise LabError("Give the rejection reason, e.g. haemolysed or clotted.")
    sample = LabSample.objects.select_for_update(of=("self",)).get(pk=sample.pk)
    if sample.status == LabSample.Status.REJECTED:
        raise LabError("This sample was already rejected.")
    if sample.items.filter(status__in=[ItemStatus.RESULT_ENTERED, ItemStatus.VERIFIED]).exists():
        raise LabError("Results were entered from this sample. Amend the results instead.")
    sample.status = LabSample.Status.REJECTED
    sample.rejected_at = timezone.now()
    sample.rejected_by = request.user
    sample.rejection_reason = reason.strip()[:200]
    sample.save(
        update_fields=["status", "rejected_at", "rejected_by", "rejection_reason", "updated_at"]
    )
    new = _new_sample(sample.order, sample.sample_type, recollection_of=sample)
    sample.items.exclude(status=ItemStatus.CANCELLED).update(
        sample=new, status=ItemStatus.PENDING, updated_at=timezone.now()
    )
    refresh_status(sample.order)
    return new


# --- Processing and results --------------------------------------------------------------------


@transaction.atomic
def start_processing(order, *, request) -> int:
    _need(request, "lab.result.enter", "process samples")
    n = order.items.filter(status=ItemStatus.COLLECTED).update(
        status=ItemStatus.PROCESSING, updated_at=timezone.now()
    )
    refresh_status(order)
    return n


def compute_flag(test, patient, *, numeric=None, text=""):
    """(flag, reference text) for a result value."""
    if test.result_type == test.ResultType.NUMERIC:
        rng = test.range_for(patient)
        ref = rng.label if rng else ""
        if numeric is None or rng is None:
            return Flag.NONE, ref
        if rng.critical_low is not None and numeric < rng.critical_low:
            return Flag.CRITICAL_LOW, ref
        if rng.critical_high is not None and numeric > rng.critical_high:
            return Flag.CRITICAL_HIGH, ref
        if rng.low is not None and numeric < rng.low:
            return Flag.LOW, ref
        if rng.high is not None and numeric > rng.high:
            return Flag.HIGH, ref
        return Flag.NORMAL, ref
    if test.normal_value and text:
        normal = text.strip().lower() == test.normal_value.strip().lower()
        return (Flag.NORMAL if normal else Flag.ABNORMAL), test.normal_value
    return Flag.NONE, test.normal_value


def _parse_value(test, raw: str):
    raw = (raw or "").strip()
    if not raw:
        raise LabError(f"Enter a result for {test.name}.")
    if test.result_type == test.ResultType.NUMERIC:
        try:
            return Decimal(raw.replace(",", "")), ""
        except ArithmeticError as err:
            raise LabError(f"{test.name}: enter a number.") from err
    if test.result_type in (test.ResultType.POSNEG, test.ResultType.CHOICE):
        options = {o.lower(): o for o in test.option_list}
        if raw.lower() not in options:
            raise LabError(f"{test.name}: choose one of {', '.join(test.option_list)}.")
        return None, options[raw.lower()]
    return None, raw[:4000]


ENTERABLE = (ItemStatus.COLLECTED, ItemStatus.PROCESSING, ItemStatus.RESULT_ENTERED)


@transaction.atomic
def enter_result(item, *, request, value: str, interpretation="") -> LabResult:
    """Enter or correct an unverified result. Verified results go through amend()."""
    _need(request, "lab.result.enter", "enter lab results")
    item = (
        LabOrderItem.objects.select_for_update(of=("self",))
        .select_related("test", "order__patient")
        .get(pk=item.pk)
    )
    if item.status == ItemStatus.VERIFIED:
        raise LabError(f"{item.test_name} is verified. Amend it to change the result.")
    if item.status not in ENTERABLE:
        raise LabError(f"{item.test_name}: the sample has not been collected.")
    numeric, text = _parse_value(item.test, value)
    flag, ref = compute_flag(item.test, item.order.patient, numeric=numeric, text=text)
    current = item.results.filter(is_current=True).first()
    fields = {
        "value_numeric": numeric,
        "value_text": text,
        "interpretation": interpretation.strip()[:4000],
        "flag": flag,
        "reference_text": ref[:120],
        "unit": item.unit,
        "entered_by": request.user,
        "entered_at": timezone.now(),
    }
    if current is not None and current.verified_at is None:
        for k, v in fields.items():
            setattr(current, k, v)
        current.save()
        result = current
    else:
        result = LabResult.objects.create(item=item, version=1, **fields)
    item.status = ItemStatus.RESULT_ENTERED
    item.save(update_fields=["status", "updated_at"])
    refresh_status(item.order)
    return result


@transaction.atomic
def verify(items, *, request) -> int:
    """Verify entered results. Returns how many were verified."""
    _need(request, "lab.result.verify", "verify lab results")
    count = 0
    now = timezone.now()
    orders = set()
    for item in items:
        item = LabOrderItem.objects.select_for_update(of=("self",)).get(pk=item.pk)
        if item.status != ItemStatus.RESULT_ENTERED:
            continue
        result = item.results.get(is_current=True)
        result.verified_by, result.verified_at = request.user, now
        result.save(update_fields=["verified_by", "verified_at", "updated_at"])
        item.status = ItemStatus.VERIFIED
        item.save(update_fields=["status", "updated_at"])
        orders.add(item.order_id)
        count += 1
    for order in LabOrder.objects.filter(pk__in=orders):
        refresh_status(order)
    return count


@transaction.atomic
def release(order, *, request) -> LabOrder:
    _need(request, "lab.result.verify", "release lab reports")
    order = refresh_status(LabOrder.objects.select_for_update(of=("self",)).get(pk=order.pk))
    if order.status != Status.VERIFIED:
        raise LabError("Verify every result before releasing the report.")
    order.released_at = timezone.now()
    order.released_by = request.user
    order.save(update_fields=["released_at", "released_by", "updated_at"])
    return refresh_status(order)


@transaction.atomic
def amend(item, *, request, value: str, interpretation="", reason: str) -> LabResult:
    """Correct a verified result: a new version, back to verification. The old one is kept."""
    _need(request, "lab.result.verify", "amend verified results")
    if not reason.strip():
        raise LabError("Give the reason for the amendment.")
    item = (
        LabOrderItem.objects.select_for_update(of=("self",))
        .select_related("test", "order__patient")
        .get(pk=item.pk)
    )
    if item.status != ItemStatus.VERIFIED:
        raise LabError("Only verified results are amended. Edit unverified results directly.")
    numeric, text = _parse_value(item.test, value)
    flag, ref = compute_flag(item.test, item.order.patient, numeric=numeric, text=text)
    last = item.results.aggregate(m=Max("version"))["m"] or 0
    item.results.filter(is_current=True).update(is_current=False, updated_at=timezone.now())
    result = LabResult.objects.create(
        item=item,
        version=last + 1,
        value_numeric=numeric,
        value_text=text,
        interpretation=interpretation.strip()[:4000],
        flag=flag,
        reference_text=ref[:120],
        unit=item.unit,
        entered_by=request.user,
        amendment_reason=reason.strip()[:300],
    )
    item.status = ItemStatus.RESULT_ENTERED
    item.save(update_fields=["status", "updated_at"])
    order = item.order
    order.amendment_count += 1
    order.released_at = None  # the amended report must be verified and released again
    order.released_by = None
    order.save(update_fields=["amendment_count", "released_at", "released_by", "updated_at"])
    refresh_status(order)
    return result


@transaction.atomic
def acknowledge_critical(result, *, request) -> LabResult:
    """A clinician confirms they have seen a critical result."""
    if not ({"consultation.view", "lab.result.verify"} & set(request.sd_perms)):
        raise LabError("You are not allowed to acknowledge critical results.")
    if not result.is_critical:
        raise LabError("This result is not critical.")
    if result.critical_acknowledged_at is None:
        result.critical_acknowledged_by = request.user
        result.critical_acknowledged_at = timezone.now()
        result.save(
            update_fields=["critical_acknowledged_by", "critical_acknowledged_at", "updated_at"]
        )
    return result


def unacknowledged_critical(organization, *, doctor=None, branch=None):
    qs = LabResult.objects.filter(
        item__order__organization=organization,
        is_current=True,
        flag__in=LabResult.CRITICAL_FLAGS,
        critical_acknowledged_at__isnull=True,
    ).select_related("item__order__patient", "item__test")
    if doctor is not None:
        qs = qs.filter(item__order__doctor=doctor)
    if branch is not None:
        qs = qs.filter(item__order__branch=branch)
    return qs.order_by("-entered_at")
