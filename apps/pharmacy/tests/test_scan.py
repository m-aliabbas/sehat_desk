from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.pharmacy import scan, stock
from apps.pharmacy.models import PharmacySale, Product

GS = "\x1d"
GTIN = "08964000000014"
EAN = "8964000000014"
NEXT_YEAR = (timezone.localdate() + timedelta(days=400)).strftime("%y%m00")
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "text",
    [
        f"01{GTIN}17270331{'10'}AB12{GS}21SER9",
        f"]d201{GTIN}17270331{'10'}AB12{GS}21SER9",
        f"01{GTIN}<GS>17270331{'10'}AB12<GS>21SER9",
        f"(01){GTIN}(17)270331(10)AB12(21)SER9",
        f"https://id.gs1.org/01/{GTIN}/10/AB12/21/SER9?17=270331",
        f"GTIN: {GTIN}\nBatch No: AB12\nExp: 03/27\nSerial: SER9",
        f"GTIN:{GTIN}; LOT:AB12; EXPIRY DATE:31/03/2027; S/N:SER9",
        f'{{"gtin":"{GTIN}","batch":"AB12","expiry":"2027-03-31","serial":"SER9"}}',
    ],
)
def test_every_way_of_writing_a_code_reads_the_same(text):
    read = scan.parse(text)
    assert (read.gtin, read.batch, read.expiry, read.serial) == (
        GTIN, "AB12", date(2027, 3, 31), "SER9",
    )  # fmt: skip
    assert read.code == GTIN and read.structured


def test_dates_are_kept_and_label_text_is_not():
    read = scan.parse(f"01{GTIN}11250101{'710'}REG77{GS}30100{GS}")
    assert read.made == date(2025, 1, 1)
    labelled = scan.parse(f"Name: Panadol\nGTIN: {EAN}\nMfr: GSK\nMRP: 50\nBatch: A1 Price: 9")
    assert (labelled.gtin, labelled.batch) == (EAN, "A1")
    payload = labelled.as_dict()
    assert not {"name", "manufacturer", "price", "extra", "registration"} & set(payload)
    assert "Panadol" not in str(payload) and "50" not in payload["summary"]


def test_day_zero_means_end_of_month():
    assert scan.parse(f"01{GTIN}17270200").expiry == date(2027, 2, 28)


def test_missing_separators_split_on_a_date_and_flag_a_guess():
    sure = scan.parse(f"01{GTIN}10AB1217270331")
    assert (sure.batch, sure.expiry, sure.batch_is_guess) == ("AB12", date(2027, 3, 31), False)
    guess = scan.parse(f"01{GTIN}17270331{'10'}AB1221SER9")
    assert (guess.batch, guess.serial, guess.batch_is_guess) == ("AB12", "SER9", True)


@pytest.mark.parametrize("text", ["panadol 500", "PAN-500", "S260101-0001", "10 mg", "ABC123"])
def test_words_and_plain_codes_stay_as_they_are(text):
    read = scan.parse(text)
    assert not read.structured and read.code == text


def test_a_bad_check_digit_is_not_a_gtin():
    assert not scan.parse("8964000000015").gtin


def test_a_label_with_a_wrong_check_digit_still_gives_its_code():
    read = scan.parse("(01)08964000000015(17)270331(10)A1")
    assert (read.code, read.batch) == ("08964000000015", "A1")


def test_variants_match_every_length():
    assert set(scan.code_variants(GTIN)) == {EAN, GTIN}
    assert scan.code_variants("ABC") == ["ABC"]


def _product(org, barcode=EAN, name="Panadol"):
    return Product.objects.create(
        organization=org, brand_name=name, generic_name="Paracetamol", sku=f"SK{name}",
        barcode=barcode, unit="tablet", pack_size=10, pack_unit="strip",
        purchase_price=Decimal("1"), sale_price=Decimal("2"),
    )  # fmt: skip


def _receive(request, product, number, expiry, qty=20):
    return stock.receive(
        request=request, product=product, batch_number=number, expiry_date=expiry, quantity=qty,
        purchase_cost=Decimal("1"), sale_price=Decimal("2"),
    )  # fmt: skip


def _scan_at_till(client, code):
    return client.post(
        reverse("pharmacy:pos-add"), {"q": code, "quantity": 1}, headers={"HX-Request": "true"}
    )


def _cart_lines(branch):
    return list(
        PharmacySale.objects.get(branch=branch, status=PharmacySale.Status.OPEN).items.all()
    )


def test_till_sells_the_scanned_batch_not_the_first_to_expire(
    admin_client, admin_user, org, branch, make_request
):
    product = _product(org)
    soon = timezone.localdate() + timedelta(days=60)
    later = timezone.localdate() + timedelta(days=300)
    _receive(make_request(admin_user), product, "OLD1", soon)
    _receive(make_request(admin_user), product, "NEW2", later)
    assert _scan_at_till(admin_client, f"01{GTIN}17{later:%y%m%d}10NEW2").status_code == 200
    lines = _cart_lines(branch)
    assert len(lines) == 1
    assert lines[0].batch.batch_number == "NEW2"


def test_till_refuses_an_expired_pack_even_with_good_stock(
    admin_client, admin_user, org, branch, make_request
):
    product = _product(org)
    _receive(make_request(admin_user), product, "NEW2", timezone.localdate() + timedelta(days=300))
    response = _scan_at_till(admin_client, f"01{GTIN}17200131{'10'}OLD1")
    assert "expired" in response["HX-Trigger"]
    assert not PharmacySale.objects.filter(branch=branch, items__isnull=False).exists()


