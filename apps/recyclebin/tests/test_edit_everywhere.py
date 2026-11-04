"""Edit and delete on every setup list, each behind its own permission."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.billing.models import PaymentMethod
from apps.encounters.models import DiagnosisCode
from apps.finance.models import ExpenseCategory
from apps.ipd.models import Bed, Room, Ward
from apps.masterdata.models import ServiceCategory, Specialty
from apps.prescriptions.models import Medicine
from apps.queue.models import Counter
from apps.recyclebin import services
from apps.recyclebin.registry import KINDS


@pytest.fixture
def ward(org, branch):
    return Ward.objects.create(organization=org, branch=branch, name="General", code="GW")


@pytest.fixture
def room(ward):
    return Room.objects.create(
        organization=ward.organization, branch=ward.branch, ward=ward, name="1",
        daily_rate=Decimal("1000"),
    )  # fmt: skip


@pytest.fixture
def bed(room):
    return Bed.objects.create(
        organization=room.organization, branch=room.branch, room=room, code="GW-1"
    )


def _records(org, branch, bed):
    return {
        "specialty": Specialty.objects.create(organization=org, name="Cardiology"),
        "service_category": ServiceCategory.objects.create(organization=org, name="Dressings"),
        "expense_category": ExpenseCategory.objects.create(
            organization=org,
            name="Tea",
            segment=ExpenseCategory._meta.get_field("segment").choices[0][0],
        ),  # fmt: skip
        "payment_method": PaymentMethod.objects.create(organization=org, code="jazz", name="Jazz"),
        "bed": bed,
        "diagnosis_code": DiagnosisCode.objects.create(organization=org, code="X1", title="Fever"),
        "medicine": Medicine.objects.create(
            organization=org, brand_name="Brufen", generic_name="Ibuprofen"
        ),
        "counter": Counter.objects.create(organization=org, branch=branch, code="09", name="Desk"),
    }


def test_admin_deletes_and_restores_every_kind(client, admin_user, org, branch, bed):
    client.force_login(admin_user)
    for key, obj in _records(org, branch, bed).items():
        model = type(obj)
        resp = client.post(reverse("recyclebin:delete", args=[key, obj.pk]))
        assert resp.status_code == 302, key
        assert not model.objects.filter(pk=obj.pk).exists(), key
        assert model.all_objects.get(pk=obj.pk).deleted_by == admin_user, key
    page = client.get(reverse("recyclebin:list")).content.decode()
    for text in ("Cardiology", "Dressings", "Tea", "Jazz", "GW-1", "Fever", "Brufen", "Desk"):
        assert text in page
    client.post(reverse("recyclebin:restore", args=["bed", bed.pk]))
    assert Bed.objects.filter(pk=bed.pk).exists()


def test_shared_edit_page_saves_and_logs(client, admin_user, org, branch, bed, room):
    client.force_login(admin_user)
    records = _records(org, branch, bed)
    url = reverse("recyclebin:edit", args=["specialty", records["specialty"].pk])
    assert client.get(url).status_code == 200
    client.post(url, {"name": "Cardiac sciences"})
    assert Specialty.objects.get().name == "Cardiac sciences"
    other_room = Room.objects.create(
        organization=org, branch=branch, ward=room.ward, name="2", daily_rate=Decimal("900")
    )
    client.post(reverse("recyclebin:edit", args=["bed", bed.pk]),
                {"room": other_room.pk, "code": "GW-9"})  # fmt: skip
    bed.refresh_from_db()
    assert (bed.room, bed.code) == (other_room, "GW-9")
    from apps.audit.models import AuditLog

    assert AuditLog.objects.filter(action="bed.update").exists()


def test_edit_refuses_a_duplicate_name(client, admin_user, org):
    Specialty.objects.create(organization=org, name="Cardiology")
    second = Specialty.objects.create(organization=org, name="Neurology")
    client.force_login(admin_user)
    resp = client.post(reverse("recyclebin:edit", args=["specialty", second.pk]),
                       {"name": "Cardiology"})  # fmt: skip
    assert resp.status_code == 200  # form shown again with the error
    assert "Another specialty already has this name" in resp.content.decode()
    assert Specialty.objects.get(pk=second.pk).name == "Neurology"


def test_edit_dropdowns_only_offer_this_hospital(client, admin_user, org, bed):
    from apps.organizations.models import Branch, Organization

    other = Organization.objects.create(name="Other")
    far = Branch.objects.create(organization=other, name="Far", code="FR")
    foreign_ward = Ward.objects.create(organization=other, branch=far, name="W", code="W")
    foreign_room = Room.objects.create(
        organization=other, branch=far, ward=foreign_ward, name="X", daily_rate=Decimal("1")
    )
    client.force_login(admin_user)
    resp = client.post(reverse("recyclebin:edit", args=["bed", bed.pk]),
                       {"room": foreign_room.pk, "code": "Z"})  # fmt: skip
    assert resp.status_code == 200
    bed.refresh_from_db()
    assert bed.room_id != foreign_room.pk
    foreign_bed = Bed.objects.create(organization=other, branch=far, room=foreign_room, code="Q")
    assert client.get(reverse("recyclebin:edit", args=["bed", foreign_bed.pk])).status_code == 404


def test_each_kind_needs_its_own_permission(client, make_user, org, branch, bed):
    records = _records(org, branch, bed)
    pharmacist = make_user("pharmacy")
    client.force_login(pharmacist)
    assert client.get(reverse("recyclebin:edit", args=["bed", bed.pk])).status_code == 403
    pharmacist.extra_permissions.add(*_perms("pharmacy.product.delete"))
    resp = client.post(reverse("recyclebin:delete", args=["bed", bed.pk]))
    assert resp.status_code == 302 and Bed.objects.filter(pk=bed.pk).exists()  # refused
    client.post(reverse("recyclebin:delete", args=["medicine", records["medicine"].pk]))
    assert Medicine.objects.exists()  # deleting products is not deleting medicines
    pharmacist.extra_permissions.add(*_perms("masterdata.medicine.delete"))
    client.post(reverse("recyclebin:delete", args=["medicine", records["medicine"].pk]))
    assert not Medicine.objects.exists()  # pharmacists manage medicines


def _perms(*codes):
    from apps.accounts.models import Permission

    return Permission.objects.filter(code__in=codes)


def test_blockers(make_request, admin_user, org, branch, bed, room, ward):
    req = make_request(admin_user)
    with pytest.raises(services.BinError, match="room"):
        services.delete(req, ward)
    with pytest.raises(services.BinError, match="bed"):
        services.delete(req, room)
    Bed.objects.filter(pk=bed.pk).update(status=Bed.Status.OCCUPIED)
    with pytest.raises(services.BinError, match="patient"):
        services.delete(req, Bed.objects.get(pk=bed.pk))
    cash = PaymentMethod.objects.create(organization=org, code="cash", name="Cash")
    with pytest.raises(services.BinError, match="Cash"):
        services.delete(req, cash)


def test_list_screens_show_the_buttons(admin_client, org, branch, bed):
    from apps.lab.models import LabTestCategory

    _records(org, branch, bed)
    LabTestCategory.objects.create(organization=org, name="Haematology")
    from apps.pharmacy.models import UnitOfMeasure

    UnitOfMeasure.objects.create(organization=org, name="Strip of 10")
    for name in ("masterdata:department-list", "masterdata:service-list", "finance:category-list",
                 "billing:settings", "ipd:setup", "lab:catalog", "encounters:diagnosis-code-list",
                 "prescriptions:medicine-list", "queue:counters", "pharmacy:uom"):  # fmt: skip
        page = admin_client.get(reverse(name)).content.decode()
        assert "goes to the Recycle bin" in page, name


def test_every_kind_has_an_edit_way():
    assert all(k.edit_url or k.edit_fields for k in KINDS.values())
