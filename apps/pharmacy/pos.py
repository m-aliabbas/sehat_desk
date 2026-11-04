"""Pharmacy point of sale (PRD §9.3). Views call these; they never change a sale directly.

Cart: one open sale per cashier per branch. Each line is one product from one batch, picked
first-expiry-first unless the pharmacist chooses a batch. Carts do not reserve stock; checkout
locks each batch and checks again (stock.sell), so two tills cannot sell the same last strip.

Checkout runs in one transaction: stock-out for every line, a billing invoice (kind "pharmacy")
issued and paid in full. If any step fails, nothing is saved.

Returns restock the original batch (stock.sale_return) and refund the customer from the original
payment(s), pro rata to any sale discount. Prescription dispense status follows sales and returns.
"""

from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.billing import services as billing
from apps.billing.models import InvoiceItem, to_money
from apps.core.dates import on_day
from apps.core.models import next_number
from apps.organizations import field_rules
from apps.organizations.models import preferences_for
from apps.prescriptions.models import Prescription, PrescriptionItem

from . import scan, stock
from .models import (
    InventoryBatch,
    PharmacyCartReturn,
    PharmacySale,
    PharmacySaleItem,
    PharmacySaleReturn,
    PharmacySaleReturnItem,
)

Status = PharmacySale.Status
ZERO = Decimal("0.00")
SELL, DISCOUNT, REFUND, COLLECT = (
    "pharmacy.sale",
    "pharmacy.discount",
    "pharmacy.refund",
    "billing.payment.collect",
)


class PosError(ValidationError):
    pass


def _need(request, code, message):
    if code not in request.sd_perms:
        raise PosError(message)


def _need_sell(request):
    _need(request, SELL, "You are not allowed to sell medicines.")


# --- Cart --------------------------------------------------------------------------------------


def current_cart(request) -> PharmacySale | None:
    return (
        PharmacySale.objects.filter(branch=request.branch, cashier=request.user, status=Status.OPEN)
        .order_by("-created_at")
        .first()
    )


@transaction.atomic
def open_cart(request) -> PharmacySale:
    """The cashier's open cart in this branch, created when needed."""
    _need_sell(request)
    cart = current_cart(request)
    if cart is not None:
        return cart
    org = request.organization
    return PharmacySale.objects.create(
        organization=org,
        branch=request.branch,
        number=f"PS-{next_number(org, 'pharmacy_sale'):05d}",
        cashier=request.user,
    )


def _lock_cart(sale, request, *, any_till=False) -> PharmacySale:
    """any_till: a supervisor (e.g. approving a discount) may act on another cashier's cart."""
    _need_sell(request)
    sale = PharmacySale.objects.select_for_update(of=("self",)).get(pk=sale.pk)
    if sale.branch_id != request.branch.pk:
        raise PosError("That sale belongs to another branch.")
    if sale.status != Status.OPEN:
        raise PosError(f"{sale.number} is {sale.get_status_display().lower()}.")
    if sale.cashier_id != request.user.pk and not any_till:
        raise PosError(f"{sale.number} is open at another till.")
    return sale


def _check_batch(batch, sale, product=None):
    if batch.branch_id != sale.branch_id:
        raise PosError("That batch belongs to another branch.")
    if product is not None and batch.product_id != product.pk:
        raise PosError("That batch is for another product.")
    if batch.is_expired:
        raise PosError(f"Batch {batch.batch_number} expired on {batch.expiry_date:%d %b %Y}.")


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _link_rx(sale, product, rx_item=None, substitution_reason=""):
    """Which prescription line this product fills, and whether it is a substitution.

    Substitution controls: a reason is required, and when both generic names are known they
    must match (same molecule, different brand). Returns (rx_item, substituted, reason)."""
    if rx_item is None:
        if sale.prescription_id and product.medicine_id:
            rx_item = sale.prescription.items.filter(medicine_id=product.medicine_id).first()
        return rx_item, False, ""
    if rx_item.prescription_id != sale.prescription_id:
        raise PosError("That prescription line is not on the attached prescription.")
    if rx_item.medicine_id and rx_item.medicine_id == product.medicine_id:
        return rx_item, False, ""
    if not substitution_reason.strip():
        raise PosError(
            f"{product} is not the prescribed {rx_item.medicine_name}. "
            "Give the reason for the substitution."
        )
    prescribed = _norm(rx_item.generic_name)
    offered = _norm(product.generic_name)
    if prescribed and offered and prescribed != offered:
        raise PosError(
            f"{product} ({product.generic_name}) is not the same medicine as "
            f"{rx_item.medicine_name} ({rx_item.generic_name}). Only same-generic substitutes "
            "can be dispensed; ask the doctor to change the prescription."
        )
    return rx_item, True, substitution_reason.strip()[:200]


def _check_available(batch, quantity):
    if quantity > batch.quantity_available:
        raise PosError(
            f"Only {batch.product.describe_quantity(batch.quantity_available)} left in batch "
            f"{batch.batch_number}."
        )


def _check_loose(product, quantity):
    if error := product.loose_sale_error(quantity):
        raise PosError(error)


