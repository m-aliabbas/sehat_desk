"""Shared test fixtures: organizations, branches and staff with real roles."""

import pytest

from apps.accounts.models import RoleAssignment, User
from apps.accounts.services import seed_default_roles, sync_permissions
from apps.organizations.models import Branch, Organization

PASSWORD = "Test-pass-2026!"


@pytest.fixture
def org(db):
    sync_permissions()
    organization = Organization.objects.create(name="Sehat Medical Centre")
    seed_default_roles(organization)
    return organization


@pytest.fixture
def branch(org):
    return Branch.objects.create(organization=org, name="Main branch", code="MAIN", is_main=True)


@pytest.fixture
def branch2(org):
    return Branch.objects.create(organization=org, name="City branch", code="CITY")


@pytest.fixture
def make_user(org, branch):
    """make_user("doctor") -> active user with that system role in all branches (or one branch)."""
    counter = {"n": 0}

    def _make(role_key=None, *, branch=None, organization=None, username=None, **fields):
        counter["n"] += 1
        organization = organization or org
        user = User(
            username=username or f"user{counter['n']}",
            organization=organization,
            first_name=fields.pop("first_name", f"Test{counter['n']}"),
            **fields,
        )
        user.set_password(PASSWORD)
        user.save()
        if role_key:
            role = organization.roles.get(system_key=role_key)
            RoleAssignment.objects.create(user=user, role=role, branch=branch)
        return user

    return _make


@pytest.fixture
def admin_user(make_user):
    return make_user("super_admin", username="admin")


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture(autouse=True)
def _no_notification_sweeps(settings):
    # Time-based notification checks are tested directly (apps/notifications/tests).
    settings.SEHATDESK_NOTIFICATION_SWEEP_SECONDS = 0


@pytest.fixture(autouse=True)
def _private_document_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "documents": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "documents"), "base_url": None},
        },
    }


@pytest.fixture(autouse=True)
def _fast_password_hashing(settings):
    # Real hashers are deliberately slow; tests only need them to round-trip.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def department(org):
    from apps.masterdata.models import Department

    return Department.objects.create(organization=org, name="General medicine", code="GM")


@pytest.fixture
def doctor(org, branch, department, make_user):
    """Dr. Zain Ali: works every day 09:00–14:00 (break 11:00–11:30) in the main branch."""
    from datetime import time
    from decimal import Decimal

    from apps.masterdata.models import DoctorProfile, DoctorSchedule

    user = make_user("doctor", username="drzain", first_name="Zain", last_name="Ali")
    profile = DoctorProfile.objects.create(
        organization=org,
        user=user,
        department=department,
        consultation_fee=Decimal("1500"),
        follow_up_fee=Decimal("800"),
        slot_minutes=30,
    )
    for day in range(7):
        DoctorSchedule.objects.create(
            doctor=profile,
            branch=branch,
            weekday=day,
            start_time=time(9),
            end_time=time(14),
            break_start=time(11),
            break_end=time(11, 30),
        )
    return profile


@pytest.fixture
def patient(org, branch, admin_user):
    from apps.patients.services import register_patient

    return register_patient(
        organization=org,
        branch=branch,
        user=admin_user,
        data={"full_name": "Ayesha Khan", "gender": "female", "phone": "03001234567"},
    )


@pytest.fixture
def make_request(rf, org, branch):
    """make_request(user) -> a request-like object for calling services directly."""
    from apps.accounts.services import effective_permissions

    def _make(user, *, branch=branch):
        req = rf.post("/")
        req.user, req.organization, req.branch = user, org, branch
        req.sd_perms = effective_permissions(user, branch)
        req.META["REMOTE_ADDR"] = "127.0.0.1"
        return req

    return _make


@pytest.fixture
def cash(org):
    from apps.billing.models import PaymentMethod
    from apps.billing.services import ensure_payment_methods

    ensure_payment_methods(org)
    return PaymentMethod.objects.get(organization=org, code="cash")
