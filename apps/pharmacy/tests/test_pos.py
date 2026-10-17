from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Invoice, InvoiceItem, PaymentMethod, Refund
from apps.encounters import services as enc_services
from apps.pharmacy import pos, stock
from apps.pharmacy.models import InventoryBatch, PharmacySale, Product, StockTransaction
from apps.prescriptions import services as rx_services
from apps.prescriptions.models import Medicine, Prescription

D = Decimal
TODAY = timezone.localdate()
Status = PharmacySale.Status


def _req(rf, user, org, branch):
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    return req


@pytest.fixture
def pharmacist(make_user):
    return make_user("pharmacy", username="pharma")


@pytest.fixture
def req(rf, pharmacist, org, branch):
    return _req(rf, pharmacist, org, branch)


@pytest.fixture
def boss_req(rf, admin_user, org, branch):
    return _req(rf, admin_user, org, branch)


@pytest.fixture
def cash(org):
    billing.ensure_payment_methods(org)
    return PaymentMethod.objects.get(organization=org, code="cash")


@pytest.fixture
def card(org, cash):
    return PaymentMethod.objects.get(organization=org, code="card")


@pytest.fixture
def paracetamol(org):
    return Medicine.objects.create(
        organization=org, brand_name="Panadol", generic_name="Paracetamol", strength="500 mg"
    )


@pytest.fixture
def panadol(org, paracetamol):
    return Product.objects.create(
        organization=org, sku="P-1", barcode="8961000000011", brand_name="Panadol",
        strength="500 mg", generic_name="Paracetamol", medicine=paracetamol, pack_size=10,
        sale_price=D("3"), reorder_level=5,
    )  # fmt: skip


@pytest.fixture
def calpol(org):
    """Same generic as Panadol, different brand: a valid substitute."""
    return Product.objects.create(
        organization=org, sku="P-2", brand_name="Calpol", strength="500 mg",
        generic_name="Paracetamol", sale_price=D("2.5"),
    )  # fmt: skip


@pytest.fixture
def augmentin(org):
    return Product.objects.create(
        organization=org, sku="P-3", brand_name="Augmentin", strength="625 mg",
        generic_name="Co-amoxiclav", sale_price=D("55"), requires_prescription=True,
        tax_percent=D("10"),
    )  # fmt: skip


def _receive(req, product, batch, days, qty, price=None):
    return stock.receive(
        request=req, product=product, batch_number=batch,
        expiry_date=TODAY + timedelta(days=days), quantity=qty, purchase_cost=D("1"),
        sale_price=price,
    ).batch  # fmt: skip


@pytest.fixture
def stocked(req, panadol, calpol, augmentin):
    return {
        "late": _receive(req, panadol, "PAN-LATE", 300, 20),
        "early": _receive(req, panadol, "PAN-EARLY", 100, 5),
        "calpol": _receive(req, calpol, "CAL-1", 200, 50),
        "aug": _receive(req, augmentin, "AUG-1", 200, 12),
    }


@pytest.fixture
def sent_rx(rf, doctor, org, branch, patient, paracetamol):
    """A signed prescription for 15 Panadol, sent to the pharmacy."""
    doc = _req(rf, doctor.user, org, branch)
    enc, _ = enc_services.start(request=doc, patient=patient)
    rx_services.add_item(
        enc, request=doc, medicine=paracetamol,
        data={"dose": "1 tablet", "frequency": "TDS", "duration_value": 5, "duration_unit": "days"},
    )  # fmt: skip
    enc_services.save_note(enc, request=doc, data={"chief_complaint": "Fever"})
    enc_services.save_note(enc, request=doc, data={"provisional_diagnosis": "Flu"})
    enc_services.complete(enc, request=doc)
    rx = enc.prescription
    rx_services.send_to_pharmacy(rx, request=doc)
    rx.refresh_from_db()
    return rx


# --- Cart --------------------------------------------------------------------------------------


def test_add_product_uses_fefo_and_merges(req, panadol, stocked):
    cart = pos.open_cart(req)
    assert pos.open_cart(req) == cart and cart.number == "PS-00001"
    pos.add_product(cart, request=req, product=panadol, quantity=3)
    pos.add_product(cart, request=req, product=panadol, quantity=4)
    lines = {i.batch.batch_number: i.quantity for i in cart.items.select_related("batch")}
    assert lines == {"PAN-EARLY": 5, "PAN-LATE": 2}  # first expiry first, then the next batch
    with pytest.raises(pos.PosError, match="Only 18 more"):
        pos.add_product(cart, request=req, product=panadol, quantity=19)
    t = pos.totals(cart)
    assert (t["units"], t["grand_total"]) == (7, D("21.00"))