def _put(sale, batch, quantity, **link) -> PharmacySaleItem:
    """Add units of one batch to the cart, merging with its existing line."""
    item = sale.items.filter(batch=batch).first()
    total = quantity + (item.quantity if item else 0)
    _check_available(batch, total)
    if item:
        item.quantity = total
        update = ["quantity", "updated_at"]
        if link.get("prescription_item") and not item.prescription_item_id:
            for k, v in link.items():
                setattr(item, k, v)
            update += list(link)
        item.save(update_fields=update)
        return item
    return PharmacySaleItem.objects.create(
        sale=sale,
        product=batch.product,
        batch=batch,
        quantity=quantity,
        unit_price=batch.sale_price,
        tax_percent=batch.product.tax_percent,
        **link,
    )


@transaction.atomic
def add_product(
    sale, *, request, product, quantity: int, batch=None, rx_item=None, substitution_reason=""
) -> list[PharmacySaleItem]:
    """Add units of a product, from the chosen batch or first-expiry-first across batches."""
    sale = _lock_cart(sale, request)
    if product.organization_id != sale.organization_id or not product.is_active:
        raise PosError("That product is not available.")
    if quantity <= 0:
        raise PosError("Enter a quantity greater than zero.")
    _check_loose(product, quantity)
    rx_item, substituted, reason = _link_rx(sale, product, rx_item, substitution_reason)
    link = {"prescription_item": rx_item, "substituted": substituted,
            "substitution_reason": reason}  # fmt: skip
    if batch is not None:
        _check_batch(batch, sale, product)
        return [_put(sale, InventoryBatch.objects.get(pk=batch.pk), quantity, **link)]
    in_cart = dict(sale.items.filter(product=product).values_list("batch_id", "quantity"))
    plan, remaining = [], quantity
    for b in stock.saleable(sale.branch, product):
        free = b.quantity_available - in_cart.get(b.pk, 0)
        if free <= 0:
            continue
        take = min(free, remaining)
        plan.append((b, take))
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        have = quantity - remaining
        raise PosError(
            f"Only {have} more {product.unit_label(have)} of {product} in saleable stock."
        )
    return [_put(sale, b, take, **link) for b, take in plan]


def _lock_item(item, request) -> PharmacySaleItem:
    _lock_cart(item.sale, request)
    return PharmacySaleItem.objects.select_related("batch__product", "sale", "product").get(
        pk=item.pk
    )


@transaction.atomic
def set_quantity(item, *, request, quantity: int) -> PharmacySaleItem | None:
    item = _lock_item(item, request)
    if quantity <= 0:
        item.delete()  # an unpaid cart line; nothing financial or stock-related yet
        return None
    _check_loose(item.product, quantity)
    _check_available(item.batch, quantity)
    item.quantity = quantity
    item.save(update_fields=["quantity", "updated_at"])
    return item


@transaction.atomic
def set_line_discount(item, *, request, percent) -> PharmacySaleItem:
    """A percent off one row. The amount follows the quantity."""
    _need(request, DISCOUNT, "You are not allowed to give discounts on pharmacy sales.")
    item = _lock_item(item, request)
    percent = Decimal(percent or 0)
    if not 0 <= percent <= 100:
        raise PosError("The discount must be between 0 and 100 percent.")
    item.discount_percent = percent
    item.discount_by = request.user if percent else None
    billing.check_discount_cap(item.sale.organization, item.discount_amount, item.gross, PosError)
    item.save(update_fields=["discount_percent", "discount_by", "updated_at"])
    return item


@transaction.atomic
def set_discount_note(sale, *, request, asked_by, reason) -> PharmacySale:
    """Who asked for this sale's discounts, and why. Kept for the discount trail."""
    _need(request, DISCOUNT, "You are not allowed to give discounts on pharmacy sales.")
    sale = _lock_cart(sale, request, any_till=True)
    if asked_by is not None and asked_by.organization_id != sale.organization_id:
        raise PosError("Choose someone from this hospital.")
    sale.discount_asked_by = asked_by
    sale.discount_reason = reason.strip()[:200]
    sale.save(update_fields=["discount_asked_by", "discount_reason", "updated_at"])
    return sale


def rx_only_without_prescription(sale) -> list[PharmacySaleItem]:
    """Rx-only lines sold with no prescription line, for the audit log and the till's notice."""
    return [
        i
        for i in sale.items.select_related("product")
        if i.product.requires_prescription and not i.prescription_item_id
    ]


@transaction.atomic
def change_batch(item, *, request, batch) -> PharmacySaleItem:
    """Sell this line from another batch of the same product (its price follows the batch)."""
    item = _lock_item(item, request)
    if batch.pk == item.batch_id:
        return item
    _check_batch(batch, item.sale, item.product)
    batch = InventoryBatch.objects.get(pk=batch.pk)
    other = item.sale.items.filter(batch=batch).first()
    quantity = item.quantity + (other.quantity if other else 0)
    _check_available(batch, quantity)
    if other:
        other.delete()
    item.batch = batch
    item.quantity = quantity
    item.unit_price = batch.sale_price
    item.save(update_fields=["batch", "quantity", "unit_price", "updated_at"])
    return item