def test_till_warns_when_the_batch_is_not_in_stock(
    admin_client, admin_user, org, branch, make_request
):
    product = _product(org)
    _receive(make_request(admin_user), product, "NEW2", timezone.localdate() + timedelta(days=300))
    response = _scan_at_till(admin_client, f"01{GTIN}17{NEXT_YEAR}10GONE")
    assert "not on this branch" in response["HX-Trigger"]
    assert _cart_lines(branch)[0].batch.batch_number == "NEW2"


def test_stock_grid_search_returns_batch_and_expiry(admin_client, org):
    product = _product(org)
    data = admin_client.get(
        reverse("pharmacy:product-suggest"), {"q": f"01{GTIN}17270331{'10'}AB12"}
    ).json()
    assert data["exact"] and data["results"][0]["id"] == product.pk
    assert (data["scan"]["batch"], data["scan"]["expiry_short"]) == ("AB12", "03/27")


def test_scan_read_names_the_product_and_needs_sign_in(admin_client, org):
    _product(org)
    url = reverse("pharmacy:scan-read")
    data = admin_client.get(url, {"code": f"(01){GTIN}(10)AB12"}).json()
    assert data["scan"]["batch"] == "AB12"
    assert [p["name"] for p in data["products"]] == [Product.objects.get().display_name]
    assert Client().get(url, {"code": EAN}).status_code in (302, 403)


def _form_data(**extra):
    return {
        "brand_name": "New drug", "generic_name": "Newcillin", "unit": "tablet", "pack_size": 1,
        "packs_per_box": 1,
        "box_unit": "box", "pack_unit": "strip", "purchase_price": 1, "sale_price": 2,
        "tax_percent": 0, "reorder_level": 0, "allow_loose_sale": "on", **extra,
    }  # fmt: skip


def test_product_form_keeps_only_the_gtin_of_a_long_code(admin_client, org):
    long_code = f"01{GTIN}17270331{'10'}" + "B" * 20 + f"{GS}21" + "S" * 20
    response = admin_client.post(reverse("pharmacy:product-create"), _form_data(barcode=long_code))
    assert response.status_code == 302, response.context["form"].errors
    assert Product.objects.get(brand_name="New drug").barcode == GTIN


def test_product_form_refuses_the_same_gtin_at_another_length(admin_client, org):
    _product(org, barcode=EAN)
    response = admin_client.post(reverse("pharmacy:product-create"), _form_data(barcode=GTIN))
    assert response.status_code == 200
    assert not Product.objects.filter(brand_name="New drug").exists()


def test_product_list_and_global_search_find_a_scanned_code(admin_client, org):
    product = _product(org)
    _product(org, barcode="", name="Other")
    page = admin_client.get(reverse("pharmacy:product-list"), {"q": f"(01){GTIN}(10)AB12"})
    assert product.brand_name.encode() in page.content and b"Other" not in page.content


# Read from real medicine packs in Pakistan, as a scanner typed them.
@pytest.mark.parametrize(
    ("text", "gtin", "batch", "expiry"),
    [
        ("(01)08964002868292(17)280329(10)043FG6", "08964002868292", "043FG6", date(2028, 3, 29)),
        ("(01)08964001392057(10)AT0540(17)310805", "08964001392057", "AT0540", date(2031, 8, 5)),
        ("(01)08964001824640(10)4EK169(17)280800", "08964001824640", "4EK169", date(2028, 8, 31)),
        (
            "0108964001807346171220281056339240LignocaineGelRs.63.981319122025",
            "08964001807346", "56339", date(2028, 12, 31),
        ),
    ],
)  # fmt: skip
def test_real_packs(text, gtin, batch, expiry):
    read = scan.parse(text)
    assert (read.gtin, read.batch, read.expiry) == (gtin, batch, expiry)
    assert not read.batch_is_guess


def test_real_pack_ignores_name_and_price():
    read = scan.parse(
        "(01)08964000972199(17)280229(10)PY035(11)260313(21)OZ8C1DLW3D"
        "(240)Duprex20mgCap14sRs.663.20"
    )
    assert (read.batch, read.made, read.serial) == ("PY035", date(2026, 3, 13), "OZ8C1DLW3D")
    assert "Duprex" not in str(read.as_dict()) and "663" not in str(read.as_dict())


def test_same_label_with_separators_lost_still_splits():
    read = scan.parse(
        "01089640009721991728022910PY0351126031321OZ8C1DLW3D240Duprex20mgCap14sRs.663.20"
    )
    assert (read.batch, read.serial) == ("PY035", "OZ8C1DLW3D")
    assert read.made == date(2026, 3, 13)


@pytest.mark.parametrize(
    "text",
    [
        "(01)", "(01)12", "()", "01", "0108964", "(17)991399", "(01)08964000000014(17)xx",
        "((((", "https://", "https://x/01", "GTIN:", "Exp: 99/99", "Batch:\n", "\x1d\x1d",
        "]d2", "0108964000000014" + "17", "%%%", "0" * 200, "é" * 50,
    ],
)  # fmt: skip
def test_strange_labels_never_raise(text):
    scan.parse(text).as_dict()


def test_a_bad_expiry_keeps_the_code_and_batch():
    read = scan.parse("(01)08964000000014(17)991399(10)A1")
    assert (read.code, read.batch, read.expiry) == ("08964000000014", "A1", None)
    partial = scan.parse("010896400000001417xx")
    assert partial.code == "08964000000014"


def test_pack_barcode_and_its_qr_code_find_the_same_product(admin_client, org):
    product = _product(org, barcode="8964000972199", name="Duprex")
    qs = Product.objects.filter(organization=org)
    assert scan.find_products(qs, "8964000972199") == [product]
    assert scan.find_products(qs, "(01)08964000972199(17)280229(10)PY035") == [product]
