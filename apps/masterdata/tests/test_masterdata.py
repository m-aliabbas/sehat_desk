from datetime import date, time
from decimal import Decimal

import pytest

from apps.accounts.models import Permission
from apps.audit.models import AuditLog
from apps.masterdata.models import (
    Department,
    DoctorProfile,
    DoctorSchedule,
    Service,
    ServiceBranchPrice,
    ServiceCategory,
)
from apps.organizations.models import Organization


def test_new_permission_reaches_existing_system_roles(org):
    """sync_permissions grants a brand-new code to matching system roles in existing orgs."""
    from apps.accounts.services import sync_permissions

    Permission.objects.filter(code="admin.masterdata.manage").delete()
    sync_permissions()
    admin = org.roles.get(system_key="hospital_admin")
    assert admin.permissions.filter(code="admin.masterdata.manage").exists()
    assert (
        not org.roles.get(system_key="reception")
        .permissions.filter(code="admin.masterdata.manage")
        .exists()
    )


@pytest.mark.parametrize(
    "url", ["/settings/departments/", "/settings/doctors/", "/settings/services/"]
)
def test_masterdata_needs_permission(client, make_user, url):
    client.force_login(make_user("reception"))
    assert client.get(url).status_code == 403


def test_department_crud_and_unique_code(admin_client, org):
    admin_client.post("/settings/departments/new/", {"name": "Cardiology", "code": "card"})
    dept = Department.objects.get(organization=org, code="CARD")
    response = admin_client.post("/settings/departments/new/", {"name": "Heart", "code": "CARD"})
    assert response.status_code == 200
    admin_client.post(f"/settings/departments/{dept.pk}/status/")
    dept.refresh_from_db()
    assert not dept.is_active
    assert AuditLog.objects.filter(action="department.deactivate").exists()


def test_department_from_other_org_is_404(admin_client):
    other = Organization.objects.create(name="Other")
    dept = Department.objects.create(organization=other, name="X", code="X")
    assert admin_client.get(f"/settings/departments/{dept.pk}/").status_code == 404


def test_add_doctor_profile(admin_client, org, department, make_user):
    user = make_user("doctor", username="drsara")
    response = admin_client.post(
        "/settings/doctors/new/",
        {
            "user": user.pk,
            "department": department.pk,
            "consultation_fee": "2000",
            "follow_up_fee": "1000",
            "follow_up_days": "7",
            "slot_minutes": "20",
        },
    )
    profile = DoctorProfile.objects.get(user=user)
    assert response.url == f"/settings/doctors/{profile.pk}/"
    assert profile.organization == org and profile.display_name.startswith("Dr. ")


def test_save_weekly_schedule(admin_client, doctor, branch):
    data = {"days-TOTAL_FORMS": "7", "days-INITIAL_FORMS": "7", "save_schedule": "1"}
    for i in range(7):
        data[f"days-{i}-weekday"] = str(i)
        if i < 5:
            data.update(
                {
                    f"days-{i}-enabled": "on",
                    f"days-{i}-start_time": "10:00",
                    f"days-{i}-end_time": "13:00",
                    f"days-{i}-room": "Room 3",
                }
            )
    response = admin_client.post(f"/settings/doctors/{doctor.pk}/?branch={branch.pk}", data)
    assert response.status_code == 302
    active = DoctorSchedule.objects.filter(doctor=doctor, branch=branch, is_active=True)
    assert active.count() == 5
    assert active.first().start_time == time(10)


def test_schedule_rejects_break_outside_hours(admin_client, doctor, branch):
    data = {"days-TOTAL_FORMS": "7", "days-INITIAL_FORMS": "7", "save_schedule": "1"}
    for i in range(7):
        data[f"days-{i}-weekday"] = str(i)
    data.update(
        {
            "days-0-enabled": "on",
            "days-0-start_time": "09:00",
            "days-0-end_time": "12:00",
            "days-0-break_start": "13:00",
            "days-0-break_end": "13:30",
        }
    )
    response = admin_client.post(f"/settings/doctors/{doctor.pk}/", data)
    assert response.status_code == 200
    assert b"break must fall inside" in response.content


def test_slot_starts_skip_break(doctor, branch):
    sched = doctor.schedules.get(weekday=0)
    starts = list(sched.slot_starts(30, date(2026, 10, 19)))
    assert time(10, 30) in starts and time(11) not in starts and time(11, 30) in starts
    assert starts[-1] == time(13, 30)


def test_leave_warns_about_existing_bookings(
    admin_client, doctor, patient, branch, org, admin_user
):
    from apps.appointments.models import Appointment

    Appointment.objects.create(
        organization=org,
        branch=branch,
        number="APT-1",
        patient=patient,
        doctor=doctor,
        department=doctor.department,
        date=date(2030, 1, 7),
        start_time=time(9),
        end_time=time(9, 30),
        fee=Decimal("0"),
        created_by=admin_user,
    )
    response = admin_client.post(
        f"/settings/doctors/{doctor.pk}/leave/",
        {"start_date": "2030-01-06", "end_date": "2030-01-08", "reason": "Conference"},
        follow=True,
    )
    assert b"need rescheduling" in response.content


def test_service_with_branch_price(admin_client, org, branch, branch2):
    cat = ServiceCategory.objects.create(organization=org, name="Procedures")
    admin_client.post(
        "/settings/services/new/",
        {
            "category": cat.pk,
            "code": "dress",
            "name": "Dressing",
            "price": "500",
            f"branch_price_{branch2.pk}": "650",
        },
    )
    svc = Service.objects.get(organization=org, code="DRESS")
    assert svc.price_for(branch) == Decimal("500")
    assert svc.price_for(branch2) == Decimal("650")
    # Clearing the branch price falls back to the default.
    admin_client.post(
        f"/settings/services/{svc.pk}/",
        {"category": cat.pk, "code": "DRESS", "name": "Dressing", "price": "550"},
    )
    assert not ServiceBranchPrice.objects.filter(service=svc).exists()
    svc.refresh_from_db()
    assert svc.price_for(branch2) == Decimal("550")
