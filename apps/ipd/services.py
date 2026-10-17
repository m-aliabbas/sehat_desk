"""Inpatient operations. Views call these; they never change bed status or money directly.

Bed changes run in a transaction that locks the bed row (select_for_update: a real row lock on
PostgreSQL, a no-op on SQLite, which serializes writers anyway). The open-occupancy unique
constraints are the final guard: if two admissions race for one bed, the second one fails.

Money: deposits are recorded on AdmissionDeposit when taken. When the final bill is issued,
each deposit is applied to it as an ordinary billing Payment (method and reference kept), and
any amount left over is returned and recorded. For the cashier's day totals
(billing.services.day_totals) a deposit is cash in on the day it is taken and a returned balance
is money out on the day it is returned; the applied-deposit Payments (idempotency key
billing.DEPOSIT_PAYMENT_KEY_PREFIX + deposit id) are not counted again. Bed charges are posted
from the occupancy ledger when the bill is finalized.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Invoice, InvoiceItem
from apps.core.models import next_number

from .models import (
    Admission,
    AdmissionDeposit,
    Bed,
    BedOccupancy,
    BedTransfer,
    DischargeSummary,
    DoctorRound,
    MedicationOrder,
    Room,
    Ward,
)

ZERO = Decimal("0.00")
Status = Admission.Status


class IpdError(ValidationError):
    pass


def _need(request, code, action):
    if code not in request.sd_perms:
        raise IpdError(f"You are not allowed to {action}.")


# --- Facility ----------------------------------------------------------------------------------


@transaction.atomic
def add_beds(room: Room, *, prefix: str, count: int, start: int = 1) -> list[Bed]:
    """Create `count` beds labelled prefix-01, prefix-02 ... skipping labels already used."""
    if not 1 <= count <= 100:
        raise IpdError("Add between 1 and 100 beds at a time.")
    prefix = prefix.strip().upper()[:12]
    if not prefix:
        raise IpdError("Give a bed label prefix, e.g. G.")
    taken = set(Bed.objects.filter(branch=room.branch).values_list("code", flat=True))
    beds, n = [], start
    while len(beds) < count:
        code = f"{prefix}-{n:02d}"
        n += 1
        if code in taken:
            continue
        beds.append(Bed(organization=room.organization, branch=room.branch, room=room, code=code))
    return Bed.objects.bulk_create(beds)


@transaction.atomic
def set_bed_status(bed: Bed, *, request, status: str, note: str = "") -> Bed:
    _need(request, "ipd.manage", "change bed status")
    bed = Bed.objects.select_for_update(of=("self",)).get(pk=bed.pk)
    if status not in Bed.MANUAL_STATUSES:
        raise IpdError("Choose a valid bed status.")
    if bed.status == Bed.Status.OCCUPIED or bed.occupancies.filter(ended_at__isnull=True).exists():
        raise IpdError(f"{bed.code} is occupied. Transfer or discharge the patient first.")
    if status in (Bed.Status.MAINTENANCE, Bed.Status.BLOCKED, Bed.Status.RESERVED) and not (
        note.strip()
    ):
        raise IpdError("Give a short note for reserved, maintenance or blocked beds.")
    bed.status = status
    bed.status_note = note.strip()[:120]
    bed.status_changed_at = timezone.now()
    bed.save(update_fields=["status", "status_note", "status_changed_at", "updated_at"])
    return bed


def occupancy(beds) -> dict:
    """Counts for the occupancy indicator. `beds` is a queryset of active beds."""
    counts = dict(beds.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    total = sum(counts.values())
    occupied = counts.get(Bed.Status.OCCUPIED, 0)
    # Beds out of service do not count towards capacity.
    out_of_service = counts.get(Bed.Status.MAINTENANCE, 0) + counts.get(Bed.Status.BLOCKED, 0)
    usable = total - out_of_service
    return {
        "total": total,
        "usable": usable,
        "occupied": occupied,
        "available": counts.get(Bed.Status.AVAILABLE, 0),
        "reserved": counts.get(Bed.Status.RESERVED, 0),
        "cleaning": counts.get(Bed.Status.CLEANING, 0),
        "out_of_service": out_of_service,
        "percent": round(occupied * 100 / usable) if usable else 0,
    }


def free_beds(branch):
    """Beds a patient can be put in now: available or reserved, in active rooms and wards."""
    return (
        Bed.objects.filter(
            branch=branch,
            is_active=True,
            room__is_active=True,
            room__ward__is_active=True,
            status__in=[Bed.Status.AVAILABLE, Bed.Status.RESERVED],
        )
        .select_related("room__ward")
        .order_by("room__ward__sort_order", "room__ward__name", "room__name", "code")
    )


# --- Admission ---------------------------------------------------------------------------------


def _lock_free_bed(bed: Bed, branch) -> Bed:
    bed = Bed.objects.select_for_update(of=("self",)).select_related("room__ward").get(pk=bed.pk)
    if bed.branch_id != branch.pk:
        raise IpdError("That bed is in another branch.")
    if not (bed.is_active and bed.room.is_active and bed.room.ward.is_active):
        raise IpdError(f"{bed.code} is not in use.")
    if bed.status not in (Bed.Status.AVAILABLE, Bed.Status.RESERVED):
        raise IpdError(f"{bed.code} is {bed.get_status_display().lower()}. Choose another bed.")
    return bed


def _occupy(admission: Admission, bed: Bed, at) -> BedOccupancy:
    try:
        with transaction.atomic():
            occ = BedOccupancy.objects.create(
                bed=bed, admission=admission, started_at=at, daily_rate=bed.room.daily_rate
            )
    except IntegrityError as err:
        raise IpdError(
            f"{bed.code} was just taken by another patient. Choose another bed."
        ) from err
    bed.status = Bed.Status.OCCUPIED
    bed.status_note = ""
    bed.status_changed_at = at
    bed.save(update_fields=["status", "status_note", "status_changed_at", "updated_at"])
    admission.current_bed = bed
    admission.save(update_fields=["current_bed", "updated_at"])
    return occ


def _vacate(admission: Admission, at, *, bed_status=Bed.Status.CLEANING) -> None:
    occ = admission.occupancies.filter(ended_at__isnull=True).select_related("bed").first()
    if occ is None:
        return
    occ.ended_at = at
    occ.save(update_fields=["ended_at", "updated_at"])
    bed = Bed.objects.select_for_update(of=("self",)).get(pk=occ.bed_id)
    bed.status = bed_status
    bed.status_note = "After discharge" if bed_status == Bed.Status.CLEANING else ""
    bed.status_changed_at = at
    bed.save(update_fields=["status", "status_note", "status_changed_at", "updated_at"])


@transaction.atomic
def admit(
    *,
    request,
    patient,
    doctor,
    department,
    bed,
    reason: str,
    diagnosis: str = "",
    notes: str = "",
    attendant: dict | None = None,
    admitted_at=None,
) -> Admission:
    _need(request, "ipd.manage", "admit patients")
    org = request.organization
    if patient.organization_id != org.id or not patient.is_active:
        raise IpdError("Choose an active patient of this hospital.")
    if doctor.organization_id != org.id or department.organization_id != org.id:
        raise IpdError("Choose a doctor and department of this hospital.")
    if not reason.strip():
        raise IpdError("Give the reason for admission.")
    open_stay = Admission.objects.filter(
        organization=org, patient=patient, status__in=Admission.ACTIVE_STATUSES
    ).first()
    if open_stay:
        raise IpdError(f"{patient.full_name} is already admitted ({open_stay.number}).")
    bed = _lock_free_bed(bed, request.branch)
    at = admitted_at or timezone.now()
    number = f"ADM-{next_number(org, 'admission'):05d}"
    attendant = attendant or {}
    try:
        with transaction.atomic():
            admission = Admission.objects.create(
                organization=org,
                branch=request.branch,
                number=number,
                patient=patient,
                admitting_doctor=doctor,
                department=department,
                admitted_at=at,
                admission_reason=reason.strip()[:300],
                admission_diagnosis=diagnosis.strip(),
                notes=notes.strip()[:300],
                attendant_name=attendant.get("name", "").strip()[:150],
                attendant_relation=attendant.get("relation", "").strip()[:40],
                attendant_phone=attendant.get("phone", "").strip()[:20],
                created_by=request.user,
            )
    except IntegrityError as err:  # a parallel admission of the same patient won the race
        raise IpdError(f"{patient.full_name} is already admitted.") from err
    _occupy(admission, bed, at)
    admission.invoice = billing.create_invoice(
        request=request, patient=patient, notes=f"Inpatient stay {number}"
    )
    admission.save(update_fields=["invoice", "updated_at"])
    return admission


def _lock(admission) -> Admission:
    return Admission.objects.select_for_update(of=("self",)).get(pk=admission.pk)


def _require_active(admission):
    if not admission.is_active:
        raise IpdError(f"{admission.number} is {admission.get_status_display().lower()}.")


@transaction.atomic
def transfer(admission, *, request, to_bed, reason: str) -> BedTransfer:
    _need(request, "ipd.manage", "transfer patients")
    admission = _lock(admission)
    _require_active(admission)
    if not reason.strip():
        raise IpdError("Give the reason for the transfer.")
    current = admission.current_bed
    if current is None:
        raise IpdError("This patient has no bed to transfer from.")
    if to_bed.pk == current.pk:
        raise IpdError("Choose a different bed.")
    to_bed = _lock_free_bed(to_bed, admission.branch)
    now = timezone.now()
    _vacate(admission, now)
    _occupy(admission, to_bed, now)
    kind = (
        BedTransfer.Kind.BED
        if to_bed.room.ward_id == current.room.ward_id
        else BedTransfer.Kind.WARD
    )
    return BedTransfer.objects.create(
        admission=admission,
        from_bed=current,
        to_bed=to_bed,
        kind=kind,
        reason=reason.strip()[:200],
        transferred_at=now,
        transferred_by=request.user,
    )


@transaction.atomic
def cancel_admission(admission, *, request, reason: str) -> Admission:
    """Undo an admission made by mistake: only before any charge or deposit."""
    _need(request, "ipd.manage", "cancel admissions")
    admission = _lock(admission)
    if admission.status != Status.ADMITTED:
        raise IpdError("Only an open admission can be cancelled.")
    if not reason.strip():
        raise IpdError("Give the reason for cancelling the admission.")
    if admission.deposits.exists():
        raise IpdError("A deposit was taken. Finalize the bill and return it instead.")
    if admission.invoice_id and admission.invoice.items.exists():
        raise IpdError("Charges were posted. Discharge the patient instead.")
    now = timezone.now()
    _vacate(admission, now, bed_status=Bed.Status.AVAILABLE)
    if admission.invoice_id and admission.invoice.status == Invoice.Status.DRAFT:
        billing.cancel(admission.invoice, request=request, reason=f"Admission cancelled: {reason}")
    admission.medication_orders.filter(status=MedicationOrder.Status.ACTIVE).update(
        status=MedicationOrder.Status.STOPPED,
        stopped_at=now,
        stopped_by=request.user,
        stop_reason="Admission cancelled",
    )
    admission.status = Status.CANCELLED
    admission.cancelled_at = now
    admission.cancel_reason = reason.strip()[:200]
    admission.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
    return admission


# --- Money -------------------------------------------------------------------------------------


@transaction.atomic
def record_deposit(
    admission, *, request, method, amount, reference: str = "", idempotency_key: str = ""
) -> AdmissionDeposit:
    _need(request, "billing.payment.collect", "take deposits")
    if idempotency_key:
        done = AdmissionDeposit.objects.filter(
            organization=request.organization, idempotency_key=idempotency_key
        ).first()
        if done:
            return done  # the same form was submitted twice; never take money twice
    admission = _lock(admission)
    _require_active(admission)
    if admission.bill_finalized_at:
        raise IpdError("The final bill is issued. Take further payments on the invoice.")
    amount = Decimal(amount).quantize(Decimal("0.01"))
    if amount <= ZERO:
        raise IpdError("Enter an amount greater than zero.")
    if method.organization_id != admission.organization_id or not method.is_active:
        raise IpdError("Choose an active payment method.")
    if method.requires_reference and not reference.strip():
        raise IpdError(f"{method.name} payments need a reference number.")
    try:
        with transaction.atomic():
            return AdmissionDeposit.objects.create(
                organization=admission.organization,
                branch=request.branch,
                number=f"DEP-{next_number(admission.organization, 'deposit'):05d}",
                admission=admission,
                method=method,
                amount=amount,
                reference=reference.strip()[:80],
                received_by=request.user,
                idempotency_key=idempotency_key[:40],
            )
    except IntegrityError as err:
        raise IpdError("This deposit was already recorded.") from err


@transaction.atomic
def add_charge(
    admission, *, request, service=None, description="", unit_price=None, quantity=1, kind=None
) -> InvoiceItem:
    """Post a service, procedure or other charge to the running inpatient bill."""
    _need(request, "ipd.manage", "post inpatient charges")
    admission = _lock(admission)
    _require_active(admission)
    invoice = admission.invoice
    if invoice is None or invoice.status != Invoice.Status.DRAFT or admission.bill_finalized_at:
        raise IpdError("The inpatient bill is already finalized.")
    try:
        return billing.add_item(
            invoice,
            request=request,
            service=service,
            description=description,
            unit_price=unit_price,
            quantity=quantity,
            kind=kind or (InvoiceItem.Kind.SERVICE if service else InvoiceItem.Kind.MISC),
        )
    except billing.BillingError as err:
        raise IpdError(err.message) from err


def stay_invoices(admission):
    """Every invoice of the patient raised during the stay (bed bill, lab, pharmacy ...)."""
    qs = Invoice.objects.filter(
        organization=admission.organization,
        patient=admission.patient,
        created_at__gte=admission.admitted_at,
    ).exclude(status=Invoice.Status.CANCELLED)
    if admission.stay_end:
        qs = qs.filter(created_at__lte=admission.stay_end)
    return qs.select_related("branch").order_by("created_at")


def unbilled_bed_charges(admission) -> Decimal:
    return sum(
        (o.daily_rate * o.charge_days for o in admission.occupancies.filter(charged=False)),
        ZERO,
    )


def billing_summary(admission) -> dict:
    invoices = list(stay_invoices(admission))
    deposits = list(admission.deposits.all())
    ipd = admission.invoice
    others = [i for i in invoices if ipd is None or i.pk != ipd.pk]
    outstanding = sum((i.balance_due for i in invoices), ZERO)
    unapplied = sum((d.unapplied for d in deposits), ZERO)
    bed_estimate = unbilled_bed_charges(admission) if admission.is_active else ZERO
    return {
        "ipd_invoice": ipd,
        "ipd_charges": ipd.grand_total if ipd else ZERO,
        "bed_estimate": bed_estimate,
        "other_invoices": others,
        "other_total": sum((i.grand_total for i in others), ZERO),
        "deposits": deposits,
        "deposit_total": sum((d.amount for d in deposits), ZERO),
        "deposit_unapplied": unapplied,
        "outstanding": outstanding,
        # What the family still owes (or gets back) if the bill were closed now.
        "estimated_due": (ipd.grand_total if ipd and ipd.is_draft else ZERO)
        + bed_estimate
        + outstanding
        - unapplied,
    }


@transaction.atomic
def finalize_bill(admission, *, request) -> Admission:
    """Post bed charges, issue the inpatient bill and apply the deposits to it."""
    _need(request, "billing.payment.collect", "finalize inpatient bills")
    admission = _lock(admission)
    _require_active(admission)
    if admission.bill_finalized_at:
        raise IpdError("The bill is already finalized.")
    invoice = admission.invoice
    if invoice is None or invoice.status != Invoice.Status.DRAFT:
        raise IpdError("The inpatient bill is missing or was already issued from Billing.")
    now = timezone.now()
    for occ in admission.occupancies.filter(charged=False).select_related("bed__room__ward"):
        if occ.daily_rate > ZERO:
            billing.add_item(
                invoice,
                request=request,
                kind=InvoiceItem.Kind.ROOM,
                description=f"Bed {occ.bed.code} · {occ.bed.room.ward.name} "
                f"({occ.started_at:%d %b}–{(occ.ended_at or now):%d %b})",
                unit_price=occ.daily_rate,
                quantity=occ.charge_days,
            )
        occ.charged = True
        occ.save(update_fields=["charged", "updated_at"])
    invoice.refresh_from_db()
    if invoice.items.exists():
        invoice = billing.issue(invoice, request=request)
        for dep in admission.deposits.select_for_update(of=("self",)).order_by("created_at"):
            take = min(dep.unapplied, invoice.balance_due)
            if take <= ZERO:
                continue
            billing.receive_payment(
                invoice,
                request=request,
                method=dep.method,
                amount=take,
                reference=dep.number,
                note="Advance deposit applied",
                idempotency_key=f"{billing.DEPOSIT_PAYMENT_KEY_PREFIX}{dep.pk}",
            )
            dep.applied_amount += take
            dep.save(update_fields=["applied_amount", "updated_at"])
            invoice.refresh_from_db()
    else:
        billing.cancel(invoice, request=request, reason="No inpatient charges")
    admission.bill_finalized_at = now
    admission.bill_finalized_by = request.user
    admission.save(update_fields=["bill_finalized_at", "bill_finalized_by", "updated_at"])
    return admission


@transaction.atomic
def return_deposit_balance(admission, *, request) -> Decimal:
    """Record that the unused deposit was handed back. Only after the final bill."""
    _need(request, "billing.refund", "return deposits")
    admission = _lock(admission)
    if not admission.bill_finalized_at:
        raise IpdError("Finalize the bill first, so the deposit is applied to it.")
    total = ZERO
    for dep in admission.deposits.select_for_update(of=("self",)):
        left = dep.unapplied
        if left > ZERO:
            dep.returned_amount += left
            dep.returned_at = timezone.now()
            dep.returned_by = request.user
            dep.save(update_fields=["returned_amount", "returned_at", "returned_by", "updated_at"])
            total += left
    if total == ZERO:
        raise IpdError("There is no unused deposit to return.")
    return total


# --- Clinical chart ----------------------------------------------------------------------------


def _own_doctor(request):
    doctor = getattr(request.user, "doctor_profile", None)
    if doctor is None or doctor.organization_id != request.organization.id:
        raise IpdError("Only a doctor can write this.")
    return doctor


@transaction.atomic
def add_round(admission, *, request, findings: str, plan: str = "") -> DoctorRound:
    _need(request, "consultation.create", "write ward rounds")
    doctor = _own_doctor(request)
    _require_active(admission)
    if not findings.strip():
        raise IpdError("Write the round findings.")
    return DoctorRound.objects.create(
        admission=admission,
        doctor=doctor,
        findings=findings.strip(),
        plan=plan.strip(),
        recorded_by=request.user,
    )


@transaction.atomic
def mark_round_error(round_note, *, request, reason: str) -> DoctorRound:
    _need(request, "consultation.create", "correct ward rounds")
    if not reason.strip():
        raise IpdError("Give the reason the note is wrong.")
    round_note = DoctorRound.objects.select_for_update(of=("self",)).get(pk=round_note.pk)
    if round_note.entered_in_error:
        raise IpdError("This note is already marked as entered in error.")
    round_note.entered_in_error = True
    round_note.error_reason = reason.strip()[:200]
    round_note.error_marked_by = request.user
    round_note.save(
        update_fields=["entered_in_error", "error_reason", "error_marked_by", "updated_at"]
    )
    return round_note


@transaction.atomic
def order_medication(admission, *, request, data: dict) -> MedicationOrder:
    _need(request, "prescription.create", "order medicines")
    _require_active(admission)
    medicine = data.get("medicine")
    if medicine is not None and medicine.organization_id != admission.organization_id:
        raise IpdError("That medicine is not in this hospital's list.")
    name = (data.get("medicine_name") or (medicine.display_name if medicine else "")).strip()
    if not name or not (data.get("dose") or "").strip():
        raise IpdError("Give the medicine and the dose.")
    if data.get("frequency") not in MedicationOrder.SCHEDULE:
        raise IpdError("Choose how often the medicine is given.")
    return MedicationOrder.objects.create(
        admission=admission,
        medicine=medicine,
        medicine_name=name[:200],
        dose=data["dose"].strip()[:40],
        route=data.get("route") or "oral",
        frequency=data["frequency"],
        instructions=(data.get("instructions") or "").strip()[:200],
        starts_at=data.get("starts_at") or timezone.now(),
        ends_on=data.get("ends_on"),
        ordered_by=request.user,
    )


@transaction.atomic
def stop_medication(order, *, request, reason: str) -> MedicationOrder:
    _need(request, "prescription.create", "stop medicines")
    order = MedicationOrder.objects.select_for_update(of=("self",)).get(pk=order.pk)
    if not order.is_active:
        raise IpdError("This medicine is already stopped.")
    if not reason.strip():
        raise IpdError("Give the reason for stopping the medicine.")
    order.status = MedicationOrder.Status.STOPPED
    order.stopped_at = timezone.now()
    order.stopped_by = request.user
    order.stop_reason = reason.strip()[:200]
    order.save(update_fields=["status", "stopped_at", "stopped_by", "stop_reason", "updated_at"])
    return order


# --- Discharge ---------------------------------------------------------------------------------

DISCHARGE_FIELDS = [
    "discharge_type",
    "discharge_condition",
    "discharge_diagnosis",
    "hospital_course",
    "discharge_medicines",
    "discharge_advice",
    "follow_up_date",
    "follow_up_instructions",
]


@transaction.atomic
def save_discharge_draft(admission, *, request, data: dict) -> Admission:
    _need(request, "consultation.create", "write discharge summaries")
    admission = _lock(admission)
    _require_active(admission)
    for field in DISCHARGE_FIELDS:
        if field in data:
            setattr(admission, field, data[field])
    admission.save(update_fields=[*DISCHARGE_FIELDS, "updated_at"])
    return admission


@transaction.atomic
def plan_discharge(admission, *, request) -> Admission:
    """Doctor clearance: the clinical summary is ready and the patient may go."""
    _need(request, "consultation.create", "clear patients for discharge")
    admission = _lock(admission)
    if admission.status != Status.ADMITTED:
        raise IpdError("Discharge is already planned or the stay is closed.")
    if not admission.discharge_diagnosis.strip():
        raise IpdError("Write the discharge diagnosis first.")
    admission.status = Status.DISCHARGE_PLANNED
    admission.discharge_planned_at = timezone.now()
    admission.discharge_planned_by = request.user
    admission.save(
        update_fields=["status", "discharge_planned_at", "discharge_planned_by", "updated_at"]
    )
    return admission


@transaction.atomic
def revert_discharge_plan(admission, *, request) -> Admission:
    _need(request, "consultation.create", "change discharge plans")
    admission = _lock(admission)
    if admission.status != Status.DISCHARGE_PLANNED:
        raise IpdError("Discharge is not planned.")
    if admission.bill_finalized_at:
        raise IpdError("The final bill is issued; the stay can no longer be extended.")
    admission.status = Status.ADMITTED
    admission.save(update_fields=["status", "updated_at"])
    return admission


def pending_lab_orders(admission):
    from apps.lab.models import LabOrder

    return LabOrder.objects.filter(
        organization=admission.organization,
        patient=admission.patient,
        created_at__gte=admission.admitted_at,
    ).exclude(status__in=[LabOrder.Status.RELEASED, LabOrder.Status.CANCELLED])


def checklist(admission) -> dict:
    money = billing_summary(admission)
    labs = list(pending_lab_orders(admission))
    return {
        "summary_ready": bool(admission.discharge_diagnosis.strip()),
        "doctor_cleared": admission.status == Status.DISCHARGE_PLANNED,
        "pending_labs": labs,
        "active_medicines": admission.medication_orders.filter(
            status=MedicationOrder.Status.ACTIVE
        ).count(),
        "bill_finalized": admission.bill_finalized_at is not None,
        "outstanding": money["outstanding"],
        "deposit_unapplied": money["deposit_unapplied"],
        "money": money,
    }


def summary_content(admission) -> dict:
    """The signed discharge summary: a frozen copy of the draft and the stay facts."""
    return {
        "admission_number": admission.number,
        "patient_mrn": admission.patient.mrn,
        "admitted_at": admission.admitted_at.isoformat(),
        "discharged_at": (admission.discharged_at or timezone.now()).isoformat(),
        "admitting_doctor": str(admission.admitting_doctor),
        "department": str(admission.department),
        "admission_reason": admission.admission_reason,
        "admission_diagnosis": admission.admission_diagnosis,
        "discharge_type": admission.get_discharge_type_display(),
        "discharge_condition": admission.get_discharge_condition_display()
        if admission.discharge_condition
        else "",
        "discharge_diagnosis": admission.discharge_diagnosis,
        "hospital_course": admission.hospital_course,
        "discharge_medicines": admission.discharge_medicines,
        "discharge_advice": admission.discharge_advice,
        "follow_up_date": admission.follow_up_date.isoformat() if admission.follow_up_date else "",
        "follow_up_instructions": admission.follow_up_instructions,
        "beds": [
            {
                "bed": o.bed.code,
                "ward": o.bed.room.ward.name,
                "from": o.started_at.isoformat(),
                "to": o.ended_at.isoformat() if o.ended_at else "",
            }
            for o in admission.occupancies.select_related("bed__room__ward")
        ],
    }


@transaction.atomic
def release(admission, *, request, override_reason: str = "") -> Admission:
    """Discharge: close the stay, free the bed for cleaning and sign the summary (version 1)."""
    _need(request, "ipd.manage", "discharge patients")
    admission = _lock(admission)
    if admission.status != Status.DISCHARGE_PLANNED:
        raise IpdError("The doctor must clear the patient for discharge first.")
    if not admission.bill_finalized_at:
        raise IpdError("Finalize the bill before discharge.")
    outstanding = sum((i.balance_due for i in stay_invoices(admission)), ZERO)
    if outstanding > ZERO:
        if "ipd.discharge.override" not in request.sd_perms:
            raise IpdError(
                f"{outstanding:,.2f} is still unpaid. Collect it, or ask a supervisor to "
                "approve discharge with a balance."
            )
        if not override_reason.strip():
            raise IpdError("Give the reason the patient leaves with an unpaid balance.")
        admission.outstanding_override_reason = override_reason.strip()[:200]
    now = timezone.now()
    _vacate(admission, now)
    admission.medication_orders.filter(status=MedicationOrder.Status.ACTIVE).update(
        status=MedicationOrder.Status.COMPLETED,
        stopped_at=now,
        stopped_by=request.user,
        stop_reason="Discharged",
    )
    admission.status = Status.DISCHARGED
    admission.discharged_at = now
    admission.discharged_by = request.user
    admission.save(
        update_fields=[
            "status",
            "discharged_at",
            "discharged_by",
            "outstanding_override_reason",
            "updated_at",
        ]
    )
    DischargeSummary.objects.create(
        admission=admission, version=1, content=summary_content(admission), signed_by=request.user
    )
    return admission


@transaction.atomic
def amend_summary(admission, *, request, data: dict, reason: str) -> DischargeSummary:
    """Correct a signed discharge summary: a new version, the earlier ones stay."""
    _need(request, "consultation.create", "amend discharge summaries")
    admission = _lock(admission)
    if admission.status != Status.DISCHARGED:
        raise IpdError("Only a discharged stay has a signed summary to amend.")
    if not reason.strip():
        raise IpdError("Give the reason for the amendment.")
    for field in DISCHARGE_FIELDS:
        if field in data:
            setattr(admission, field, data[field])
    admission.save(update_fields=[*DISCHARGE_FIELDS, "updated_at"])
    last = admission.summary_versions.order_by("-version").first()
    return DischargeSummary.objects.create(
        admission=admission,
        version=(last.version if last else 0) + 1,
        content=summary_content(admission),
        signed_by=request.user,
        amendment_reason=reason.strip()[:200],
    )


# --- Queries -----------------------------------------------------------------------------------


def current_admission(patient, organization):
    return (
        Admission.objects.filter(
            organization=organization, patient=patient, status__in=Admission.ACTIVE_STATUSES
        )
        .select_related("current_bed__room__ward", "branch")
        .first()
    )


def ward_overview(branch):
    """Wards with their beds and the patient in each occupied bed, for the bed board."""
    wards = list(Ward.objects.filter(branch=branch, is_active=True))
    beds = (
        Bed.objects.filter(branch=branch, is_active=True, room__is_active=True)
        .select_related("room__ward")
        .order_by("room__name", "code")
    )
    open_stays = {
        a.current_bed_id: a
        for a in Admission.objects.filter(
            branch=branch, status__in=Admission.ACTIVE_STATUSES
        ).select_related("patient", "admitting_doctor__user", "department")
    }
    by_ward = {w.pk: [] for w in wards}
    for b in beds:
        if b.room.ward_id in by_ward:
            b.admission = open_stays.get(b.pk)
            by_ward[b.room.ward_id].append(b)
    return [(w, by_ward[w.pk]) for w in wards]


def discharges_today(branch) -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return Admission.objects.filter(
        Q(discharged_at__gte=start) | Q(status=Status.DISCHARGE_PLANNED), branch=branch
    ).count()


def deposit_totals(branch, day=None):
    """Deposits taken in a branch on a day (cash in hand before the bill is closed)."""
    day = day or timezone.localdate()
    return AdmissionDeposit.objects.filter(branch=branch, created_at__date=day).aggregate(
        s=Sum("amount"), n=Count("id")
    )
