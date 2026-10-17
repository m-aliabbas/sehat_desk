import json

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.core.navigation import NAV_ITEMS
from conftest import PASSWORD


@pytest.fixture
def user(make_user):
    return make_user("reception", username="reception1")


@pytest.fixture
def staff(admin_user):
    return admin_user


# --- Login required by default ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["/", "/design-system/", "/design-system/table/", "/modules/patients/"],
)
def test_pages_redirect_anonymous_users_to_login(client, admin_user, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response.url.startswith(reverse("accounts:login"))


def test_login_page_renders(client, admin_user):
    response = client.get(reverse("accounts:login"))
    assert response.status_code == 200
    assert b"Sign in" in response.content


def test_login_then_dashboard(client, user):
    response = client.post(
        reverse("accounts:login"), {"username": "reception1", "password": PASSWORD}, follow=True
    )
    assert response.status_code == 200
    assert b"Welcome back" in response.content


def test_logout_requires_post(client, user):
    client.force_login(user)
    assert client.get(reverse("accounts:logout")).status_code == 405
    assert client.post(reverse("accounts:logout")).status_code == 302


# --- Shell and permission-aware navigation ------------------------------------------


def test_dashboard_shows_shell(client, user):
    client.force_login(user)
    response = client.get("/")
    assert response.status_code == 200
    body = response.content.decode()
    assert 'aria-label="Main navigation"' in body
    assert 'id="global-search"' in body
    assert 'aria-current="page"' in body  # Dashboard marked active


def test_nav_hides_admin_items_from_reception(client, user):
    client.force_login(user)
    keys = [item["key"] for item in client.get("/").context["nav_items"]]
    assert {"dashboard", "reception", "patients", "appointments", "billing"} <= set(keys)
    assert not {"settings", "design-system", "staff", "pharmacy", "laboratory"} & set(keys)


def test_nav_shows_everything_to_super_admin(client, staff):
    client.force_login(staff)
    keys = [item["key"] for item in client.get("/").context["nav_items"]]
    assert keys == [item.key for item in NAV_ITEMS]


# --- Server-side authorization (not just hidden links) ---------------------------------


@pytest.mark.parametrize(
    "method,url",
    [
        ("get", "/design-system/"),
        ("get", "/design-system/table/"),
        ("get", "/design-system/modal/"),
        ("post", "/design-system/action/"),
        ("get", "/ipd/"),
    ],
)
def test_staff_screens_forbidden_for_regular_users(client, user, method, url):
    client.force_login(user)
    assert getattr(client, method)(url).status_code == 403


def test_reports_module_is_built(client, make_user):
    # Reports was the last placeholder module (Phase 20); its nav link now opens the hub.
    client.force_login(make_user("cashier"))
    assert client.get("/modules/reports/").status_code == 404
    response = client.get("/reports/")
    assert response.status_code == 200
    assert b"Daily collection" in response.content


def test_placeholder_404_for_unknown_or_built_module(client, user):
    client.force_login(user)
    assert client.get("/modules/not-a-module/").status_code == 404
    assert client.get("/modules/dashboard/").status_code == 404
    assert client.get("/modules/billing/").status_code == 404


# --- Design system gallery ---------------------------------------------------------


def test_design_system_page(client, staff):
    client.force_login(staff)
    response = client.get("/design-system/")
    assert response.status_code == 200
    assert b"Color palette" in response.content


@pytest.mark.parametrize(
    "state,expected",
    [("loaded", b"<table"), ("empty", b"No appointments found"), ("error", b"Retry")],
)
def test_demo_table_states(client, staff, state, expected):
    client.force_login(staff)
    response = client.get("/design-system/table/", {"state": state})
    assert response.status_code == 200
    assert expected in response.content


def test_demo_table_paginates_and_keeps_query(client, staff):
    client.force_login(staff)
    response = client.get("/design-system/table/", {"state": "loaded", "page": 2})
    body = response.content.decode()
    assert "Showing 9–16 of 37" in body
    assert "/design-system/table/?state=loaded&amp;page=3" in body


def test_demo_modal_and_drawer(client, staff):
    client.force_login(staff)
    assert b'role="dialog"' in client.get("/design-system/modal/").content
    assert b"drawer-panel" in client.get("/design-system/modal/", {"kind": "drawer"}).content


def test_demo_action_requires_post_and_returns_toast(client, staff):
    client.force_login(staff)
    assert client.get("/design-system/action/").status_code == 405
    response = client.post("/design-system/action/")
    assert response.status_code == 204
    assert json.loads(response["HX-Trigger"])["toast"]["level"] == "success"


# --- Template helpers --------------------------------------------------------------


def test_status_chip_maps_status_and_escapes_label():
    html = Template('{% load ui %}{% status_chip "cancelled" label %}').render(
        Context({"label": "<b>x</b>"})
    )
    assert 'class="chip chip-danger"' in html
    assert "&lt;b&gt;" in html


def test_icon_unknown_name_renders_nothing():
    assert Template('{% load ui %}{% icon "nope" %}').render(Context()) == ""