@transaction.atomic
def remove_item(item, *, request) -> None:
    item = _lock_item(item, request)
    item.delete()


@transaction.atomic
def attach_patient(sale, *, request, patient=None, customer_name="") -> PharmacySale:
    """Attach a registered patient, or name a walk-in customer (patient=None)."""
    sale = _lock_cart(sale, request)
    if patient is not None and patient.organization_id != sale.organization_id:
        raise PosError("That patient is not available.")
    if sale.prescription_id and (patient is None or patient.pk != sale.prescription.patient_id):
        raise PosError("Remove the prescription first; it belongs to another patient.")
    sale.patient = patient
    sale.customer_name = "" if patient else customer_name.strip()[:120]
    sale.save(update_fields=["patient", "customer_name", "updated_at"])
    return sale


def dispensable(request):
    """Prescriptions this branch's pharmacy can dispense from."""
    return Prescription.objects.filter(
        organization=request.organization,
        branch=request.branch,
        status=Prescription.Status.SIGNED,
        sent_to_pharmacy_at__isnull=False,
        dispense_status__in=[Prescription.Dispense.PENDING, Prescription.Dispense.PARTIAL],
    )


@transaction.atomic
def attach_prescription(sale, *, request, prescription) -> PharmacySale:
    sale = _lock_cart(sale, request)
    rx = dispensable(request).filter(pk=prescription.pk).select_related("patient").first()
    if rx is None:
        raise PosError("That prescription is not waiting to be dispensed at this pharmacy.")
    if sale.patient_id and sale.patient_id != rx.patient_id:
        raise PosError(f"{rx.number} is for {rx.patient.full_name}, not {sale.patient.full_name}.")
    sale.prescription = rx
    sale.patient = rx.patient
    sale.customer_name = ""
    sale.save(update_fields=["prescription", "patient", "customer_name", "updated_at"])
    by_medicine = {i.medicine_id: i for i in rx.items.all() if i.medicine_id}
    for line in sale.items.filter(prescription_item__isnull=True).select_related("product"):
        match = by_medicine.get(line.product.medicine_id)
        if match:
            line.prescription_item = match
            line.save(update_fields=["prescription_item", "updated_at"])
    return sale


@transaction.atomic
def attach_paper_prescription(sale, *, request, doctor) -> PharmacySale:
    """The patient brought a handwritten prescription: an empty one to record against. What is
    sold gets added to it at checkout."""
    sale = _lock_cart(sale, request)
    if sale.patient_id is None:
        raise PosError("Attach the patient first: a prescription belongs to a patient.")
    if sale.prescription_id:
        raise PosError(f"{sale.prescription.number} is already attached.")
    if doctor.organization_id != sale.organization_id:
        raise PosError("Choose a doctor from this hospital.")
    year = timezone.localdate().year
    org = sale.organization
    sale.prescription = Prescription.objects.create(
        organization=org,
        branch=sale.branch,
        number=f"RX-{year}-{next_number(org, f'prescription:{year}'):05d}",
        patient=sale.patient,
        doctor=doctor,
        status=Prescription.Status.PAPER,
    )
    sale.save(update_fields=["prescription", "updated_at"])
    return sale


def visits_without_rx(request, *, patient=None, q="") -> list[dict]:
    """Today's reception visits with nothing for the pharmacy yet. The doctor may have written on
    paper, so the till can tag the sale to the visit (attach_paper_prescription)."""
    from apps.queue.models import QueueToken

    today = timezone.localdate()
    tokens = QueueToken.objects.filter(branch=request.branch, date=today).exclude(
        status=QueueToken.Status.CANCELLED
    )
    appointments = Appointment.objects.filter(branch=request.branch, date=today).exclude(
        status__in=Appointment.RELEASED_STATUSES
    )
    if patient is not None:
        tokens = tokens.filter(patient=patient)
        appointments = appointments.filter(patient=patient)
    if q:
        words = " ".join(q.lower().split())
        tokens = tokens.filter(
            Q(patient__mrn__icontains=q) | Q(patient__name_search__icontains=words)
        )
        appointments = appointments.filter(
            Q(patient__mrn__icontains=q) | Q(patient__name_search__icontains=words)
        )
    handled = set(
        Prescription.objects.filter(branch=request.branch)
        .filter(
            Q(status=Prescription.Status.PAPER, **on_day("created_at", today))
            | Q(**on_day("sent_to_pharmacy_at", today))
        )
        .values_list("patient_id", "doctor_id")
    )
    visits = {}
    for visit in [
        *tokens.select_related("patient", "doctor__user").order_by("created_at")[:100],
        *appointments.select_related("patient", "doctor__user").order_by("start_time")[:100],
    ]:
        key = (visit.patient_id, visit.doctor_id)
        if key not in handled and key not in visits:
            visits[key] = {
                "patient": visit.patient,
                "doctor": visit.doctor,
                "label": getattr(visit, "label", None) or visit.number,
                "status": visit.get_status_display(),
            }
    return list(visits.values())


