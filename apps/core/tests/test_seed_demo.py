"""Demo data (Phase 31): `manage.py seed_demo` builds the demo hospital through the real
services, and every demo login can open every screen in its menu with that data loaded."""

import re
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from apps.core.navigation import NAV_ITEMS

PASSWORD = "Demo-test-pass-2026!"
NAV_URLS = {item.key: item.url_name for item in NAV_ITEMS if item.url_name}


@pytest.fixture
def demo(db, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    call_command("seed_demo", password=PASSWORD, stdout=StringIO())
    from apps.organizations.models import Organization

    return Organization.objects.get(name="SehatDesk Demo Hospital")


def test_demo_counts_meet_phase_31(demo):
    from apps.accounts.models import User
    from apps.appointments.models import Appointment
    from apps.billing.models import Invoice, Payment
    from apps.encounters.models import Encounter
    from apps.ipd.models import Admission, Bed
    from apps.lab.models import LabOrder
    from apps.masterdata.models import Department, DoctorProfile
    from apps.patients.models import Patient
    from apps.pharmacy.models import InventoryBatch, Product, StockTransaction
    from apps.prescriptions.models import Prescription

    def staff(role):
        return User.objects.filter(organization=demo, assignments__role__system_key=role).count()

    org = {"organization": demo}
    assert demo.branches.count() == 2
    assert Department.objects.filter(**org).count() == 5
    assert DoctorProfile.objects.filter(**org).count() == 8
    assert staff("reception") + staff("cashier") == 4
    assert staff("lab") == 2 and staff("pharmacy") == 2
    assert Patient.objects.filter(**org).count() == 50
    assert Appointment.objects.filter(**org).count() == 30
    assert Encounter.objects.filter(**org, status="completed").count() == 20
    assert Prescription.objects.filter(**org).count() == 15
    assert LabOrder.objects.filter(**org).count() == 20
    assert Product.objects.filter(**org).count() == 50
    assert (
        InventoryBatch.objects.filter(**org).count()
        + StockTransaction.objects.filter(**org).count()
        >= 100
    )
    assert Invoice.objects.filter(**org).count() >= 20
    assert Payment.objects.filter(**org).count() >= 20
    assert Bed.objects.filter(**org).count() == 10
    assert Admission.objects.filter(**org, status__in=Admission.ACTIVE_STATUSES).count() == 3


def test_demo_history_is_spread_over_past_days_and_never_in_the_future(demo):
    from apps.billing.models import Payment
    from apps.encounters.models import Encounter
    from apps.pharmacy.models import InventoryBatch

    days = {
        timezone.localdate(e.completed_at) for e in Encounter.objects.filter(status="completed")
    }
    assert len(days) >= 8
    assert not Payment.objects.filter(created_at__gt=timezone.now()).exists()
    for batch in InventoryBatch.objects.all():  # the stock ledger adds up to every batch
        assert batch.quantity_available == sum(t.quantity for t in batch.transactions.all())


def test_demo_refuses_to_run_twice_or_beside_a_real_hospital(demo, org):
    with pytest.raises(CommandError, match="already loaded"):
        call_command("seed_demo")
    demo.name = "Renamed"
    demo.save()
    with pytest.raises(CommandError, match="already has a hospital"):
        call_command("seed_demo")


def test_demo_logins_written_to_data_folder(demo, settings):
    text = (settings.DATA_DIR / "DEMO_LOGIN.txt").read_text()
    assert PASSWORD in text and "demo.dr.ayesha" in text


@pytest.mark.parametrize(
    "username",
    ["demo.admin", "demo.manager", "demo.reception1", "demo.cashier1", "demo.dr.ayesha",
     "demo.lab1", "demo.pharmacy1", "demo.nurse1", "demo.pharmacy2"],
)  # fmt: skip
def test_every_demo_login_opens_its_menu(demo, client, username):
    response = client.post(
        reverse("accounts:login"), {"username": username, "password": PASSWORD}, follow=True
    )
    assert response.wsgi_request.user.is_authenticated
    urls = set(re.findall(r'href="([^"]+)"', response.content.decode()))
    keys = [key for key, name in NAV_URLS.items() if reverse(name) in urls]
    assert keys
    for key in keys:
        assert client.get(reverse(NAV_URLS[key]), follow=True).status_code == 200, key
