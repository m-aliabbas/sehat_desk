"""Till returns, table entry, open packs, bulk products, auto orders, barcodes, alerts."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing.models import Refund
from apps.pharmacy import barcodes, catalog, pos, purchasing, stock
from apps.pharmacy.models import PharmacySale, Product, Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_(make_request, make_user):
    user = make_user("pharmacy", username="ph1")
    request = make_request(user)
    request.sd_perms = set(request.sd_perms) | {"pharmacy.refund", "billing.payment.collect"}
    return request


def _product(org, **kw):
    data = {"sku": "P1", "brand_name": "Panadol", "pack_size": 10, "packs_per_box": 10,
            "sale_price": Decimal("5"), "purchase_price": Decimal("3")}  # fmt: skip
    return Product.objects.create(organization=org, **{**data, **kw})


def _stock(request, product, qty=300, batch="B1"):
    return stock.receive(
        request=request,
        product=product,
        batch_number=batch,
        expiry_date=timezone.localdate() + timedelta(days=400),
        quantity=qty,
        purchase_cost=Decimal("3"),
        reference="t",
    )


def _sell(request, product, qty, org, cash):
    cart = pos.open_cart(request)
    pos.add_product(cart, request=request, product=product, quantity=qty)
    total = pos.totals(cart)["grand_total"]
    return pos.checkout(cart, request=request, payments=[{"method": cash, "amount": total}])


def test_units_are_free_text_and_three_levels_show(org):
    p = _product(org, unit="ampoule", pack_unit="tray", box_unit="carton", pack_size=5,
                 packs_per_box=4)  # fmt: skip
    assert p.describe_quantity(27) == "27 ampoules (1 carton + 1 tray + 2)"


def test_till_return_only_restocks_and_refunds(request_, org, cash):
    p = _product(org)
    _stock(request_, p)
    _sell(request_, p, 20, org, cash)
    cart = pos.open_cart(request_)
    pos.add_return(cart, request=request_, product=p, quantity=5)
    assert pos.totals(cart)["net"] == Decimal("-25.00")
    pos.settle_returns_only(cart, request=request_)
    assert stock.on_hand(request_.branch, p) == 285
    assert Refund.objects.get().amount == Decimal("25.00")


def test_row_return_button_and_mixed_cart_at_checkout(request_, org, cash):
    p = _product(org)
    _stock(request_, p)
    _sell(request_, p, 10, org, cash)
    cart = pos.open_cart(request_)
    line = pos.add_product(cart, request=request_, product=p, quantity=2)[0]
    pos.item_to_return(line, request=request_)
    assert not cart.items.exists() and cart.cart_returns.count() == 1
    pos.add_product(cart, request=request_, product=p, quantity=1)
    pos.checkout(cart, request=request_, payments=[{"method": cash, "amount": Decimal("5")}])
    assert stock.on_hand(request_.branch, p) == 291  # 300 - 10 + 2 - 1
    assert Refund.objects.get().amount == Decimal("10.00")
    cart.refresh_from_db()
    assert cart.status == PharmacySale.Status.COMPLETED


def test_cannot_return_more_than_was_sold(request_, org, cash):
    p = _product(org)
    _stock(request_, p)
    _sell(request_, p, 3, org, cash)
    cart = pos.open_cart(request_)
    with pytest.raises(pos.PosError):
        pos.add_return(cart, request=request_, product=p, quantity=4)
    pos.add_return(cart, request=request_, product=p, quantity=3)
    with pytest.raises(pos.PosError):  # already on the cart
        pos.add_return(cart, request=request_, product=p, quantity=1)


def test_return_needs_permission(make_request, make_user, org, request_, cash):
    p = _product(org)
    _stock(request_, p)
    _sell(request_, p, 3, org, cash)
    plain = make_request(make_user("pharmacy", username="ph2"))
    plain.sd_perms = set(plain.sd_perms) - {"pharmacy.refund"}
    with pytest.raises(pos.PosError):
        pos.add_return(pos.open_cart(plain), request=plain, product=p, quantity=1)


def test_till_page_keeps_the_empty_row_and_scan_adds_by_box(client, make_user, request_, org):
    p = _product(org, barcode="111")
    _stock(request_, p)
    client.force_login(make_user("pharmacy", username="ph3"))
    assert 'id="pos-entry"' in client.get("/pharmacy/pos/").content.decode()
    client.post("/pharmacy/pos/add/", {"q": "111", "quantity": "1", "qty_unit": "box"})
    assert PharmacySale.objects.get(status="open").items.get().quantity == 100


def test_add_many_generates_barcodes_and_converts_pack_prices(request_, org):
    products, errors = catalog.add_many(
        [{f: "" for f in catalog.BULK_FIELDS} | {
            "brand_name": "Brufen", "unit": "syringe", "pack_unit": "tray", "pack_size": "10",
            "pack_cost": "100", "pack_price": "150", "tax_percent": "1", "reorder_packs": "2",
            "expiry_days": "30",
        }],
        request=request_,
    )  # fmt: skip
    assert not errors
    p = products[0]
    assert p.unit == "syringe" and p.sale_price == Decimal("15.00") and p.reorder_level == 20
    assert p.tax_percent == 1 and p.expiry_alert_days == 30 and len(p.barcode) == 13


def test_add_many_is_all_or_nothing(request_, org):
    good = {f: "" for f in catalog.BULK_FIELDS} | {"brand_name": "A", "unit": "tablet"}
    bad = good | {"brand_name": "B", "pack_size": "x"}
    products, errors = catalog.add_many([good, bad], request=request_)
    assert not products and 1 in errors
    assert not Product.objects.filter(brand_name="A").exists()


def test_box_cost_reaches_the_supplier_total_exactly(request_, org):
    p = _product(org, pack_size=3, packs_per_box=10)
    vendor = Vendor.objects.create(organization=org, name="V")
    purchase = purchasing.create_purchase(request=request_, vendor=vendor)
    cost = purchasing.cost_per_unit(p, "100", "box")
    purchasing.add_item(purchase, request=request_, product=p, quantity=30, unit_cost=cost)
    purchase.refresh_from_db()
    assert purchase.grand_total == Decimal("100.00")


def test_auto_reorder_groups_by_supplier_and_skips_what_is_on_order(request_, org):
    v1 = Vendor.objects.create(organization=org, name="V1")
    v2 = Vendor.objects.create(organization=org, name="V2")
    a = _product(org, reorder_level=50, preferred_vendor=v1)
    b = _product(org, sku="P2", brand_name="B", reorder_level=50, preferred_vendor=v2)
    rows = purchasing.reorder_suggestions(request_)
    assert {r["product"].pk for r in rows} == {a.pk, b.pk}
    assert rows[0]["packs"] == 10  # up to twice the minimum, whole packs
    made = purchasing.create_reorder_purchases(
        request=request_, lines=[(r["product"], r["vendor"], r["packs"]) for r in rows]
    )
    assert len(made) == 2 and all(p.status == "draft" for p in made)
    assert purchasing.reorder_suggestions(request_) == []


def test_per_product_expiry_alert(request_, org):
    soon = timezone.localdate() + timedelta(days=40)
    short = _product(org, expiry_alert_days=10)
    usual = _product(org, sku="P2", brand_name="B")
    for product in (short, usual):
        stock.receive(request=request_, product=product, batch_number="X", expiry_date=soon,
                      quantity=5, purchase_cost=Decimal("1"), reference="t")  # fmt: skip
    found = {b.product_id for b in stock.near_expiry(request_.branch)}
    assert found == {usual.pk}


def test_code128_is_well_formed():
    assert all(sum(int(w) for w in p) in (11, 13) for p in barcodes._PATTERNS)
    assert len(set(barcodes._PATTERNS)) == 107
    # Known checksum: "PJJ123C" is the standard worked example, check value 55.
    widths = barcodes.code128_widths("PJJ123C")
    assert widths[6 * 8 : 6 * 9] == barcodes._PATTERNS[55]


def test_generated_barcode_is_valid_ean13(org):
    code = barcodes.new_barcode(org)
    assert len(code) == 13 and code.startswith("20")
    assert barcodes.ean13_check_digit(code[:12]) == code[12]


def test_barcode_page_generates_for_existing_and_prints_counts(admin_client, org, request_):
    p = _product(org)
    _stock(request_, p, qty=50)
    assert "Generate 1 missing" in admin_client.get("/inventory/barcodes/").content.decode()
    admin_client.post("/inventory/barcodes/")
    p.refresh_from_db()
    assert p.barcode
    page = admin_client.get(f"/inventory/products/{p.pk}/labels/")
    assert page.context["count"] == 5  # one per pack in stock
    sheet = admin_client.get(f"/inventory/barcodes/print/?count_{p.pk}=3")
    assert len(sheet.context["labels"]) == 3


def _decode_128(widths):
    symbols = [widths[i : i + 6] for i in range(0, len(widths) - 7, 6)]
    return [barcodes._PATTERNS.index(s) for s in symbols]


def test_code128_digits_use_subset_c_with_a_valid_check():
    even = _decode_128(barcodes.code128_widths("123456"))
    assert even[:4] == [105, 12, 34, 56]
    assert even[4] == (105 + 12 + 2 * 34 + 3 * 56) % 103
    odd = _decode_128(barcodes.code128_widths("2000000000017"))
    assert odd[:3] == [104, ord("2") - 32, 99] and odd[3:5] == [0, 0]
    # Narrow enough for a 1.5 inch sticker: bars must stay at least 0.25 mm wide.
    assert sum(int(w) for w in barcodes.code128_widths("2000000000017")) + 20 <= 143
    assert _decode_128(barcodes.code128_widths("AB-1"))[0] == 104


def test_labels_print_on_the_sticker_size(admin_client, org, request_):
    p = _product(org, barcode="2000000000017")
    url = f"/inventory/products/{p.pk}/labels/"
    assert "size: 38.1mm 25.4mm" in admin_client.get(url).content.decode()
    assert "size: 50mm 25mm" in admin_client.get(url + "?layout=50x25").content.decode()
    assert "size: A4" in admin_client.get(url + "?layout=sheet").content.decode()
    bulk = admin_client.get(f"/inventory/barcodes/print/?count_{p.pk}=2")
    assert "size: 38.1mm 25.4mm" in bulk.content.decode()
    assert (
        "size: A4"
        in admin_client.get(
            f"/inventory/barcodes/print/?count_{p.pk}=2&layout=sheet"
        ).content.decode()
    )


def test_new_batch_same_product_in_one_post(admin_client, org, request_):
    p = _product(org)
    vendor = Vendor.objects.create(organization=org, name="V")
    purchase = purchasing.create_purchase(
        request=request_, vendor=vendor, supplier_invoice_number="INV-1"
    )
    later = (timezone.localdate() + timedelta(days=300)).isoformat()
    admin_client.post(
        f"/inventory/purchases/{purchase.pk}/items/add/",
        {"product": [p.pk, p.pk], "qty": ["1", "2"], "unit": ["box", "pack"], "free": ["", ""],
         "cost": ["300", "30"], "tax": ["1", ""], "batch": ["A", "B"],
         "expiry": [later, later], "sale_price": ["", "60"]},
    )  # fmt: skip
    purchase.refresh_from_db()
    assert purchase.items.count() == 2
    purchasing.receive(purchase, request=request_)
    assert stock.on_hand(request_.branch, p) == 120


def test_rx_only_sale_goes_through_unless_admin_requires_it(request_, org, cash):
    from apps.organizations.models import preferences_for

    p = _product(org, requires_prescription=True)
    _stock(request_, p)
    cart = pos.open_cart(request_)
    pos.add_product(cart, request=request_, product=p, quantity=1)
    assert pos.checkout_problems(cart) == []
    assert len(pos.rx_only_without_prescription(cart)) == 1
    prefs = preferences_for(org)
    prefs.pharmacy_require_prescription = True
    prefs.save()
    assert "needs a prescription" in pos.checkout_problems(cart)[0]


def test_row_unit_and_discount_reach_the_invoice(request_, org, cash):
    request_.sd_perms = set(request_.sd_perms) | {"pharmacy.discount"}
    p = _product(org)
    _stock(request_, p)
    cart = pos.open_cart(request_)
    line = pos.add_product(cart, request=request_, product=p, quantity=10)[0]
    assert line.sold_as == (1, "pack")
    pos.set_line_discount(line, request=request_, percent="10")
    total = pos.totals(cart)["grand_total"]
    assert total == Decimal("45.00")
    sale = pos.checkout(cart, request=request_, payments=[{"method": cash, "amount": total}])
    assert sale.invoice.grand_total == Decimal("45.00")
    assert sale.invoice.line_discount_total == Decimal("5.00")


def test_row_discount_needs_permission(request_, org):
    request_.sd_perms = set(request_.sd_perms) - {"pharmacy.discount"}
    p = _product(org)
    _stock(request_, p)
    line = pos.add_product(pos.open_cart(request_), request=request_, product=p, quantity=1)[0]
    with pytest.raises(pos.PosError):
        pos.set_line_discount(line, request=request_, percent="10")


def test_returned_line_with_discount_refunds_what_was_paid(request_, org, cash):
    request_.sd_perms = set(request_.sd_perms) | {"pharmacy.discount"}
    p = _product(org)
    _stock(request_, p)
    cart = pos.open_cart(request_)
    line = pos.add_product(cart, request=request_, product=p, quantity=10)[0]
    pos.set_line_discount(line, request=request_, percent="10")
    pos.checkout(cart, request=request_, payments=[{"method": cash, "amount": Decimal("45")}])
    back = pos.open_cart(request_)
    pos.add_return(back, request=request_, product=p, quantity=10)
    pos.settle_returns_only(back, request=request_)
    assert Refund.objects.get().amount == Decimal("45.00")


def test_create_screen_makes_supplier_bill_with_taxes_and_posts(admin_client, org, request_):
    p = _product(org)
    vendor = Vendor.objects.create(organization=org, name="V")
    later = (timezone.localdate() + timedelta(days=300)).isoformat()
    admin_client.post(
        "/inventory/purchases/new/",
        {"vendor": vendor.pk, "supplier_invoice_number": "B-1", "tax_amount": "10",
         "further_tax": "5", "advance_tax": "2", "post": "1",
         "product": [p.pk], "qty": ["1"], "unit": ["pack"], "free": [""], "cost": ["100"],
         "tax": ["1"], "batch": ["Z"], "expiry": [later], "sale_price": [""]},
    )  # fmt: skip
    purchase = vendor.purchases.get()
    assert purchase.status == "received"
    assert purchase.line_tax == Decimal("1.00")
    assert purchase.grand_total == Decimal("118.00")
    assert vendor.ledger.get().amount == Decimal("118.00")
    report = admin_client.get("/reports/purchase-tax/")
    assert report.context["rows"][0]["gst"] == Decimal("11.00")


def test_refused_purchase_saves_nothing_and_keeps_the_rows(admin_client, org):
    p = _product(org)
    vendor = Vendor.objects.create(organization=org, name="V")
    page = admin_client.post(
        "/inventory/purchases/new/",
        {"vendor": vendor.pk, "post": "1", "product": [p.pk], "qty": ["1"], "unit": ["pack"],
         "free": [""], "cost": ["100"], "tax": [""], "batch": [""], "expiry": [""],
         "sale_price": [""]},
    )  # fmt: skip
    assert page.status_code == 200 and not vendor.purchases.exists()
    assert page.context["initial_rows"][0]["id"] == p.pk


def test_receive_rows_all_or_nothing(admin_client, org, request_):
    a, b = _product(org), _product(org, sku="P2", brand_name="B")
    later = (timezone.localdate() + timedelta(days=300)).isoformat()
    data = {"rows": "1", "product": [a.pk, b.pk], "qty": ["1", "1"], "unit": ["box", "unit"],
            "free": ["", ""], "cost": ["300", "3"], "tax": ["", ""], "batch": ["A", ""],
            "expiry": [later, later], "sale_price": ["", ""]}  # fmt: skip
    assert admin_client.post("/inventory/receive/", data).status_code == 200
    assert stock.on_hand(request_.branch, a) == 0
    data["batch"] = ["A", "B"]
    assert admin_client.post("/inventory/receive/", data).status_code == 302
    assert stock.on_hand(request_.branch, a) == 100


def test_inline_product_and_supplier(admin_client, org):
    made = admin_client.post(
        "/inventory/products/quick/",
        {"brand_name": "Newmed", "packing": "Box of 5 vials", "pack_cost": "50",
         "pack_price": "75"},
    ).json()["result"]  # fmt: skip
    assert made["name"].startswith("Newmed") and made["packSize"] == 5
    assert Product.objects.get(pk=made["id"]).barcode
    assert admin_client.post("/inventory/products/quick/", {"unit": "x"}).status_code == 400
    vendor = admin_client.post(
        "/inventory/suppliers/quick/", {"name": "Quick Co", "phone": "0300 1234567"}
    ).json()
    assert Vendor.objects.get(pk=vendor["id"]).name == "Quick Co"


def test_preview_and_receipt_can_be_framed_but_others_cannot(admin_client, org):
    page = admin_client.get("/settings/print-layouts/preview/")
    assert page["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in page["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in admin_client.get("/inventory/")["Content-Security-Policy"]


def _patient_cart(request_, patient):
    cart = pos.open_cart(request_)
    pos.attach_patient(cart, request=request_, patient=patient)
    return cart


def test_paper_rx_starts_empty_and_sold_medicines_go_on_it(request_, org, cash, patient, doctor):
    p = _product(org, requires_prescription=True)
    _stock(request_, p)
    cart = _patient_cart(request_, patient)
    pos.attach_paper_prescription(cart, request=request_, doctor=doctor)
    cart.refresh_from_db()
    rx = cart.prescription
    assert rx.status == "paper" and rx.encounter is None and not rx.items.exists()
    pos.add_product(cart, request=request_, product=p, quantity=10)
    assert pos.rx_only_without_prescription(cart)  # linked only once sold
    pos.checkout(cart, request=request_, payments=[{"method": cash, "amount": Decimal("50")}])
    item = rx.items.get()
    assert item.added_at_pharmacy and item.added_by == request_.user and item.quantity == 10
    assert item.dose == "" and item.frequency == ""  # never invents the doctor's directions
    assert cart.items.get().prescription_item == item
    rx.refresh_from_db()
    assert rx.dispense_status == "dispensed"


def test_paper_rx_needs_a_patient(request_, doctor):
    with pytest.raises(pos.PosError, match="patient"):
        pos.attach_paper_prescription(pos.open_cart(request_), request=request_, doctor=doctor)


def test_detaching_an_unused_paper_rx_removes_it(request_, patient, doctor):
    from apps.prescriptions.models import Prescription

    cart = _patient_cart(request_, patient)
    pos.attach_paper_prescription(cart, request=request_, doctor=doctor)
    pos.detach_prescription(cart, request=request_)
    assert not Prescription.objects.filter(patient=patient).exists()


def test_paper_rx_prints_and_till_warns_of_allergy(
    client, make_user, request_, org, patient, doctor
):
    from apps.patients.models import Allergy

    Allergy.objects.create(patient=patient, substance="Panadol", recorded_by=request_.user)
    p = _product(org)
    _stock(request_, p)
    user = make_user("pharmacy", username="ph7")
    client.force_login(user)
    client.post("/pharmacy/pos/add/", {"product": p.pk, "quantity": "1"})
    client.post("/pharmacy/pos/customer/", {"patient": patient.pk})
    client.post("/pharmacy/pos/prescription/", {"action": "paper", "doctor": doctor.pk})
    cart = PharmacySale.objects.get(status="open", cashier=user)
    assert cart.prescription.doctor == doctor
    assert "Allergy: Panadol" in client.get("/pharmacy/pos/").content.decode()
    page = client.get(f"/prescriptions/{cart.prescription.pk}/print/")
    assert page.status_code == 200 and "Handwritten" in page.content.decode()


def test_till_shows_discount_to_the_default_pharmacist(client, make_user, request_, org):
    p = _product(org)
    _stock(request_, p)
    client.force_login(make_user("pharmacy", username="ph-d"))
    client.post("/pharmacy/pos/add/", {"product": p.pk, "quantity": "1"})
    html = client.get("/pharmacy/pos/").content.decode()
    assert "Disc %" in html and "Discount on whole sale" in html


def test_discount_trail_records_who_gave_and_who_asked(
    admin_client, request_, org, cash, make_user
):
    doctor_user = make_user("doctor", username="dr-ask")
    p = _product(org)
    _stock(request_, p)
    cart = pos.open_cart(request_)
    line = pos.add_product(cart, request=request_, product=p, quantity=10)[0]
    pos.set_line_discount(line, request=request_, percent="10")
    pos.set_discount_note(cart, request=request_, asked_by=doctor_user, reason="Doctor's family")
    pos.checkout(cart, request=request_, payments=[{"method": cash, "amount": Decimal("45")}])
    row = admin_client.get("/reports/discount-trail/").context["rows"][0]
    assert row["given"] == str(request_.user) and row["asked"] == str(doctor_user)
    assert row["amount"] == Decimal("5.00") and row["reason"] == "Doctor's family"


def _rules(org, **rules):
    from apps.organizations.models import preferences_for

    prefs = preferences_for(org)
    prefs.field_rules = {k.replace("__", "."): v for k, v in rules.items()}
    prefs.save()


def test_rules_are_optional_by_default_and_enforced_when_required(request_, org, make_user):
    p = _product(org)
    _stock(request_, p)
    cart = pos.open_cart(request_)
    pos.add_product(cart, request=request_, product=p, quantity=1)
    assert pos.checkout_problems(cart) == []
    _rules(org, till__customer="required")
    assert "customer" in pos.checkout_problems(cart)[0]


def test_required_product_field_blocks_the_grid_not_the_inline_add(admin_client, request_, org):
    _rules(org, product__generic="required")
    row = {f: "" for f in catalog.BULK_FIELDS} | {"brand_name": "A", "unit": "tablet"}
    assert catalog.add_many([row], request=request_)[0] == []
    assert catalog.add_many([row], request=request_, apply_rules=False)[0]


def test_hidden_gst_leaves_the_purchase_grid(admin_client, org):
    assert "GST %" in admin_client.get("/inventory/receive/").content.decode()
    _rules(org, purchase__gst="hidden")
    assert "GST %" not in admin_client.get("/inventory/receive/").content.decode()


def test_fields_page_saves_rules_and_refuses_unknown_ones(admin_client, org):
    from apps.organizations.field_rules import rule
    from apps.organizations.models import OrganizationPreferences

    admin_client.post(
        "/settings/fields/",
        {"rule:till.customer": "required", "rule:product.generic": "hidden",
         "default_purchase_gst_percent": "18", "default_sale_tax_percent": "0"},
    )  # fmt: skip
    org = type(org).objects.get(pk=org.pk)
    assert rule(org, "till.customer") == "required"
    assert rule(org, "product.generic") == "optional"  # hidden is not allowed for this one
    assert OrganizationPreferences.objects.get(organization=org).default_purchase_gst_percent == 18


def test_home_screens(client, make_user):
    client.force_login(make_user("pharmacy", username="h-ph"))
    assert client.get("/")["Location"] == "/pharmacy/pos/"
    assert client.get("/?view=overview").status_code == 200
    client.force_login(make_user("reception", username="h-rc"))
    assert client.get("/")["Location"] == "/reception/"
    client.force_login(make_user("doctor", username="h-dr"))
    assert client.get("/").status_code == 200


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Strip of 10 tablets", ("tablet", "strip", 10, 1)),
        ("box of 10 strips of 14 Capsules", ("capsule", "strip", 14, 10)),
        ("Box of 5 vials", ("vial", "box", 5, 1)),
        ("Bottle", ("bottle", "bottle", 1, 1)),
        ("Tray of 6 Syringes", ("syringe", "tray", 6, 1)),
    ],
)
def test_packing_phrase(text, expected):
    from apps.pharmacy.packing import parse_packing

    p = parse_packing(text)
    assert (p["unit"], p["pack_unit"], p["pack_size"], p["packs_per_box"]) == expected


def test_bad_packing_tells_what_to_type(request_, org):
    row = {f: "" for f in catalog.BULK_FIELDS} | {"brand_name": "A", "packing": "ten pills"}
    products, errors = catalog.add_many([row], request=request_)
    assert not products and "Strip of 10" in errors[0]


def test_one_line_splits_strength_and_takes_rx_from_category(request_, org):
    from apps.pharmacy.models import ProductCategory
    from apps.pharmacy.packing import packing_text

    category = ProductCategory.objects.create(
        organization=org, name="Rx", requires_prescription=True
    )
    row = {f: "" for f in catalog.BULK_FIELDS} | {
        "brand_name": "Panadol 500 mg", "packing": "Box of 10 strips of 10 tablets",
        "category": str(category.pk), "pack_cost": "30", "pack_price": "50",
    }  # fmt: skip
    p = catalog.add_many([row], request=request_)[0][0]
    assert p.brand_name == "Panadol" and p.strength == "500 mg" and p.requires_prescription
    assert p.sale_price == Decimal("5.00")
    assert packing_text(p) == "Box of 10 strips of 10 tablets"


def test_full_form_takes_packing(admin_client, org):
    admin_client.post(
        "/inventory/products/new/",
        {"brand_name": "Ampi", "packing": "Box of 5 vials", "purchase_price": "1",
         "sale_price": "2",
         "tax_percent": "0", "reorder_level": "0"},
    )  # fmt: skip
    p = Product.objects.get(brand_name="Ampi")
    assert p.unit == "vial" and p.pack_size == 5 and p.pack_unit == "box"


def test_expiry_month_year_means_end_of_month():
    from datetime import date

    from apps.pharmacy.views_purchasing import parse_expiry

    assert parse_expiry("02/28") == date(2028, 2, 29)
    assert parse_expiry("12/2027") == date(2027, 12, 31)
    assert parse_expiry("2027-05-01") == date(2027, 5, 1)


def test_till_quantity_prefix_and_row_unit(client, make_user, request_, org):
    p = _product(org, barcode="222")
    _stock(request_, p)
    user = make_user("pharmacy", username="ph-q")
    client.force_login(user)
    client.post("/pharmacy/pos/add/", {"q": "3*222", "quantity": "1"})
    line = PharmacySale.objects.get(status="open", cashier=user).items.get()
    assert line.quantity == 3
    client.post(f"/pharmacy/pos/items/{line.pk}/", {"quantity": "3", "unit": "pack"})
    line.refresh_from_db()
    assert (
        line.quantity == 30 and line.sold_as == (3, "pack") and line.sold_price == Decimal("50.00")
    )


def test_paying_returns_to_the_till_and_prints(client, make_user, request_, org, cash):
    p = _product(org)
    _stock(request_, p)
    user = make_user("pharmacy", username="ph-p")
    client.force_login(user)
    client.post("/pharmacy/pos/add/", {"product": p.pk, "quantity": "1"})
    response = client.post(
        "/pharmacy/pos/pay/", {"method_0": cash.pk, "amount_0": "5"}, HTTP_HX_REQUEST="true"
    )
    url = response["HX-Redirect"]
    assert url.startswith("/pharmacy/pos/?sale=")
    html = client.get(url).content.decode()
    assert "receipt printing" in html and "?print=1" in html


def test_uom_suggests_saved_and_already_used_phrases(admin_client, org):
    from apps.pharmacy.catalog import packing_suggestions

    _product(org, unit="ampoule", pack_unit="tray", pack_size=6, packs_per_box=1)
    admin_client.post("/inventory/uom/", {"name": "strip of 12 tablets"})
    options = packing_suggestions(org)
    assert "Strip of 12 tablets" in options and "Tray of 6 ampoules" in options
    assert "Tray of 6 ampoules" in admin_client.get("/inventory/products/bulk/").content.decode()
    admin_client.post("/inventory/uom/", {"name": "nonsense 12 x"})
    assert not any("nonsense" in o for o in packing_suggestions(org))


def test_bad_expiry_names_the_row(admin_client, org):
    p = _product(org)
    vendor = Vendor.objects.create(organization=org, name="V")
    page = admin_client.post(
        "/inventory/purchases/new/",
        {"vendor": vendor.pk, "product": [p.pk], "qty": ["1"], "unit": ["pack"], "free": [""],
         "cost": ["10"], "tax": [""], "batch": ["A"], "expiry": ["20/2024"], "sale_price": [""]},
    )  # fmt: skip
    assert "should be month and year" in page.content.decode()
    assert not vendor.purchases.exists()


def test_full_form_prices_are_per_pack(admin_client, org):
    admin_client.post(
        "/inventory/products/new/",
        {"brand_name": "Ampi", "packing": "Strip of 10 tablets", "pack_cost": "30",
         "pack_price": "50", "reorder_packs": "2", "tax_percent": "0"},
    )  # fmt: skip
    p = Product.objects.get(brand_name="Ampi")
    assert p.sale_price == Decimal("5.00") and p.purchase_price == Decimal("3.00")
    assert p.reorder_level == 20


def test_till_picks_the_earliest_expiry_and_skips_expired(request_, org):
    p = _product(org)
    today = timezone.localdate()
    for name, days in (("LATE", 300), ("SOON", 60)):
        stock.receive(request=request_, product=p, batch_number=name,
                      expiry_date=today + timedelta(days=days), quantity=5,
                      purchase_cost=Decimal("1"), reference="t")  # fmt: skip
    lines = pos.add_product(pos.open_cart(request_), request=request_, product=p, quantity=7)
    assert [(i.batch.batch_number, i.quantity) for i in lines] == [("SOON", 5), ("LATE", 2)]