@transaction.atomic
def tag_visit(sale, *, request, patient, doctor=None) -> PharmacySale:
    """Put a visit's patient on the sale and, when the doctor is known, an empty paper Rx."""
    sale = _lock_cart(sale, request)
    if sale.patient_id and sale.patient_id != patient.pk:
        raise PosError(f"This sale is for {sale.patient.full_name}. Finish or clear it first.")
    sale = attach_patient(sale, request=request, patient=patient)
    if doctor is not None:
        sale = attach_paper_prescription(sale, request=request, doctor=doctor)
    return sale


def suggested_doctor(sale):
    """Whoever the patient saw most recently: the likely author of a handwritten prescription."""
    if not sale or not sale.patient_id:
        return None
    appointment = (
        Appointment.objects.filter(patient=sale.patient, branch=sale.branch)
        .exclude(status__in=Appointment.RELEASED_STATUSES)
        .filter(date__lte=timezone.localdate())
        .order_by("-date", "-start_time")
        .first()
    )
    return appointment.doctor if appointment else None


@transaction.atomic
def detach_prescription(sale, *, request) -> PharmacySale:
    sale = _lock_cart(sale, request)
    rx = sale.prescription
    sale.items.update(prescription_item=None, substituted=False, substitution_reason="")
    sale.prescription = None
    sale.save(update_fields=["prescription", "updated_at"])
    if rx and rx.status == Prescription.Status.PAPER and not rx.items.exists():
        rx.delete()  # attached by mistake, nothing was ever recorded on it
    return sale


def _add_sold_to_prescription(sale, request) -> list[PrescriptionItem]:
    """Medicines sold against an attached prescription go on it, marked as added at the pharmacy.
    Existing lines are never changed. The doctor's directions are not invented."""
    added = []
    rx = sale.prescription
    order = rx.items.count()
    for line in sale.items.filter(prescription_item__isnull=True).select_related("product"):
        order += 1
        product = line.product
        item = PrescriptionItem.objects.create(
            prescription=rx,
            medicine=product.medicine,
            medicine_name=product.display_name,
            generic_name=product.generic_name,
            dose="",
            frequency="",
            quantity=line.quantity,
            sort_order=order,
            added_at_pharmacy=True,
            added_by=request.user,
        )
        line.prescription_item = item
        line.save(update_fields=["prescription_item", "updated_at"])
        added.append(item)
    return added


def fee_lines(sale):
    """Visit fees billed with this sale (doctor's fee, procedure, injection...): the invoice's
    lines that are not medicine. Receipts and the sale screen list them under the medicines."""
    if not sale.invoice_id:
        return InvoiceItem.objects.none()
    return sale.invoice.items.exclude(kind=InvoiceItem.Kind.PHARMACY)


def visit_dues(sale) -> list:
    """Fees the hospital routes to the pharmacy for this patient today (doctor's fee,
    injection...). They join the sale's invoice at checkout (apps/flow)."""
    from apps.flow.services import pharmacy_dues

    return pharmacy_dues(sale)


def totals(sale) -> dict:
    items = list(sale.items.all())
    lines = sum((i.line_total for i in items), ZERO)
    discount = min(to_money(sale.discount), lines)
    returns = sum((r.amount for r in pending_returns(sale)), ZERO)
    dues = visit_dues(sale)
    dues_total = sum((d.amount for d in dues), ZERO)
    return {
        "dues": dues,
        "dues_total": dues_total,
        "before_discount": lines + dues_total,
        "returns": returns,
        "net": lines + dues_total - discount - returns,
        "lines": len(items),
        "units": sum(i.quantity for i in items),
        "subtotal": sum((i.gross for i in items), ZERO),
        "line_discount": sum((i.discount_amount for i in items), ZERO),
        "tax": sum((i.tax_amount for i in items), ZERO),
        "lines_total": lines,
        "discount": discount,
        "grand_total": lines + dues_total - discount,
    }


@transaction.atomic
def set_discount(sale, *, request, amount, reason) -> PharmacySale:
    _need(request, DISCOUNT, "You are not allowed to give discounts on pharmacy sales.")
    sale = _lock_cart(sale, request, any_till=True)
    amount = to_money(amount)
    lines = totals(sale)["grand_total"] + to_money(sale.discount)
    if amount < 0 or amount > lines:
        raise PosError(f"The discount must be between 0 and {lines}.")
    billing.check_discount_cap(sale.organization, amount, lines, error=PosError)
    if amount and not reason.strip():
        raise PosError("Give a reason for the discount.")
    sale.discount = amount
    keep_note = sale.items.filter(discount_percent__gt=0).exists()
    sale.discount_reason = reason.strip()[:200] if amount or keep_note else ""
    sale.discount_by = request.user if amount else None
    sale.save(update_fields=["discount", "discount_reason", "discount_by", "updated_at"])
    return sale