def test_quantity_batch_change_and_remove(req, panadol, stocked):
    cart = pos.open_cart(req)
    [item] = pos.add_product(cart, request=req, product=panadol, quantity=2)
    with pytest.raises(pos.PosError, match="Only 5"):
        pos.set_quantity(item, request=req, quantity=6)
    pos.set_quantity(item, request=req, quantity=4)
    item = pos.change_batch(item, request=req, batch=stocked["late"])
    assert item.batch == stocked["late"] and item.quantity == 4
    with pytest.raises(pos.PosError, match="another product"):
        pos.change_batch(item, request=req, batch=stocked["calpol"])
    assert pos.set_quantity(item, request=req, quantity=0) is None
    assert not cart.items.exists()


def test_units_of_measure_wording(panadol, augmentin):
    assert panadol.describe_quantity(25) == "25 tablets (2 strips + 5)"
    assert panadol.describe_quantity(20) == "20 tablets (2 strips)"
    assert panadol.describe_quantity(5) == "5 tablets"
    assert panadol.describe_quantity(1) == "1 tablet"
    assert augmentin.describe_quantity(3) == "3 tablets"  # pack of 1: no pack wording
    panadol.pack_unit = Product.PackUnit.BOX
    assert panadol.pack_breakdown(10) == "1 box" and panadol.pack_breakdown(35) == "3 boxes + 5"
    assert panadol.pack_price == D("30.00")


def test_loose_tablets_from_a_strip(req, panadol, stocked):
    """A strip holds 10; the customer takes 5 loose. Stock and price are per tablet."""
    cart = pos.open_cart(req)
    pos.add_product(cart, request=req, product=panadol, quantity=5)
    assert pos.totals(cart)["grand_total"] == D("15.00")


def test_whole_packs_only(req, panadol, stocked):
    panadol.allow_loose_sale = False
    panadol.save()
    cart = pos.open_cart(req)
    with pytest.raises(pos.PosError, match="whole strips of 10"):
        pos.add_product(cart, request=req, product=panadol, quantity=5)
    [item] = pos.add_product(cart, request=req, product=panadol, quantity=20, batch=stocked["late"])
    with pytest.raises(pos.PosError, match="multiple of 10"):
        pos.set_quantity(item, request=req, quantity=15)
    pos.set_quantity(item, request=req, quantity=10)
    assert not pos.checkout_problems(cart)


def test_add_full_packs_from_the_till(client, pharmacist, panadol, stocked):
    client.force_login(pharmacist)
    client.post(
        reverse("pharmacy:pos-add"), {"product": panadol.pk, "quantity": "2", "qty_unit": "pack"}
    )
    cart = PharmacySale.objects.get(status=Status.OPEN)
    assert pos.totals(cart)["units"] == 20
    resp = client.get(reverse("pharmacy:pos"))
    assert "1 strip + 5" in resp.content.decode()  # 5 from the first-expiry batch, 15 next


def test_expired_batch_is_never_offered_or_sold(req, panadol, stocked):
    InventoryBatch.objects.filter(pk=stocked["early"].pk).update(
        expiry_date=TODAY - timedelta(days=1)
    )
    cart = pos.open_cart(req)
    pos.add_product(cart, request=req, product=panadol, quantity=2)
    assert cart.items.get().batch == stocked["late"]
    with pytest.raises(pos.PosError, match="expired"):
        pos.add_product(
            cart, request=req, product=panadol, quantity=1,
            batch=InventoryBatch.objects.get(pk=stocked["early"].pk),
        )  # fmt: skip


def test_checkout_walk_in_posts_stock_invoice_and_split_payment(
    req, panadol, augmentin, stocked, cash, card
):
    cart = pos.open_cart(req)
    pos.attach_patient(cart, request=req, customer_name="Bilal")
    pos.add_product(cart, request=req, product=panadol, quantity=10)  # 5 early + 5 late
    with pytest.raises(pos.PosError, match="Payments add up"):
        pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "10"}])
    sale = pos.checkout(
        cart, request=req,
        payments=[{"method": cash, "amount": "20"}, {"method": card, "amount": "10",
                                                     "reference": "TXN-1"}],
    )  # fmt: skip
    assert sale.status == Status.COMPLETED and sale.grand_total == D("30.00")
    invoice = sale.invoice
    assert invoice.patient is None and invoice.customer_name == "Bilal"
    assert invoice.status == Invoice.Status.PAID and invoice.payments.count() == 2
    assert set(invoice.items.values_list("kind", flat=True)) == {InvoiceItem.Kind.PHARMACY}
    assert stock.on_hand(req.branch, panadol) == 15
    assert StockTransaction.objects.filter(kind="sale", reference=sale.number).count() == 2
    # Submitting the same cart again never charges twice.
    assert pos.checkout(cart, request=req, payments=[]).pk == sale.pk
    assert invoice.payments.count() == 2
    assert pos.current_cart(req) is None


