"""Sign-in, setup, lockout, sessions and passwords."""

import pytest
from django.urls import reverse

from apps.accounts.models import RoleAssignment, User
from apps.audit.models import AuditLog
from conftest import PASSWORD

LOGIN = "/accounts/login/"


def login(client, username, password=PASSWORD, **extra):
    return client.post(LOGIN, {"username": username, "password": password}, **extra)


# --- First-run setup ------------------------------------------------------------------


def _setup_data(**overrides):
    data = {
        "org_name": "Shifa Clinic",
        "branch_name": "Main branch",
        "branch_code": "main",
        "city": "Lahore",
        "first_name": "Amina",
        "last_name": "Raza",
        "username": "amina",
        "email": "",
        "password1": "Strong-pass-2026",
        "password2": "Strong-pass-2026",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_fresh_install_sends_login_to_setup(client):
    assert client.get(LOGIN).url == reverse("accounts:setup")


@pytest.mark.django_db
def test_setup_creates_org_branch_roles_and_super_admin(client):
    response = client.post("/setup/", _setup_data())
    assert response.status_code == 302
    user = User.objects.get(username="amina")
    assert user.organization.name == "Shifa Clinic"
    assert user.organization.branches.get().code == "MAIN"
    assert user.organization.roles.count() == 8
    assert RoleAssignment.objects.get(user=user).role.system_key == "super_admin"
    assert AuditLog.objects.filter(action="setup.complete").exists()
    # Signed in and able to use admin screens straight away.
    assert client.get("/settings/hospital/").status_code == 200


@pytest.mark.django_db
def test_setup_rejects_weak_password(client):
    response = client.post("/setup/", _setup_data(password1="12345", password2="12345"))
    assert response.status_code == 200
    assert not User.objects.exists()


def test_setup_disabled_once_users_exist(client, admin_user):
    assert client.get("/setup/").status_code == 404
    assert client.post("/setup/", _setup_data(username="intruder")).status_code == 404


# --- Login ----------------------------------------------------------------------------


def test_login_success_is_audited(client, make_user):
    make_user("reception", username="sana")
    response = login(client, "sana")
    assert response.status_code == 302
    assert AuditLog.objects.filter(action="auth.login", actor__username="sana").exists()


def test_login_wrong_password_shows_error_and_audits(client, make_user):
    make_user("reception", username="sana")
    response = login(client, "sana", "wrong-password")
    assert response.status_code == 200
    assert b"not correct" in response.content
    log = AuditLog.objects.get(action="auth.login_failed")
    assert "wrong-password" not in str(log.changes) + log.summary


def test_inactive_account_gets_clear_message(client, make_user):
    make_user("reception", username="sana", is_active=False)
    response = login(client, "sana")
    assert response.status_code == 200
    assert b"inactive" in response.content


def test_multi_branch_user_lands_on_workspace_choice(client, make_user, branch2):
    make_user("doctor", username="dr")  # all branches
    assert login(client, "dr").url == reverse("accounts:workspace")


def test_single_branch_user_goes_to_dashboard(client, make_user, branch, branch2):
    make_user("doctor", username="dr", branch=branch)
    assert login(client, "dr").url == reverse("core:dashboard")


def test_lockout_after_repeated_failures(client, make_user, settings):
    make_user("reception", username="sana")
    for _ in range(settings.AXES_FAILURE_LIMIT):
        login(client, "sana", "nope")
    response = login(client, "sana")  # correct password, still locked
    assert response.status_code == 429
    assert b"Too many sign-in attempts" in response.content


def test_admin_unlock_clears_lockout(client, admin_client, make_user, settings):
    from django.test import Client

    sana = make_user("reception", username="sana")
    other = Client()
    for _ in range(settings.AXES_FAILURE_LIMIT):
        login(other, "sana", "nope")
    assert login(other, "sana").status_code == 429
    admin_client.post(f"/staff/{sana.pk}/unlock/")
    assert login(other, "sana").status_code == 302


def test_logout_requires_post_and_clears_session(client, make_user):
    user = make_user("reception")
    client.force_login(user)
    assert client.get("/accounts/logout/").status_code == 405
    client.post("/accounts/logout/")
    assert client.get("/").status_code == 302


def test_session_expired_banner_after_timeout(client, make_user):
    make_user("reception", username="sana")
    login(client, "sana")
    client.session.flush()  # what an idle timeout does server-side
    client.cookies.pop("sessionid", None)
    response = client.get(LOGIN)
    assert b"Session expired" in response.content


def test_htmx_request_after_timeout_redirects_whole_page(client, admin_user):
    response = client.get("/staff/", HTTP_HX_REQUEST="true")
    assert response.status_code == 204
    assert response["HX-Redirect"].startswith(LOGIN)


def test_deactivated_user_session_ends_immediately(client, make_user):
    user = make_user("reception")
    client.force_login(user)
    assert client.get("/").status_code == 200
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert client.get("/").status_code == 302


def test_deactivated_organization_blocks_its_users(client, make_user, org):
    user = make_user("reception")
    client.force_login(user)
    org.is_active = False
    org.save()
    assert client.get("/").status_code == 302


# --- Workspace / branch -------------------------------------------------------------


def test_user_without_roles_sees_access_not_assigned(client, make_user):
    client.force_login(make_user(None))
    response = client.get("/", follow=True)
    assert response.redirect_chain[-1][0] == reverse("accounts:no-access")
    assert b"Access not assigned" in response.content


def test_switch_branch_only_to_accessible_branch(client, make_user, branch, branch2, org):
    from apps.organizations.models import Branch

    other_org_branch = Branch.objects.create(
        organization=__import__(
            "apps.organizations.models", fromlist=["Organization"]
        ).Organization.objects.create(name="Other"),
        name="Other",
        code="OTH",
    )
    user = make_user("doctor", branch=branch)
    client.force_login(user)
    for target in (branch2, other_org_branch):
        client.post("/workspace/switch/", {"branch": target.pk})
        assert client.get("/").context["current_branch"] == branch


def test_branch_scoped_role_only_applies_in_that_branch(client, make_user, branch, branch2, org):
    user = make_user("reception", branch=branch)
    doctor_role = org.roles.get(system_key="doctor")
    RoleAssignment.objects.create(user=user, role=doctor_role, branch=branch2)
    client.force_login(user)
    perms = client.get("/").context["sd_perms"]
    assert "queue.manage" in perms and "consultation.sign" not in perms
    client.post("/workspace/switch/", {"branch": branch2.pk})
    perms = client.get("/").context["sd_perms"]
    assert "consultation.sign" in perms and "queue.manage" not in perms


def test_switch_branch_ignores_external_next_url(client, make_user, branch2):
    client.force_login(make_user("doctor"))
    response = client.post(
        "/workspace/switch/", {"branch": branch2.pk, "next": "https://evil.example/"}
    )
    assert response.url == reverse("core:dashboard")


# --- Passwords -----------------------------------------------------------------------


def test_forced_password_change_blocks_other_pages(client, make_user):
    user = make_user("reception", must_change_password=True)
    client.force_login(user)
    assert client.get("/").url == reverse("accounts:password-change")
    response = client.post(
        "/accounts/password/change/",
        {
            "old_password": PASSWORD,
            "new_password1": "Brand-new-2026!",
            "new_password2": "Brand-new-2026!",
        },
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert not user.must_change_password
    assert client.get("/").status_code == 200  # session kept after change


def test_forgot_password_without_email_explains_admin_reset(client, admin_user):
    response = client.get("/accounts/password/forgot/")
    assert b"Ask your administrator" in response.content


def test_forgot_password_email_flow(client, make_user, settings, mailoutbox):
    from apps.accounts import views_auth

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    views_auth.password_reset.view_initkwargs["extra_context"] = {"email_configured": True}
    make_user("reception", username="sana", email="sana@example.com")
    response = client.post("/accounts/password/forgot/", {"email": "sana@example.com"})
    assert response.status_code == 302
    assert len(mailoutbox) == 1
    assert "/accounts/password/reset/" in mailoutbox[0].body
