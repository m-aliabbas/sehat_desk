"""Tenant and branch isolation with real records (Phase 26/27).

One hospital's records are opened by direct ID:
- by a Super admin of ANOTHER hospital: every record must be a 404 (it does not exist for them);
- by a Super admin of the SAME hospital limited to another branch: branch records are 404,
  hospital-wide records (patients, staff, products) stay visible.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import RoleAssignment
from apps.accounts.services import seed_default_roles
from apps.appointments.tests.test_appointments import _appt
from apps.encounters import services as encounters
from apps.ipd.tests.helpers import admit, make_request, make_ward
from apps.lab import services as lab
from apps.lab.tests.test_lab import catalogue, tech  # noqa: F401 (fixtures)
from apps.organizations.models import Branch, Organization
from apps.pharmacy.models import Product
from apps.queue import services as queue


@pytest.fixture
def world(rf, org, branch, admin_user, doctor, department, patient, catalogue, tech):  # noqa: F811
    req = make_request(rf, admin_user, org, branch)
    doc_req = make_request(rf, doctor.user, org, branch)
    appt = _appt(org, branch, patient, doctor, admin_user, timezone.localdate() + timedelta(days=1))
    token = queue.issue_token(request=req, patient=patient, department=department, doctor=doctor)
    queue.call(token, request=req, doctor=doctor)
    token.refresh_from_db()
    encounter, _ = encounters.start(request=doc_req, patient=patient, token=token)
    order = lab.create_order(
        request=make_request(rf, tech, org, branch), patient=patient, tests=[catalogue["hb"]]
    )
    _, _, beds = make_ward(org, branch)
    admission = admit(req, patient, doctor, beds[0])
    product = Product.objects.create(organization=org, sku="P-9", brand_name="Panadol")
    branch_records = {
        "appointment": reverse("appointments:detail", args=[appt.pk]),
        "appointment slip": reverse("appointments:slip", args=[appt.pk]),
        "token slip": reverse("queue:slip", args=[token.pk]),
        "invoice": reverse("billing:invoice-detail", args=[order.invoice_id]),
        "invoice print": reverse("billing:invoice-print", args=[order.invoice_id]),
        "lab order": reverse("lab:order-detail", args=[order.pk]),
        "lab order print": reverse("lab:order-print", args=[order.pk]),
        "lab report": reverse("lab:report", args=[order.pk]),
        "admission": reverse("ipd:admission-detail", args=[admission.pk]),
        "admission print": reverse("ipd:admission-print", args=[admission.pk]),
        "bed": reverse("ipd:bed-detail", args=[beds[0].pk]),
    }
    org_records = {
        "patient": reverse("patients:detail", args=[patient.pk]),
        "registration slip": reverse("patients:slip", args=[patient.pk]),
        "staff": reverse("accounts:staff-detail", args=[doctor.user.pk]),
        "product": reverse("pharmacy:product-detail", args=[product.pk]),
        "encounter": reverse("encounters:detail", args=[encounter.pk]),
    }
    return branch_records, org_records


@pytest.fixture
def outsider(make_user):
    other = Organization.objects.create(name="Other hospital")
    seed_default_roles(other)
    Branch.objects.create(organization=other, name="Other main", code="OTH", is_main=True)
    return make_user("super_admin", organization=other, username="outsider")


@pytest.fixture
def branch2_admin(make_user, branch2):
    user = make_user(username="b2admin")
    role = user.organization.roles.get(system_key="super_admin")
    RoleAssignment.objects.create(user=user, role=role, branch=branch2)
    return user


def test_owner_can_open_everything(client, admin_user, world):
    client.force_login(admin_user)
    for label, url in {**world[0], **world[1]}.items():
        assert client.get(url).status_code == 200, label


def test_other_hospital_sees_nothing(client, outsider, world):
    client.force_login(outsider)
    for label, url in {**world[0], **world[1]}.items():
        assert client.get(url).status_code == 404, label


def test_other_branch_sees_only_hospital_wide_records(client, branch2_admin, world):
    branch_records, org_records = world
    client.force_login(branch2_admin)
    for label, url in branch_records.items():
        assert client.get(url).status_code == 404, label
    for label, url in org_records.items():
        assert client.get(url).status_code in (200, 404), label
    assert client.get(org_records["patient"]).status_code == 200


def test_other_hospital_cannot_change_records(client, outsider, world):
    client.force_login(outsider)
    branch_records, org_records = world
    targets = [
        branch_records["invoice"] + "pay/",
        branch_records["invoice"] + "cancel/",
        branch_records["appointment"] + "cancel/",
        org_records["patient"] + "edit/",
        org_records["staff"] + "status/",
    ]
    for url in targets:
        assert client.post(url, {"reason": "x", "amount": "1"}).status_code == 404, url