def test_checkout_rolls_back_when_stock_ran_out(req, rf, make_user, org, branch, panadol,
                                                stocked, cash):  # fmt: skip
    cart = pos.open_cart(req)
    pos.add_product(cart, request=req, product=panadol, quantity=5, batch=stocked["early"])
    other = _req(rf, make_user("pharmacy"), org, branch)  # another till sells the same strip
    rival = pos.open_cart(other)
    pos.add_product(rival, request=other, product=panadol, quantity=3, batch=stocked["early"])
    pos.checkout(rival, request=other, payments=[{"method": cash, "amount": "9"}])
    with pytest.raises(pos.PosError, match="Only 2"):
        pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "15"}])
    cart.refresh_from_db()
    assert cart.status == Status.OPEN and Invoice.objects.count() == 1  # nothing half-saved


def test_prescription_only_needs_prescription(req, augmentin, stocked, cash):
    cart = pos.open_cart(req)
    pos.add_product(cart, request=req, product=augmentin, quantity=1)
    assert "needs a prescription" in pos.checkout_problems(cart)[0]
    with pytest.raises(pos.PosError, match="needs a prescription"):
        pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "60.50"}])


def test_discount_needs_permission_and_is_kept_at_checkout(req, boss_req, panadol, augmentin,
                                                           stocked, cash):  # fmt: skip
    cart = pos.open_cart(req)
    pos.add_product(cart, request=req, product=panadol, quantity=10)
    with pytest.raises(pos.PosError, match="not allowed"):
        pos.set_discount(cart, request=req, amount="5", reason="Staff")
    # A supervisor approves the discount on the pharmacist's cart.
    with pytest.raises(pos.PosError, match="reason"):
        pos.set_discount(cart, request=boss_req, amount="5", reason="")
    pos.set_discount(cart, request=boss_req, amount="5", reason="Staff family")
    sale = pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "25"}])
    assert sale.invoice.invoice_discount == D("5.00") and sale.invoice.grand_total == D("25.00")


def test_hold_resume_and_clear(req, panadol, stocked):
    cart = pos.open_cart(req)
    with pytest.raises(pos.PosError, match="empty"):
        pos.hold(cart, request=req)
    pos.add_product(cart, request=req, product=panadol, quantity=1)
    pos.hold(cart, request=req, note="Customer fetching wallet")
    second = pos.open_cart(req)
    assert second != cart
    pos.add_product(second, request=req, product=panadol, quantity=2)
    pos.resume(cart, request=req)  # the cart in progress is held automatically
    second.refresh_from_db()
    cart.refresh_from_db()
    assert cart.status == Status.OPEN and second.status == Status.HELD
    pos.clear(second, request=req)
    second.refresh_from_db()
    assert second.status == Status.VOIDED and not second.items.exists()


# --- Prescriptions -----------------------------------------------------------------------------


def test_dispense_prescription_partial_then_complete(req, sent_rx, patient, panadol, stocked,
                                                     cash):  # fmt: skip
    lines = pos.prescription_lines(sent_rx, req.branch)
    assert lines[0]["prescribed"] == 15 and lines[0]["in_stock"] == 25
    cart = pos.open_cart(req)
    rx_item = sent_rx.items.get()
    pos.fill_from_prescription(
        cart, request=req, prescription=sent_rx,
        lines=[{"rx_item": rx_item, "product": panadol, "quantity": 10}],
    )  # fmt: skip
    cart.refresh_from_db()
    assert cart.patient == patient and cart.items.filter(prescription_item=rx_item).count() == 2
    pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "30"}])
    sent_rx.refresh_from_db()
    assert sent_rx.dispense_status == Prescription.Dispense.PARTIAL
    cart2 = pos.open_cart(req)
    pos.fill_from_prescription(
        cart2, request=req, prescription=sent_rx,
        lines=[{"rx_item": rx_item, "product": panadol, "quantity": 5}],
    )  # fmt: skip
    pos.checkout(cart2, request=req, payments=[{"method": cash, "amount": "15"}])
    sent_rx.refresh_from_db()
    assert sent_rx.dispense_status == Prescription.Dispense.DISPENSED
    with pytest.raises(pos.PosError, match="not waiting"):
        pos.attach_prescription(pos.open_cart(req), request=req, prescription=sent_rx)


def test_substitution_controls(req, sent_rx, calpol, augmentin, stocked):
    rx_item = sent_rx.items.get()
    cart = pos.open_cart(req)
    pos.attach_prescription(cart, request=req, prescription=sent_rx)
    with pytest.raises(pos.PosError, match="reason for the substitution"):
        pos.add_product(cart, request=req, product=calpol, quantity=5, rx_item=rx_item)
    with pytest.raises(pos.PosError, match="not the same medicine"):
        pos.add_product(
            cart, request=req, product=augmentin, quantity=1, rx_item=rx_item,
            substitution_reason="Out of stock",
        )  # fmt: skip
    [item] = pos.add_product(
        cart, request=req, product=calpol, quantity=5, rx_item=rx_item,
        substitution_reason="Panadol out of stock",
    )  # fmt: skip
    assert item.substituted and item.prescription_item == rx_item


