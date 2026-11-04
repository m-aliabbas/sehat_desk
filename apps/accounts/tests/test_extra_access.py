from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.accounts import matrix
from apps.accounts.models import Permission
from apps.accounts.permissions import DELETE_AREAS, DELETE_CODES
from apps.accounts.services import effective_permissions, role_codes
from apps.pharmacy import stock
from apps.pharmacy.models import Product


def _role_ticks(user):
    """What the person's access matrix posts when nothing is changed."""
    return sorted(matrix.person_on(user))


def test_person_plus_and_minus(admin_client, make_user, branch):
    pharmacist = make_user("pharmacy")
    url = reverse("accounts:staff-access", args=[pharmacist.pk])
    assert "Products and stock" in admin_client.get(url).content.decode()
    ticks = _role_ticks(pharmacist)
    assert "pharmacy.product.delete" not in effective_permissions(pharmacist, branch)
    # "+": delete products; "–": no till sales; a code outside the matrix is ignored.
    wanted = [c for c in ticks if c != "pharmacy.sale"]
    admin_client.post(url, {"codes": [*wanted, "pharmacy.product.delete", "admin.not-a-code"]})
    held = effective_permissions(pharmacist, branch)
    assert "pharmacy.product.delete" in held and "pharmacy.sale" not in held
    page = admin_client.get(reverse("accounts:staff-detail", args=[pharmacist.pk])).content
    assert "+ Delete products" in page.decode()
    admin_client.post(url, {"codes": ticks})  # back to the role
    held = effective_permissions(pharmacist, branch)
    assert "pharmacy.product.delete" not in held and "pharmacy.sale" in held
    assert not pharmacist.extra_permissions.exists() and not pharmacist.denied_permissions.exists()


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
    client.post(
        reverse("accounts:staff-access", args=[other.pk]),
        {"codes": [*_role_ticks(other), "pharmacy.product.delete"]},
    )
    assert "pharmacy.product.delete" not in effective_permissions(other, branch)


def test_super_admin_is_never_changed(admin_client, admin_user, branch):
    admin_client.post(reverse("accounts:staff-access", args=[admin_user.pk]), {"codes": []})
    assert "pharmacy.sale" in effective_permissions(admin_user, branch)


def test_role_matrix_sets_area_codes_and_keeps_the_rest(admin_client, org):
    role = org.roles.get(system_key="pharmacy")
    before = role_codes(role)
    url = reverse("accounts:access-matrix")
    assert "Delete products" in admin_client.get(f"{url}?role={role.pk}").content.decode()
    ticks = sorted(before & matrix.CODES)
    admin_client.post(f"{url}?role={role.pk}", {"codes": [*ticks, "pharmacy.product.delete"]})
    assert role_codes(role) == before | {"pharmacy.product.delete"}
    admin_client.post(f"{url}?role={role.pk}", {"codes": []})
    assert role_codes(role) == before - matrix.CODES  # e.g. hr.self stays


def test_old_delete_right_moves_to_each_area_it_could_edit(org):
    from apps.accounts.models import Role
    from apps.accounts.services import sync_permissions

    old = Permission.objects.create(code="records.delete", module="Edit & delete", label="x")
    role = Role.objects.create(organization=org, name="Lab lead")
    role.permissions.set(
        Permission.objects.filter(code__in=["records.delete", "lab.catalog.manage"])
    )
    Permission.objects.filter(code__in=DELETE_CODES).delete()
    sync_permissions()
    codes = role_codes(role)
    assert "lab.catalog.delete" in codes and "pharmacy.product.delete" not in codes
    assert not Permission.objects.filter(pk=old.pk).exists()
    pharmacist = org.roles.get(system_key="pharmacy")
    assert "pharmacy.product.delete" not in role_codes(pharmacist)
    assert DELETE_CODES <= role_codes(org.roles.get(system_key="hospital_admin"))


def test_every_bin_kind_deletes_with_its_area_code():
    from apps.recyclebin.registry import KINDS

    for kind in KINDS.values():
        assert kind.delete_perm in DELETE_AREAS
        assert set(kind.manage_perms) & set(DELETE_AREAS[kind.delete_perm])
        assert kind.delete_perm in matrix.CODES


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
