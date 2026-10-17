"""Shared test helpers for the inpatient, nursing, documents and notification tests."""

from decimal import Decimal

from apps.accounts.services import effective_permissions
from apps.ipd import services
from apps.ipd.models import Room, Ward


def make_request(rf, user, org, branch):
    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    req.META["REMOTE_ADDR"] = "127.0.0.1"
    return req


def make_ward(org, branch, *, code="GW", beds=3, rate="1000", name=None):
    ward = Ward.objects.create(organization=org, branch=branch, name=name or f"Ward {code}",
                               code=code)  # fmt: skip
    room = Room.objects.create(
        organization=org, branch=branch, ward=ward, name="101", daily_rate=Decimal(rate)
    )
    return ward, room, services.add_beds(room, prefix=code, count=beds)


def admit(req, patient, doctor, bed, **extra):
    return services.admit(
        request=req,
        patient=patient,
        doctor=doctor,
        department=doctor.department,
        bed=bed,
        reason=extra.pop("reason", "Fever for 5 days"),
        **extra,
    )