def test_close_partly_dispensed_and_reopen_blocked(rf, req, doctor, org, branch, sent_rx,
                                                   panadol, stocked, cash):  # fmt: skip
    cart = pos.open_cart(req)
    pos.fill_from_prescription(
        cart, request=req, prescription=sent_rx,
        lines=[{"rx_item": sent_rx.items.get(), "product": panadol, "quantity": 4}],
    )  # fmt: skip
    pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "12"}])
    with pytest.raises(pos.PosError, match="why"):
        pos.close_prescription(sent_rx, request=req, note="")
    pos.close_prescription(sent_rx, request=req, note="Patient will buy the rest later")
    sent_rx.refresh_from_db()
    assert sent_rx.dispense_status == Prescription.Dispense.CLOSED
    doc = _req(rf, doctor.user, org, branch)
    with pytest.raises(enc_services.EncounterError, match="dispensed"):
        enc_services.reopen(sent_rx.encounter, request=doc, reason="Change dose")


# --- Returns -----------------------------------------------------------------------------------


def test_return_restocks_and_refunds_pro_rata(req, boss_req, sent_rx, panadol, stocked, cash):
    cart = pos.open_cart(req)
    pos.fill_from_prescription(
        cart, request=req, prescription=sent_rx,
        lines=[{"rx_item": sent_rx.items.get(), "product": panadol, "quantity": 15}],
    )  # fmt: skip
    pos.set_discount(cart, request=boss_req, amount="4.50", reason="Senior citizen")
    sale = pos.checkout(cart, request=req, payments=[{"method": cash, "amount": "40.50"}])
    early = sale.items.get(batch=stocked["early"])
    with pytest.raises(pos.PosError, match="Only 5"):
        pos.return_items(sale, request=req, quantities={early.pk: 6}, reason="Unused")
    ret = pos.return_items(sale, request=req, quantities={early.pk: 5}, reason="Unused")
    assert ret.amount == D("13.50")  # 5 × 3 = 15, less the 10% sale discount
    assert stock.on_hand(req.branch, panadol) == 15
    refund = Refund.objects.get()
    assert refund.amount == D("13.50") and ret.refunds.get() == refund
    sale.invoice.refresh_from_db()
    assert sale.invoice.amount_refunded == D("13.50")
    sent_rx.refresh_from_db()
    assert sent_rx.dispense_status == Prescription.Dispense.PARTIAL


def test_cashier_cannot_use_pos(rf, make_user, org, branch):
    cashier = _req(rf, make_user("cashier"), org, branch)
    with pytest.raises(pos.PosError, match="not allowed"):
        pos.open_cart(cashier)


# --- Views -------------------------------------------------------------------------------------


def test_pos_screen_flow(client, pharmacist, panadol, stocked, cash):
    client.force_login(pharmacist)
    assert client.get(reverse("pharmacy:pos")).status_code == 200
    resp = client.get(reverse("pharmacy:pos-search"), {"q": "8961000000011"})
    assert "Panadol" in resp.content.decode()
    client.post(reverse("pharmacy:pos-add"), {"product": panadol.pk, "quantity": "4"})
    cart = PharmacySale.objects.get(status=Status.OPEN)
    item = cart.items.get()
    client.post(reverse("pharmacy:pos-item", args=[item.pk]), {"quantity": "6"})
    assert cart.items.get().quantity == 4  # only 5 in the first-expiry batch; 6 refused
    client.post(reverse("pharmacy:pos-customer"), {"customer_name": "Walk-in Ali"})
    resp = client.get(reverse("pharmacy:pos-pay"), HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    total = pos.totals(cart)["grand_total"]
    resp = client.post(
        reverse("pharmacy:pos-pay"),
        {"key": "k1", "method_0": cash.pk, "amount_0": str(total), "reference_0": ""},
    )
    assert resp.status_code == 302
    cart.refresh_from_db()
    assert cart.status == Status.COMPLETED
    assert client.get(reverse("pharmacy:sale-receipt", args=[cart.pk])).status_code == 200
    assert client.get(reverse("pharmacy:sale-detail", args=[cart.pk])).status_code == 200
    assert client.get(reverse("pharmacy:sale-list")).status_code == 200


def test_pos_views_need_sale_permission(client, make_user):
    client.force_login(make_user("reception"))
    assert client.get(reverse("pharmacy:pos")).status_code == 403
    assert client.get(reverse("pharmacy:rx-queue")).status_code == 403
