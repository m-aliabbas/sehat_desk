"""Permissions, roles, staff management and organization isolation."""

import pytest

from apps.accounts.models import Permission, Role, RoleAssignment, User
from apps.accounts.permissions import ALL_CODES
from apps.accounts.services import effective_permissions, seed_default_roles, sync_permissions
from apps.audit.models import AuditLog
from apps.organizations.models import Branch, Organization


@pytest.fixture
def other_org(db):
    o = Organization.objects.create(name="Other Hospital")
    seed_default_roles(o)
    return o


@pytest.fixture
def other_staff(other_org):
    Branch.objects.create(organization=other_org, name="Main", code="MAIN", is_main=True)
    return User.objects.create_user("outsider", organization=other_org)


# --- Catalogue and seeding --------------------------------------------------------------


def test_sync_is_idempotent_and_super_admin_gets_everything(org):
    sync_permissions()
    sync_permissions()
    assert Permission.objects.count() == len(ALL_CODES)
    super_admin = org.roles.get(system_key="super_admin")
    assert set(super_admin.permissions.values_list("code", flat=True)) == ALL_CODES


def test_hospital_admin_has_no_clinical_access(org):
    codes = set(
        org.roles.get(system_key="hospital_admin").permissions.values_list("code", flat=True)
    )
    assert "admin.users.manage" in codes
    assert (
        not {"consultation.view", "consultation.sign", "prescription.create", "lab.result.verify"}
        & codes
    )


def test_inactive_role_grants_nothing(make_user, org, branch):
    user = make_user("doctor")
    assert "consultation.sign" in effective_permissions(user, branch)
    org.roles.filter(system_key="doctor").update(is_active=False)
    assert effective_permissions(user, branch) == frozenset()


# --- Backend guards ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,url,expected",
    [
        ("reception", "/staff/", 403),
        ("reception", "/settings/hospital/", 403),
        ("reception", "/settings/roles/", 403),
        ("reception", "/settings/branches/", 403),
        ("doctor", "/settings/roles/matrix/", 403),
        ("hospital_admin", "/staff/", 200),
        ("hospital_admin", "/settings/roles/matrix/", 200),
        ("hospital_admin", "/settings/hospital/", 200),
    ],
)
def test_screens_check_permissions_on_server(client, make_user, role, url, expected):
    client.force_login(make_user(role))
    assert client.get(url).status_code == expected


def test_view_only_staff_permission_cannot_post(client, make_user, org):
    viewer = Role.objects.create(organization=org, name="Viewer")
    viewer.permissions.set(Permission.objects.filter(code="admin.users.view"))
    user = make_user(None)
    RoleAssignment.objects.create(user=user, role=viewer)
    target = make_user("reception")
    client.force_login(user)
    assert client.get("/staff/").status_code == 200
    assert client.post(f"/staff/{target.pk}/status/").status_code == 403
    assert client.get("/staff/new/").status_code == 403


# --- Organization isolation (tenant) -------------------------------------------------------


def test_staff_from_other_org_is_invisible(admin_client, other_staff):
    assert admin_client.get(f"/staff/{other_staff.pk}/").status_code == 404
    assert admin_client.post(f"/staff/{other_staff.pk}/status/").status_code == 404
    assert not admin_client.get("/staff/?q=outsider").context["page_obj"].object_list


def test_roles_and_branches_from_other_org_are_invisible(admin_client, other_org):
    role = other_org.roles.get(system_key="doctor")
    other_branch = Branch.objects.create(organization=other_org, name="Elsewhere", code="ELS")
    assert admin_client.get(f"/settings/roles/{role.pk}/").status_code == 404
    assert admin_client.get(f"/settings/branches/{other_branch.pk}/").status_code == 404
    assert admin_client.post(f"/settings/branches/{other_branch.pk}/status/").status_code == 404


def test_cannot_assign_other_org_role_or_branch(admin_client, make_user, other_org):
    target = make_user("reception")
    foreign_role = other_org.roles.get(system_key="doctor")
    admin_client.post(f"/staff/{target.pk}/roles/", {"role": foreign_role.pk})
    assert not RoleAssignment.objects.filter(user=target, role=foreign_role).exists()


# --- Staff management -------------------------------------------------------------------------


def test_create_staff_with_role_and_forced_password_change(admin_client, org, branch):
    role = org.roles.get(system_key="doctor")
    response = admin_client.post(
        "/staff/new/",
        {
            "first_name": "Zain",
            "last_name": "Ali",
            "username": "zain",
            "designation": "Consultant",
            "password1": "Temp-pass-2026",
            "password2": "Temp-pass-2026",
            "role": role.pk,
            "branch": branch.pk,
        },
    )
    assert response.status_code == 302, response.context["form"].errors
    zain = User.objects.get(username="zain")
    assert zain.organization == org and zain.must_change_password
    assert zain.check_password("Temp-pass-2026")
    assert RoleAssignment.objects.get(user=zain).branch == branch
    assert AuditLog.objects.filter(action="staff.create", record_id=str(zain.pk)).exists()