@transaction.atomic
def hold(sale, *, request, note="") -> PharmacySale:
    sale = _lock_cart(sale, request)
    if not sale.items.exists():
        raise PosError("The cart is empty; there is nothing to hold.")
    sale.status = Status.HELD
    sale.hold_note = note.strip()[:120]
    sale.held_at = timezone.now()
    sale.save(update_fields=["status", "hold_note", "held_at", "updated_at"])
    return sale


@transaction.atomic
def resume(sale, *, request) -> PharmacySale:
    """Bring a held sale back to this till. A cart already in progress is held first."""
    _need_sell(request)
    sale = PharmacySale.objects.select_for_update(of=("self",)).get(pk=sale.pk)
    if sale.branch_id != request.branch.pk or sale.status != Status.HELD:
        raise PosError(f"{sale.number} is not a held sale.")
    current = current_cart(request)
    if current is not None:
        if current.items.exists():
            hold(current, request=request, note="Held to resume another sale")
        else:
            _void(current)
    sale.status = Status.OPEN
    sale.cashier = request.user
    sale.save(update_fields=["status", "cashier", "updated_at"])
    return sale


def _void(sale):
    sale.items.all().delete()  # cart lines only; no stock or money was ever posted
    sale.cart_returns.filter(settled_by=None).delete()
    sale.status = Status.VOIDED
    sale.save(update_fields=["status", "updated_at"])


@transaction.atomic
def clear(sale, *, request) -> PharmacySale:
    """Empty and close an unpaid cart (or a held sale) without selling."""
    _need_sell(request)
    sale = PharmacySale.objects.select_for_update(of=("self",)).get(pk=sale.pk)
    if sale.branch_id != request.branch.pk or not sale.is_cart:
        raise PosError(f"{sale.number} cannot be cleared.")
    if sale.status == Status.OPEN and sale.cashier_id != request.user.pk:
        raise PosError(f"{sale.number} is open at another till.")
    _void(sale)
    return sale


# --- Checkout ----------------------------------------------------------------------------------


def checkout_problems(sale) -> list[str]:
    """Reasons the cart cannot be sold yet (shown before payment, enforced at checkout)."""
    problems = []
    items = list(sale.items.select_related("product", "batch"))
    if not items and not visit_dues(sale):
        problems.append("The cart is empty.")
    org = sale.organization
    if field_rules.is_required(org, "till.customer") and not (
        sale.patient_id or sale.customer_name
    ):
        problems.append("Enter the customer's name or attach the patient.")
    if sale.discount or any(i.discount_percent for i in items):
        if field_rules.is_required(org, "till.discount_reason") and not sale.discount_reason:
            problems.append("Give the reason for the discount.")
        if field_rules.is_required(org, "till.discount_asked_by") and not sale.discount_asked_by_id:
            problems.append("Say who asked for the discount.")
    strict = preferences_for(org).pharmacy_require_prescription
    for item in items:
        if strict and item.product.requires_prescription and not item.prescription_item_id:
            problems.append(
                f"{item.product} needs a prescription. Attach the patient's prescription."
            )
        if item.batch.is_expired:
            problems.append(f"Batch {item.batch.batch_number} of {item.product} has expired.")
        elif item.quantity > item.batch.quantity_available:
            problems.append(
                f"Only {item.product.describe_quantity(item.batch.quantity_available)} of "
                f"{item.product} left in batch {item.batch.batch_number}."
            )
        if error := item.product.loose_sale_error(item.quantity):
            problems.append(error)
    return problems


