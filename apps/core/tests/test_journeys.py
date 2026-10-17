"""Role journeys (Phase 27): each role signs in through the real login form, then opens every
screen its menu offers. Every page must render (200), modules outside the role must not be in
its menu, and the role's main screen must offer the action that role exists for.

Detailed click-through flows per role live next to each module:
reception  queue/tests  (desk, walk-in, check-in, token slip)
doctor     encounters/tests, lab/tests::test_doctor_orders_from_consultation
lab        lab/tests::test_desk_flow_through_views
pharmacy   pharmacy/tests/test_pos.py
cashier    billing/tests::test_cashier_flow_through_views
IPD        ipd/tests::test_discharge_view_steps
"""

import re

import pytest
from django.urls import reverse

from apps.core.navigation import NAV_ITEMS
from conftest import PASSWORD

NAV_URLS = {item.key: item.url_name for item in NAV_ITEMS if item.url_name}

JOURNEYS = {
    # role: (main menu entry, entries it must have, entries it must not have, main screen text)
    "reception": ("reception", {"appointments", "patients"},
                  {"laboratory", "pharmacy", "opd", "settings"}, "walk-in"),
    "doctor": ("opd", {"patients"}, {"settings", "staff", "pharmacy", "billing"}, "queue"),
    "lab": ("laboratory", set(), {"opd", "pharmacy", "settings"}, "worklist"),
    "pharmacy": ("pharmacy", {"inventory"}, {"opd", "laboratory", "settings"}, "pos-search"),
    "cashier": ("billing", set(), {"opd", "laboratory", "settings"}, "invoice"),
    "nurse": ("nursing", {"ipd", "vitals"}, {"settings", "billing", "pharmacy"}, "ward"),
    "hospital_admin": ("settings", {"staff", "reports", "ipd"}, {"opd", "nursing"}, "hospital"),
}  # fmt: skip


def _sign_in(client, username):
    response = client.post(
        reverse("accounts:login"), {"username": username, "password": PASSWORD}, follow=True
    )
    assert response.status_code == 200
    assert response.wsgi_request.user.is_authenticated, f"{username} could not sign in"
    return response


def _menu_keys(html: str) -> set[str]:
    urls = set(re.findall(r'href="([^"]+)"', html))
    return {key for key, name in NAV_URLS.items() if reverse(name) in urls}


@pytest.mark.parametrize("role", sorted(JOURNEYS))
def test_role_journey(client, make_user, doctor, role):
    main_key, must, must_not, main_text = JOURNEYS[role]
    must = must | {main_key}
    user = doctor.user if role == "doctor" else make_user(role, username=f"{role}-j")
    landing = _sign_in(client, user.username)
    keys = _menu_keys(landing.content.decode())
    assert must <= keys, f"{role} menu lacks {must - keys}"
    assert not (keys & must_not), f"{role} menu shows {keys & must_not}"
    for key in sorted(keys):
        page = client.get(reverse(NAV_URLS[key]), follow=True)
        assert page.status_code == 200, (role, key)
    main = client.get(reverse(NAV_URLS[main_key]), follow=True).content.decode()
    assert main_text in main.lower(), (role, main_key, main_text)
    assert client.post(reverse("accounts:logout")).status_code == 302
    assert client.get(reverse("core:dashboard")).status_code == 302


def test_admin_journey_reaches_every_settings_tab(client, admin_user):
    _sign_in(client, admin_user.username)
    html = client.get(reverse("organizations:hospital")).content.decode()
    tabs = re.findall(r'<a class="tab" href="([^"]+)"', html)
    assert len(tabs) >= 12
    for url in tabs:
        assert client.get(url).status_code == 200, url