def test_edit_staff_records_changed_fields(admin_client, make_user):
    target = make_user("reception", designation="Desk")
    admin_client.post(
        f"/staff/{target.pk}/edit/",
        {
            "first_name": target.first_name,
            "username": target.username,
            "designation": "Senior desk",
        },
    )
    log = AuditLog.objects.get(action="staff.update")
    assert log.changes == {"designation": ["Desk", "Senior desk"]}


def test_deactivate_and_reactivate(admin_client, make_user):
    target = make_user("reception")
    admin_client.post(f"/staff/{target.pk}/status/")
    target.refresh_from_db()
    assert not target.is_active
    admin_client.post(f"/staff/{target.pk}/status/")
    target.refresh_from_db()
    assert target.is_active


def test_cannot_deactivate_self(admin_client, admin_user):
    admin_client.post(f"/staff/{admin_user.pk}/status/")
    admin_user.refresh_from_db()
    assert admin_user.is_active


def test_cannot_remove_last_super_admin(admin_client, admin_user):
    a = RoleAssignment.objects.get(user=admin_user)
    admin_client.post(f"/staff/{admin_user.pk}/roles/{a.pk}/remove/")
    assert RoleAssignment.objects.filter(pk=a.pk).exists()


def test_admin_password_reset_forces_change(admin_client, make_user):
    target = make_user("reception")
    admin_client.post(
        f"/staff/{target.pk}/reset-password/",
        {"password1": "Reset-pass-2026", "password2": "Reset-pass-2026"},
    )
    target.refresh_from_db()
    assert target.check_password("Reset-pass-2026") and target.must_change_password


# --- Privilege escalation ----------------------------------------------------------------------


def test_hospital_admin_cannot_grant_super_admin(client, make_user, org):
    actor = make_user("hospital_admin")
    target = make_user("reception")
    client.force_login(actor)
    super_admin = org.roles.get(system_key="super_admin")
    client.post(f"/staff/{target.pk}/roles/", {"role": super_admin.pk})
    assert not RoleAssignment.objects.filter(user=target, role=super_admin).exists()
    # Doctor role contains clinical permissions the hospital admin does not hold.
    doctor = org.roles.get(system_key="doctor")
    client.post(f"/staff/{target.pk}/roles/", {"role": doctor.pk})
    assert not RoleAssignment.objects.filter(user=target, role=doctor).exists()


def test_hospital_admin_cannot_add_clinical_permissions_to_a_role(client, make_user, org):
    actor = make_user("hospital_admin")
    client.force_login(actor)
    role = org.roles.get(system_key="reception")
    sign = Permission.objects.get(code="consultation.sign")
    keep = list(role.permissions.values_list("pk", flat=True))
    response = client.post(
        f"/settings/roles/{role.pk}/",
        {"name": role.name, "description": "", "permissions": [*keep, sign.pk]},
    )
    assert response.status_code == 200  # form error, not saved
    assert not role.permissions.filter(code="consultation.sign").exists()


def test_hospital_admin_cannot_reset_super_admin_password(client, make_user, admin_user):
    client.force_login(make_user("hospital_admin"))
    client.post(
        f"/staff/{admin_user.pk}/reset-password/",
        {"password1": "Hijack-pass-2026", "password2": "Hijack-pass-2026"},
    )
    admin_user.refresh_from_db()
    assert not admin_user.check_password("Hijack-pass-2026")


def test_super_admin_role_cannot_be_edited(admin_client, org):
    role = org.roles.get(system_key="super_admin")
    assert admin_client.get(f"/settings/roles/{role.pk}/").status_code == 302
    admin_client.post(f"/settings/roles/{role.pk}/status/")
    role.refresh_from_db()
    assert role.is_active


# --- Roles & matrix ----------------------------------------------------------------------------


def test_create_custom_role(admin_client, org):
    perms = Permission.objects.filter(code__in=["patient.view", "billing.view"])
    admin_client.post(
        "/settings/roles/new/",
        {"name": "Front office", "description": "", "permissions": [p.pk for p in perms]},
    )
    role = org.roles.get(name="Front office")
    assert not role.is_system
    assert set(role.permissions.values_list("code", flat=True)) == {"patient.view", "billing.view"}
    assert AuditLog.objects.filter(action="role.create").exists()


def test_permission_matrix_saves_changes(admin_client, org):
    nurse = org.roles.get(system_key="nurse")
    codes = set(nurse.permissions.values_list("code", flat=True)) | {"appointment.view"}
    data = {
        f"role_{r.pk}": list(r.permissions.values_list("code", flat=True)) for r in org.roles.all()
    }
    data[f"role_{nurse.pk}"] = sorted(codes)
    admin_client.post("/settings/roles/matrix/", data)
    assert nurse.permissions.filter(code="appointment.view").exists()
    log = AuditLog.objects.get(action="role.update", record_id=str(nurse.pk))
    assert log.changes["added"] == ["appointment.view"]