@transaction.atomic
def checkout(sale, *, request, payments: list[dict], adjust_cash=False) -> PharmacySale:
    """Sell the cart. payments: [{"method", "amount", "reference"}] adding up to the total.
    adjust_cash: a cash payment may differ by up to the limit in Settings → Billing.

    Posts stock-out per line, issues an invoice and records the payments, all or nothing.
    Submitting the same cart twice returns the completed sale instead of charging again."""
    _need(request, COLLECT, "You are not allowed to take payments.")
    _need_sell(request)
    locked = PharmacySale.objects.select_for_update(of=("self",)).get(pk=sale.pk)
    if locked.status == Status.COMPLETED:
        return locked
    sale = _lock_cart(sale, request)
    problems = checkout_problems(sale)
    if problems:
        raise PosError(problems[0])
    t = totals(sale)
    returned = _settle_returns(sale, request)
    if returned:
        sale.refresh_from_db()
    paid = sum((to_money(p["amount"]) for p in payments), ZERO)
    has_cash = any(p["method"].code == "cash" for p in payments)
    if paid != t["grand_total"] and not (adjust_cash and has_cash):
        raise PosError(
            f"Payments add up to {paid}; the sale total is {t['grand_total']}. "
            "Pharmacy sales are paid in full."
        )
    if any(to_money(p["amount"]) <= ZERO for p in payments):
        raise PosError("Each payment must be greater than zero.")
    try:
        invoice = billing.create_invoice(
            request=request,
            patient=sale.patient,
            customer_name=sale.customer_name or "Walk-in customer",
            notes=f"Pharmacy sale {sale.number}",
        )
        for item in sale.items.select_related("product", "batch"):
            billing.add_item(
                invoice,
                request=request,
                kind=InvoiceItem.Kind.PHARMACY,
                description=f"{item.product.display_name} · batch {item.batch.batch_number}",
                unit_price=item.unit_price,
                quantity=item.quantity,
                tax_percent=item.tax_percent,
                discount=item.discount_amount,
                discount_reason=(sale.discount_reason or "Line discount at the till")
                if item.discount_percent
                else "",
            )
            item.stock_txn = stock.sell(
                item.batch, request=request, quantity=item.quantity, reference=sale.number
            )
            item.save(update_fields=["stock_txn", "updated_at"])
        if t["dues"]:
            from apps.flow.services import bill_pharmacy_dues

            bill_pharmacy_dues(invoice, request=request, sale=sale)
        if t["discount"]:
            billing.set_invoice_discount(
                invoice,
                request=request,
                amount=t["discount"],
                reason=sale.discount_reason,
                authorized_by=sale.discount_by,
            )
        invoice = billing.issue(invoice, request=request)
        if paid != invoice.balance_due:
            invoice = billing.apply_cash_adjustment(invoice, cash_paid=paid)
        for p in payments:
            billing.receive_payment(
                invoice,
                request=request,
                method=p["method"],
                amount=p["amount"],
                reference=p.get("reference", ""),
                note=f"Pharmacy sale {sale.number}",
            )
    except (billing.BillingError, stock.StockError) as err:
        raise PosError(err.message) from err  # rolls back the whole checkout
    invoice.refresh_from_db()
    sale.invoice = invoice
    sale.status = Status.COMPLETED
    sale.completed_at = timezone.now()
    sale.grand_total = invoice.grand_total
    sale.save(update_fields=["invoice", "status", "completed_at", "grand_total", "updated_at"])
    if sale.prescription_id:
        _add_sold_to_prescription(sale, request)
        refresh_dispense_status(sale.prescription)
    return sale


# --- Prescriptions -----------------------------------------------------------------------------


def dispensed_quantities(rx) -> dict[int, int]:
    """Net units dispensed per prescription line (completed sales minus returns)."""
    sold = defaultdict(int)
    rows = PharmacySaleItem.objects.filter(
        prescription_item__prescription=rx, sale__status=Status.COMPLETED
    ).values("prescription_item_id", "quantity", "pk")
    returned = dict(
        PharmacySaleReturnItem.objects.filter(sale_item__prescription_item__prescription=rx)
        .values("sale_item_id")
        .annotate(s=Sum("quantity"))
        .values_list("sale_item_id", "s")
    )
    for row in rows:
        sold[row["prescription_item_id"]] += row["quantity"] - returned.get(row["pk"], 0)
    return dict(sold)


def refresh_dispense_status(rx) -> Prescription:
    rx = Prescription.objects.select_for_update(of=("self",)).get(pk=rx.pk)
    if rx.dispense_status == Prescription.Dispense.CLOSED:
        return rx
    done = dispensed_quantities(rx)
    items = list(rx.items.all())
    if not any(done.get(i.pk, 0) > 0 for i in items):
        status = Prescription.Dispense.PENDING
    elif all(done.get(i.pk, 0) >= (i.quantity or 1) for i in items):
        status = Prescription.Dispense.DISPENSED
    else:
        status = Prescription.Dispense.PARTIAL
    if status != rx.dispense_status:
        rx.dispense_status = status
        rx.save(update_fields=["dispense_status", "updated_at"])
    return rx


@transaction.atomic
def close_prescription(rx, *, request, note: str) -> Prescription:
    """Finish a partly dispensed prescription: the rest will not be dispensed here."""
    _need_sell(request)
    rx = dispensable(request).select_for_update(of=("self",)).filter(pk=rx.pk).first()
    if rx is None:
        raise PosError("That prescription is not waiting to be dispensed at this pharmacy.")
    if not note.strip():
        raise PosError("Say why the rest is not being dispensed (e.g. patient declined).")
    rx.dispense_status = Prescription.Dispense.CLOSED
    rx.dispense_note = note.strip()[:200]
    rx.save(update_fields=["dispense_status", "dispense_note", "updated_at"])
    return rx


