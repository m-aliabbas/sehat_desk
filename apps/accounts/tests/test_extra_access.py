from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Permission
from apps.accounts.services import effective_permissions
from apps.pharmacy import stock
from apps.pharmacy.models import Product


def test_admin_gives_and_takes_extra_access(client, admin_client, make_user, branch):
    pharmacist = make_user("pharmacy")
    assert "records.delete" not in effective_permissions(pharmacist, branch)
    page = admin_client.get(reverse("accounts:staff-detail", args=[pharmacist.pk]))
    assert "Extra access" in page.content.decode()
    admin_client.post(
        reverse("accounts:staff-access", args=[pharmacist.pk]),
        {"codes": ["records.delete", "records.edit_full", "admin.not-a-code"]},
    )
    held = effective_permissions(pharmacist, branch)
    assert {"records.delete", "records.edit_full"} <= held
    admin_client.post(reverse("accounts:staff-access", args=[pharmacist.pk]), {"codes": []})
    assert "records.delete" not in effective_permissions(pharmacist, branch)


def test_cannot_give_what_you_do_not_hold(client, make_user, branch, org):
    from apps.accounts.models import Role, RoleAssignment

    role = Role.objects.create(organization=org, name="Staff admin")
    role.permissions.set(
        Permission.objects.filter(code__in=["admin.users.view", "admin.users.manage"])
    )
    boss = make_user()
    RoleAssignment.objects.create(user=boss, role=role)
    other = make_user("pharmacy")
    client.force_login(boss)
    client.post(reverse("accounts:staff-access", args=[other.pk]), {"codes": ["records.delete"]})
    assert "records.delete" not in effective_permissions(other, branch)


def test_full_edit_lets_the_unit_change_after_stock_moved(
    client, make_user, make_request, admin_user, org
):
    product = Product.objects.create(
        organization=org, sku="S-1", brand_name="Cough syrup", unit="tablet", pack_size=1
    )
    stock.receive(
        request=make_request(admin_user), product=product, batch_number="B1",
        expiry_date=timezone.localdate() + timedelta(days=200), quantity=5,
        purchase_cost=Decimal("50"),
    )  # fmt: skip
    pharmacist = make_user("pharmacy")
    client.force_login(pharmacist)
    edit = reverse("pharmacy:product-edit", args=[product.pk])
    data = {
        "brand_name": "Cough syrup", "unit": "bottle", "pack_unit": "bottle", "pack_size": "1",
        "box_unit": "box", "packs_per_box": "1", "pack_cost": "50", "pack_price": "80",
        "cost_per": "unit", "price_per": "unit", "tax_percent": "0",
    }  # fmt: skip
    client.post(edit, data)
    product.refresh_from_db()
    assert product.unit == "tablet"
    pharmacist.extra_permissions.add(*Permission.objects.filter(code="records.edit_full"))
    resp = client.post(edit, data)
    product.refresh_from_db()
    assert resp.status_code == 302
    assert product.unit == "bottle"
