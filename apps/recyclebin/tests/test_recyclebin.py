from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.masterdata.models import Service, ServiceCategory
from apps.pharmacy import stock
from apps.pharmacy.models import Product, ProductCategory, UnitOfMeasure, Vendor
from apps.recyclebin import services

TODAY = timezone.localdate()


@pytest.fixture
def product(org):
    return Product.objects.create(organization=org, sku="P-1", brand_name="Panadol")


def test_delete_hides_and_restore_brings_back(make_request, admin_user, product):
    services.delete(make_request(admin_user), product, reason="Duplicate")
    assert not Product.objects.filter(pk=product.pk).exists()
    gone = Product.all_objects.get(pk=product.pk)
    assert gone.deleted_by == admin_user and gone.deleted_reason == "Duplicate"
    services.restore(make_request(admin_user), gone)
    assert Product.objects.filter(pk=product.pk).exists()


def test_same_sku_can_be_reused_after_delete_but_restore_then_refuses(
    make_request, admin_user, product, org
):
    services.delete(make_request(admin_user), product)
    Product.objects.create(organization=org, sku="P-1", brand_name="Panadol new")
    with pytest.raises(services.BinError, match="same name or code"):
        services.restore(make_request(admin_user), Product.all_objects.get(pk=product.pk))


def test_product_with_stock_cannot_be_deleted(make_request, admin_user, product):
    stock.receive(
        request=make_request(admin_user), product=product, batch_number="B1",
        expiry_date=TODAY + timedelta(days=90), quantity=5, purchase_cost=Decimal("1"),
    )  # fmt: skip
    with pytest.raises(services.BinError, match="still in stock"):
        services.delete(make_request(admin_user), product)


def test_category_in_use_cannot_be_deleted(make_request, admin_user, org, product):
    cat = ProductCategory.objects.create(organization=org, name="Analgesics")
    product.category = cat
    product.save()
    with pytest.raises(services.BinError, match="use it"):
        services.delete(make_request(admin_user), cat)


def test_delete_needs_permission(make_request, make_user, product):
    pharmacist = make_user("pharmacy")
    with pytest.raises(services.BinError, match="not allowed"):
        services.delete(make_request(pharmacist), product)
    pharmacist.extra_permissions.add(*_perms("records.delete"))
    services.delete(make_request(pharmacist), product)


def _perms(*codes):
    from apps.accounts.models import Permission

    return Permission.objects.filter(code__in=codes)


def test_erase_only_unused(make_request, admin_user, org):
    uom = UnitOfMeasure.objects.create(organization=org, name="Strip of 10")
    services.delete(make_request(admin_user), uom)
    services.erase(make_request(admin_user), UnitOfMeasure.all_objects.get(pk=uom.pk))
    assert not UnitOfMeasure.all_objects.filter(pk=uom.pk).exists()


def test_screens(client, admin_user, make_user, org, product):
    vendor = Vendor.objects.create(organization=org, name="Ali Traders")
    category = ServiceCategory.objects.create(organization=org, name="Nursing")
    service = Service.objects.create(
        organization=org, category=category, name="Dressing", code="DRS", price=Decimal("100")
    )
    client.force_login(admin_user)
    resp = client.post(
        reverse("recyclebin:delete-selected", args=["product"]),
        {"ids": [product.pk], "next": reverse("pharmacy:product-list")},
    )
    assert resp.status_code == 302 and resp.url == reverse("pharmacy:product-list")
    client.post(reverse("recyclebin:delete", args=["vendor", vendor.pk]), {"reason": "Closed"})
    client.post(reverse("recyclebin:delete", args=["service", service.pk]))
    page = client.get(reverse("recyclebin:list")).content.decode()
    assert "Panadol" in page and "Ali Traders" in page and "Dressing" in page
    assert "Panadol" not in client.get(reverse("pharmacy:product-list")).content.decode()
    client.post(reverse("recyclebin:restore-selected"), {"ids": [f"product:{product.pk}"]})
    assert Product.objects.filter(pk=product.pk).exists()
    client.force_login(make_user("pharmacy"))
    assert client.get(reverse("recyclebin:list")).status_code == 403
    resp = client.post(reverse("recyclebin:delete", args=["product", product.pk]))
    assert resp.status_code == 403


def test_other_hospital_record_is_404(client, admin_user, make_user):
    from apps.organizations.models import Organization

    other = Organization.objects.create(name="Other")
    foreign = Product.objects.create(organization=other, sku="X", brand_name="Foreign")
    client.force_login(admin_user)
    resp = client.post(reverse("recyclebin:delete", args=["product", foreign.pk]))
    assert resp.status_code == 404