def prescription_lines(rx, branch) -> list[dict]:
    """Each prescribed medicine with what is dispensed, what is left, and stock to fill it:
    the prescribed product(s) and same-generic substitutes, with saleable units on hand."""
    done = dispensed_quantities(rx)
    products = list(stock.products_with_stock(branch, rx.organization).filter(is_active=True))
    lines = []
    for item in rx.items.all():
        prescribed = item.quantity or 0
        dispensed = done.get(item.pk, 0)
        exact = [p for p in products if item.medicine_id and p.medicine_id == item.medicine_id]
        generic = _norm(item.generic_name)
        substitutes = [
            p for p in products
            if p not in exact and generic and _norm(p.generic_name) == generic
        ]  # fmt: skip
        remaining = max(prescribed - dispensed, 0) if prescribed else (0 if dispensed else 1)
        in_stock = sum(p.stock for p in exact)
        suggest = min(remaining, in_stock)
        if len(exact) == 1 and exact[0].loose_sale_error(suggest):
            # Whole packs only: round up to full packs, but no more than whole packs on hand.
            size = exact[0].pack_size
            suggest = min(-(-remaining // size) * size, in_stock // size * size)
        lines.append(
            {
                "item": item,
                "prescribed": prescribed,
                "dispensed": dispensed,
                "remaining": remaining,
                "exact": exact,
                "substitutes": substitutes,
                "in_stock": in_stock,
                "suggest": suggest,
                "status": (
                    "done"
                    if remaining == 0
                    else "available"
                    if in_stock >= remaining
                    else "partial"
                    if in_stock or substitutes
                    else "unavailable"
                ),  # fmt: skip
            }
        )
    return lines


@transaction.atomic
def fill_from_prescription(sale, *, request, prescription, lines: list[dict]) -> PharmacySale:
    """Attach the prescription and add the chosen products.
    lines: [{"rx_item", "product", "quantity", "reason"}]; quantity 0 skips a line."""
    sale = attach_prescription(sale, request=request, prescription=prescription)
    added = 0
    for line in lines:
        if not line["quantity"]:
            continue
        if line["rx_item"].prescription_id != sale.prescription_id:
            raise PosError("That prescription line is not on this prescription.")
        add_product(
            sale,
            request=request,
            product=line["product"],
            quantity=line["quantity"],
            rx_item=line["rx_item"],
            substitution_reason=line.get("reason", ""),
        )
        added += 1
    if not added:
        raise PosError("Choose at least one medicine to dispense.")
    return sale


# --- Returns -----------------------------------------------------------------------------------


@transaction.atomic
def return_items(sale, *, request, quantities: dict[int, int], reason: str):
    """Return units of a completed sale: restock each batch and refund the customer.
    quantities: {sale_item_id: units}. Returns the PharmacySaleReturn."""
    _need(request, REFUND, "You are not allowed to process pharmacy returns.")
    sale = (
        PharmacySale.objects.select_for_update(of=("self",))
        .select_related("invoice")
        .get(pk=sale.pk)
    )
    if sale.branch_id != request.branch.pk or sale.status != Status.COMPLETED:
        raise PosError("Only completed sales of this branch can be returned.")
    if not reason.strip():
        raise PosError("Give the reason for the return.")
    wanted = {k: v for k, v in quantities.items() if v}
    if not wanted:
        raise PosError("Enter the quantity returned for at least one item.")
    items = {i.pk: i for i in sale.items.select_related("product", "stock_txn")}
    all_lines = sum((i.line_total for i in items.values()), ZERO)
    factor = (sale.invoice.grand_total / all_lines) if all_lines else Decimal(0)
    org = sale.organization
    ret = PharmacySaleReturn.objects.create(
        organization=org,
        branch=sale.branch,
        number=f"SR-{next_number(org, 'pharmacy_return'):05d}",
        sale=sale,
        reason=reason.strip()[:200],
        amount=ZERO,
        created_by=request.user,
    )
    total = ZERO
    for item_id, qty in wanted.items():
        item = items.get(item_id)
        if item is None:
            raise PosError("That item is not on this sale.")
        left = item.quantity - item.returned_quantity
        if qty < 0 or qty > left:
            raise PosError(
                f"Only {item.product.describe_quantity(left)} of {item.product} can still be "
                "returned."
            )
        try:
            txn = stock.sale_return(item.stock_txn, request=request, quantity=qty, reason=reason)
        except stock.StockError as err:
            raise PosError(err.message) from err
        amount = to_money(item.line_total * qty / item.quantity * factor)
        PharmacySaleReturnItem.objects.create(
            sale_return=ret, sale_item=item, quantity=qty, amount=amount, stock_txn=txn
        )
        total += amount
    invoice = sale.invoice
    total = min(total, invoice.refundable)
    owed = total
    for payment in invoice.payments.order_by("created_at"):
        if owed <= ZERO:
            break
        take = min(owed, payment.refundable)
        if take <= ZERO:
            continue
        try:
            refund = billing.refund(
                payment,
                request=request,
                amount=take,
                reason=f"{ret.number}: {reason.strip()}"[:200],
                permission=REFUND,
            )
        except billing.BillingError as err:
            raise PosError(err.message) from err
        ret.refunds.add(refund)
        owed -= take
    ret.amount = total
    ret.save(update_fields=["amount", "updated_at"])
    if sale.prescription_id:
        refresh_dispense_status(sale.prescription)
    return ret


# --- Returns entered on the till ----------------------------------------------------------------


def pending_returns(sale):
    return sale.cart_returns.filter(settled_by=None).select_related(
        "source__product", "source__batch", "source__sale__invoice"
    )


def _returnable_sources(sale, product):
    """Past sale lines of this product that can still come back: this customer's first, then
    newest first. Walk-in customers rarely keep receipts, so any sale of it here will do."""
    held = {r.source_id: r.quantity for r in sale.cart_returns.filter(settled_by=None)}
    lines = PharmacySaleItem.objects.filter(
        product=product, sale__branch=sale.branch, sale__status=Status.COMPLETED
    ).select_related("sale")
    if sale.patient_id:
        mine = [i for i in lines.filter(sale__patient=sale.patient).order_by("-sale__completed_at")]
        others = lines.exclude(sale__patient=sale.patient)
    else:
        mine, others = [], lines
    for line in [*mine, *others.order_by("-sale__completed_at")[:200]]:
        free = line.quantity - line.returned_quantity - held.get(line.pk, 0)
        if free > 0:
            yield line, free


@transaction.atomic
def add_return(sale, *, request, product, quantity: int) -> list[PharmacyCartReturn]:
    """Put a return on the till, matched to the sales it came from."""
    _need(request, REFUND, "You are not allowed to process pharmacy returns.")
    sale = _lock_cart(sale, request)
    if product.organization_id != sale.organization_id:
        raise PosError("That product is not available.")
    if quantity <= 0:
        raise PosError("Enter a quantity greater than zero.")
    rows, remaining = [], quantity
    for line, free in _returnable_sources(sale, product):
        take = min(free, remaining)
        row, _ = PharmacyCartReturn.objects.get_or_create(
            sale=sale, source=line, settled_by=None, defaults={"quantity": 0}
        )
        row.quantity += take
        row.save(update_fields=["quantity", "updated_at"])
        rows.append(row)
        remaining -= take
        if remaining == 0:
            return rows
    raise PosError(
        f"Only {quantity - remaining} {product.unit_label(quantity - remaining)} of {product} "
        "were sold here and can come back. Use a stock adjustment for anything else."
    )


@transaction.atomic
def item_to_return(item, *, request) -> list[PharmacyCartReturn]:
    """The small Return button on a till row: this entry is coming back, not going out."""
    item = _lock_item(item, request)
    product, quantity, sale = item.product, item.quantity, item.sale
    item.delete()
    return add_return(sale, request=request, product=product, quantity=quantity)


@transaction.atomic
def set_return_quantity(row, *, request, quantity: int) -> None:
    _need(request, REFUND, "You are not allowed to process pharmacy returns.")
    _lock_cart(row.sale, request)
    if row.settled_by_id:
        raise PosError("That return is already done.")
    if quantity <= 0:
        row.delete()
        return
    left = row.source.quantity - row.source.returned_quantity
    if quantity > left:
        raise PosError(f"Only {left} can come back from sale {row.source.sale.number}.")
    row.quantity = quantity
    row.save(update_fields=["quantity", "updated_at"])


def _settle_returns(sale, request) -> list:
    done = []
    for row in pending_returns(sale):
        ret = return_items(
            row.source.sale,
            request=request,
            quantities={row.source_id: row.quantity},
            reason=row.reason,
        )
        row.settled_by = ret
        row.save(update_fields=["settled_by", "updated_at"])
        done.append(ret)
    return done


@transaction.atomic
def settle_returns_only(sale, *, request) -> list:
    """A till entry that only has returns: restock and refund, with nothing to charge."""
    sale = _lock_cart(sale, request)
    if sale.items.exists():
        raise PosError("This cart also has items to sell. Take payment instead.")
    done = _settle_returns(sale, request)
    if not done:
        raise PosError("There is nothing to return.")
    _void(sale)
    return done


# --- Lookups -----------------------------------------------------------------------------------


def search_products(request, q: str, limit=12):
    """Products for the POS search box: exact barcode/SKU first, then name words."""
    q = q.strip()
    qs = stock.products_with_stock(request.branch, request.organization).filter(is_active=True)
    if not q:
        return []
    read = scan.parse(q)
    if exact := scan.find_products(qs, read):
        return exact
    if read.structured:
        return []
    for word in q.lower().split():
        qs = qs.filter(search_text__contains=word)
    return list(qs.order_by("-stock", "brand_name")[:limit])


def batch_for_scan(sale, product, read) -> tuple[InventoryBatch | None, str]:
    """The batch of the pack in the pharmacist's hand, and a note if it cannot be used.
    None means first-expiry-first. An expired pack is refused even if newer stock exists."""
    if read.expired:
        raise PosError(f"This pack of {product} expired on {read.expiry:%d %b %Y}. Do not sell it.")
    if not read.batch or read.batch_is_guess:
        return None, ""
    lots = InventoryBatch.objects.filter(
        branch=sale.branch, product=product, batch_number__iexact=read.batch
    )
    if lots.filter(expiry_date__lt=timezone.localdate()).exists():
        raise PosError(f"Batch {read.batch} of {product} has expired. Do not sell it.")
    in_cart = dict(sale.items.filter(product=product).values_list("batch_id", "quantity"))
    for lot in stock.saleable(sale.branch, product).filter(pk__in=lots.values("pk")):
        if lot.quantity_available > in_cart.get(lot.pk, 0):
            return lot, ""
    return None, f"Batch {read.batch} is not on this branch's stock. Sold from the first to expire."


def rx_line_for(rx_item_id, sale) -> PrescriptionItem | None:
    if not (rx_item_id and sale.prescription_id):
        return None
    return PrescriptionItem.objects.filter(pk=rx_item_id, prescription=sale.prescription).first()
