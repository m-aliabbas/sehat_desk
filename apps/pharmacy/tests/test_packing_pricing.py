"""Packing sizes, price changes, lots and the product ledger."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.pharmacy import catalog, pricing, stock
from apps.pharmacy.models import InventoryBatch, Product, ProductPriceChange

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_(make_request, admin_user):
    return make_request(admin_user)


@pytest.fixture
def product(org):
    return Product.objects.create(
        organization=org, sku="P1", brand_name="Panadol", unit="tablet", pack_unit="strip",
        pack_size=10, packs_per_box=10, box_unit="box", purchase_price=D("1"), sale_price=D("2"),
    )  # fmt: skip


def _expiry():
    return timezone.localdate() + timedelta(days=400)


def _post(admin_client, **extra):
    data = {
        "brand_name": "Syringe 5 ml", "unit": "syringe", "pack_unit": "pack", "pack_size": "6",
        "box_unit": "box", "packs_per_box": "1", "pack_cost": "120", "pack_price": "180",
        "cost_per": "pack", "price_per": "pack", "tax_percent": "0", "reorder_packs": "0",
        **extra,
    }  # fmt: skip
    return admin_client.post(reverse("pharmacy:product-create"), data)


def test_syringe_pack_of_six_or_single(admin_client, org):
    assert _post(admin_client).status_code == 302
    p = Product.objects.get(organization=org, brand_name="Syringe 5 ml")
    assert (p.unit, p.pack_size, p.has_boxes) == ("syringe", 6, False)
    assert p.sale_price == D("30") and p.purchase_price == D("20")


def test_box_price_is_what_was_typed(admin_client, org):
    _post(admin_client, unit="tablet", pack_unit="strip", pack_size="10", packs_per_box="3",
          pack_price="355", price_per="box", pack_cost="300", cost_per="box")  # fmt: skip
    p = Product.objects.get(organization=org, brand_name="Syringe 5 ml")
    assert p.box_size == 30
    assert p.box_price == D("355.00")
    assert (p.sale_price * 30).quantize(D("0.01")) == D("355.00")


def test_price_typed_on_a_small_size_still_counts(admin_client, org):
    _post(admin_client, pack_price="35", price_per="unit")
    assert Product.objects.get(organization=org).sale_price == D("35")


def test_bigger_pack_without_a_pack_becomes_the_pack(admin_client, org):
    _post(admin_client, unit="vial", pack_size="1", packs_per_box="5", box_unit="box",
          pack_price="500", price_per="box", pack_cost="400", cost_per="box")  # fmt: skip
    p = Product.objects.get(organization=org)
    assert (p.pack_unit, p.pack_size, p.packs_per_box) == ("box", 5, 1)
    assert p.sale_price == D("100")


def test_bulk_row_prices_are_for_the_biggest_size(request_, org):
    row = {f: "" for f in catalog.BULK_FIELDS} | {
        "brand_name": "Augmentin", "unit": "tablet", "pack_unit": "strip", "pack_size": "6",
        "box_unit": "box", "packs_per_box": "2", "pack_cost": "600", "pack_price": "720",
        "price_per": "box",
    }  # fmt: skip
    products, errors = catalog.add_many([row], request=request_, apply_rules=False)
    assert not errors
    assert products[0].sale_price == D("60")


def test_smallest_unit_is_locked_once_stock_moved(client, make_user, request_, product):
    # Without records.edit_full (admins hold it; tests/test_extra_access.py covers that side).
    client.force_login(make_user("pharmacy"))
    stock.receive(request=request_, product=product, batch_number="A", expiry_date=_expiry(),
                  quantity=10, purchase_cost=D("1"))  # fmt: skip
    response = client.post(
        reverse("pharmacy:product-edit", args=[product.pk]),
        {
            "brand_name": "Panadol", "unit": "capsule", "pack_unit": "strip", "pack_size": "10",
            "box_unit": "box", "packs_per_box": "10", "pack_cost": "100", "pack_price": "200",
            "cost_per": "box", "price_per": "box", "tax_percent": "0",
        },
    )  # fmt: skip
    assert response.status_code == 200
    product.refresh_from_db()
    assert product.unit == "tablet"


def test_same_batch_new_cost_opens_a_lot_and_keeps_old_cost(request_, product):
    first = stock.receive(request=request_, product=product, batch_number="A",
                          expiry_date=_expiry(), quantity=10, purchase_cost=D("1"))  # fmt: skip
    second = stock.receive(request=request_, product=product, batch_number="a",
                           expiry_date=_expiry(), quantity=5, purchase_cost=D("1.5"))  # fmt: skip
    assert first.batch_id != second.batch_id
    assert (second.batch.batch_number, second.batch.lot) == ("A", 2)
    assert first.unit_cost == D("1") and second.unit_cost == D("1.5")
    assert first.batch.label == "A" and second.batch.label == "A (lot 2)"


def test_same_batch_same_cost_joins_the_lot(request_, product):
    for _ in range(2):
        txn = stock.receive(request=request_, product=product, batch_number="A",
                            expiry_date=_expiry(), quantity=5, purchase_cost=D("1"))  # fmt: skip
    assert InventoryBatch.objects.filter(product=product).count() == 1
    assert txn.balance_after == 10


def test_new_sale_price_on_receipt_sets_new_stock_but_not_the_shelf(request_, product):
    old = stock.receive(request=request_, product=product, batch_number="A",
                        expiry_date=_expiry(), quantity=5, purchase_cost=D("1"))  # fmt: skip
    new = stock.receive(request=request_, product=product, batch_number="B",
                        expiry_date=_expiry(), quantity=5, purchase_cost=D("1"),
                        sale_price=D("3"))  # fmt: skip
    old.batch.refresh_from_db()
    product.refresh_from_db()
    assert old.batch.sale_price == D("2") and new.batch.sale_price == D("3")
    assert product.sale_price == D("3")
    change = ProductPriceChange.objects.get(product=product)
    assert change.scope == ProductPriceChange.Scope.RECEIPT
    assert (change.old_price, change.new_price) == (D("2"), D("3"))


def test_price_change_makes_no_batch_and_no_ledger_entry(request_, product):
    txn = stock.receive(request=request_, product=product, batch_number="A",
                        expiry_date=_expiry(), quantity=5, purchase_cost=D("1"))  # fmt: skip
    pricing.change_price(product, request=request_, sale_price=D("4"))
    txn.batch.refresh_from_db()
    assert txn.batch.sale_price == D("2")
    assert InventoryBatch.objects.filter(product=product).count() == 1
    assert product.transactions.count() == 1


def test_reprice_shelf_changes_price_never_cost(request_, product):
    txn = stock.receive(request=request_, product=product, batch_number="A",
                        expiry_date=_expiry(), quantity=5, purchase_cost=D("1"))  # fmt: skip
    change = pricing.change_price(
        product, request=request_, sale_price=D("4"), scope=ProductPriceChange.Scope.SHELF
    )
    txn.batch.refresh_from_db()
    assert txn.batch.sale_price == D("4") and txn.batch.purchase_cost == D("1")
    assert change.units_repriced == 5


def test_reprice_batch_needs_a_reason_and_leaves_the_product(request_, product):
    txn = stock.receive(request=request_, product=product, batch_number="A",
                        expiry_date=_expiry(), quantity=5, purchase_cost=D("1"))  # fmt: skip
    with pytest.raises(stock.StockError):
        pricing.reprice_batch(txn.batch, request=request_, sale_price=D("1"), reason=" ")
    pricing.reprice_batch(txn.batch, request=request_, sale_price=D("1"), reason="Near expiry")
    txn.batch.refresh_from_db()
    product.refresh_from_db()
    assert txn.batch.sale_price == D("1") and product.sale_price == D("2")


def test_editing_without_touching_price_logs_nothing(admin_client, product):
    product.sale_price = D("11.83")
    product.save()
    admin_client.post(
        reverse("pharmacy:product-edit", args=[product.pk]),
        {
            "brand_name": "Panadol 2", "unit": "tablet", "pack_unit": "strip", "pack_size": "10",
            "box_unit": "box", "packs_per_box": "10", "pack_cost": "100.00",
            "pack_price": "1183.00", "cost_per": "box", "price_per": "box", "tax_percent": "0",
        },
    )  # fmt: skip
    product.refresh_from_db()
    assert product.brand_name == "Panadol 2"
    assert not product.price_changes.exists()


def test_statement_has_opening_running_balance_and_closing(request_, product, branch):
    t1 = stock.receive(request=request_, product=product, batch_number="A",
                       expiry_date=_expiry(), quantity=10, purchase_cost=D("1"))  # fmt: skip
    stock.receive(request=request_, product=product, batch_number="B",
                  expiry_date=_expiry(), quantity=5, purchase_cost=D("2"))  # fmt: skip
    stock.sell(t1.batch, request=request_, quantity=3)
    s = stock.product_statement(branch, product)
    assert [r["balance"] for r in s["rows"]] == [10, 15, 12]
    assert (s["opening"], s["total_in"], s["total_out"], s["closing"]) == (0, 15, 3, 12)
    assert s["closing_value"] == D("17.00")
    assert stock.ledger_mismatches(branch) == []


def test_ledger_pages_render(admin_client, request_, product):
    stock.receive(request=request_, product=product, batch_number="A", expiry_date=_expiry(),
                  quantity=10, purchase_cost=D("1"))  # fmt: skip
    for url in (
        reverse("pharmacy:product-ledger", args=[product.pk]),
        reverse("pharmacy:ledger"),
        reverse("pharmacy:product-detail", args=[product.pk]),
    ):
        assert admin_client.get(url).status_code == 200


def test_price_view_changes_price_for_the_chosen_size(admin_client, product):
    admin_client.post(
        reverse("pharmacy:product-price", args=[product.pk]), {"price": "500", "level": "box"}
    )
    product.refresh_from_db()
    assert product.sale_price == D("5")
