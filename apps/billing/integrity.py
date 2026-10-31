"""Do the books agree with themselves? Read-only checks of stored totals against their source rows,
and the patient statement. Payments, refunds, invoice lines, stock and supplier ledger rows are
the truth; the totals kept on invoices are copies for fast lists.
"""

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum

from apps.core.db import locked

from .models import ZERO, Invoice, InvoiceItem, Payment, Refund
from .services import Status, _status_from_money, recalculate


@dataclass
class Problem:
    area: str
    number: str
    detail: str
    invoice: Invoice | None = None
    url_name: str = ""
    pk: int | None = None


def _sums(qs, key):
    return {r[key]: r["s"] or ZERO for r in qs.values(key).annotate(s=Sum("amount"))}


def invoice_problems(branch) -> list[Problem]:
    invoices = Invoice.objects.filter(branch=branch).exclude(status=Status.DRAFT)
    paid = _sums(Payment.objects.filter(branch=branch), "invoice_id")
    refunded = _sums(Refund.objects.filter(branch=branch), "invoice_id")
    lines = {
        r["invoice_id"]: r["s"] or ZERO
        for r in InvoiceItem.objects.filter(invoice__branch=branch)
        .values("invoice_id")
        .annotate(s=Sum("line_total"))
    }
    problems = []
    for inv in invoices:
        found = []
        real_paid, real_refunded = paid.get(inv.pk, ZERO), refunded.get(inv.pk, ZERO)
        if inv.amount_paid != real_paid:
            found.append(f"shows {inv.amount_paid} paid, payments add up to {real_paid}")
        if inv.amount_refunded != real_refunded:
            found.append(f"shows {inv.amount_refunded} refunded, refunds add up to {real_refunded}")
        if inv.status == Status.CANCELLED:
            if real_paid - real_refunded:
                found.append(f"is cancelled but still holds {real_paid - real_refunded}")
        else:
            total = lines.get(inv.pk, ZERO) - inv.invoice_discount
            if inv.grand_total != total:
                found.append(f"shows a total of {inv.grand_total}, its lines give {total}")
            if not found and inv.status != _status_from_money(inv):
                found.append(
                    f"is marked {inv.get_status_display().lower()} but the money disagrees"
                )
        if found:
            problems.append(
                Problem("Invoice", inv.number, "; ".join(found), invoice=inv,
                        url_name="billing:invoice-detail", pk=inv.pk)
            )  # fmt: skip
    return problems


def supplier_problems(branch) -> list[Problem]:
    from apps.pharmacy.models import Purchase, VendorLedgerEntry

    entries = {
        r["purchase_id"]: r["s"] or ZERO
        for r in VendorLedgerEntry.objects.filter(
            branch=branch, kind=VendorLedgerEntry.Kind.PURCHASE
        )
        .values("purchase_id")
        .annotate(s=Sum("amount"))
    }
    problems = []
    for p in Purchase.objects.filter(branch=branch, status=Purchase.Status.RECEIVED):
        posted = entries.get(p.pk, ZERO)
        if posted != p.grand_total:
            problems.append(
                Problem("Supplier", p.number,
                        f"total is {p.grand_total}, supplier ledger shows {posted}",
                        url_name="pharmacy:purchase-detail", pk=p.pk)
            )  # fmt: skip
    return problems


def stock_problems(branch) -> list[Problem]:
    from apps.pharmacy.models import InventoryBatch, StockTransaction

    moved = {
        r["batch_id"]: r["s"] or 0
        for r in StockTransaction.objects.filter(branch=branch)
        .values("batch_id")
        .annotate(s=Sum("quantity"))
    }
    problems = []
    for b in InventoryBatch.objects.filter(branch=branch).select_related("product"):
        if b.quantity_available != moved.get(b.pk, 0):
            problems.append(
                Problem("Stock", f"{b.product} · {b.batch_number}",
                        f"shows {b.quantity_available}, ledger adds up to {moved.get(b.pk, 0)}",
                        url_name="pharmacy:product-detail", pk=b.product_id)
            )  # fmt: skip
    return problems


def all_problems(branch) -> list[Problem]:
    return invoice_problems(branch) + supplier_problems(branch) + stock_problems(branch)


@transaction.atomic
def repair_invoice(invoice) -> Invoice:
    """Reset an invoice's stored totals and status from its own rows. Moves no money and adds no
    row, so the payments and refunds on record stay exactly as they were."""
    invoice = locked(invoice)
    invoice.amount_paid = invoice.payments.aggregate(s=Sum("amount"))["s"] or ZERO
    invoice.amount_refunded = invoice.refunds.aggregate(s=Sum("amount"))["s"] or ZERO
    if invoice.status != Status.CANCELLED:
        recalculate(invoice)
        invoice.status = _status_from_money(invoice)
    invoice.save(update_fields=["amount_paid", "amount_refunded", "status", "updated_at"])
    return invoice


def patient_statement(patient, branch) -> dict:
    """Every bill, payment and refund for a patient, oldest first, with a running balance.

    A refund gives money back and takes the same amount off what was billed, so it does not change
    what is owed. The closing balance always equals the sum of the invoices' balance due.
    """
    invoices = list(
        Invoice.objects.filter(patient=patient, branch=branch)
        .exclude(status__in=(Status.DRAFT, Status.CANCELLED))
        .prefetch_related("payments__method", "refunds__method")
    )
    entries = []
    for inv in invoices:
        entries.append(
            {"at": inv.issued_at or inv.created_at, "number": inv.number, "kind": "Invoice",
             "invoice": inv, "billed": inv.grand_total, "paid": ZERO})  # fmt: skip
        for p in inv.payments.all():
            entries.append(
                {"at": p.created_at, "number": p.number, "kind": f"Payment · {p.method}",
                 "invoice": inv, "billed": ZERO, "paid": p.amount})  # fmt: skip
        for r in inv.refunds.all():
            entries.append(
                {"at": r.created_at, "number": r.number, "kind": f"Refund · {r.method}",
                 "invoice": inv, "billed": -r.amount, "paid": -r.amount})  # fmt: skip
    entries.sort(key=lambda e: e["at"])
    balance = ZERO
    for e in entries:
        balance += e["billed"] - e["paid"]
        e["balance"] = balance
    return {
        "entries": entries,
        "billed": sum((e["billed"] for e in entries), ZERO),
        "paid": sum((e["paid"] for e in entries), ZERO),
        "balance": balance,
    }
