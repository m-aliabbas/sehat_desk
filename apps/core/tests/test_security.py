"""Security hardening (Phase 26): headers, cookies, rate limits, sessions."""

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from conftest import PASSWORD


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    cache.clear()
    yield
    cache.clear()


def test_security_headers_on_every_page(client, admin_user):
    response = client.get(reverse("accounts:login"))
    csp = response["Content-Security-Policy"]
    for directive in ("default-src 'self'", "connect-src 'self'", "frame-ancestors 'none'",
                      "object-src 'none'", "form-action 'self'"):  # fmt: skip
        assert directive in csp
    assert "camera=()" in response["Permissions-Policy"]
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "same-origin"
    assert response["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "Access-Control-Allow-Origin" not in response  # no CORS


def test_signed_in_pages_are_not_cached(admin_client):
    response = admin_client.get(reverse("core:dashboard"))
    assert "no-store" in response["Cache-Control"] and "private" in response["Cache-Control"]


def test_csrf_cookie_is_http_only_and_posts_need_the_token(admin_user):
    c = Client(enforce_csrf_checks=True)
    response = c.get(reverse("accounts:login"))
    assert response.cookies["csrftoken"]["httponly"]
    bad = c.post(reverse("accounts:login"), {"username": "admin", "password": PASSWORD})
    assert bad.status_code == 403


def test_cross_origin_post_is_refused_even_with_token(admin_user, settings):
    c = Client(enforce_csrf_checks=True)
    c.get(reverse("accounts:login"))
    token = c.cookies["csrftoken"].value
    response = c.post(
        reverse("accounts:login"),
        {"username": "admin", "password": PASSWORD, "csrfmiddlewaretoken": token},
        HTTP_ORIGIN="http://evil.example",
    )
    assert response.status_code == 403


def test_password_reset_requests_are_rate_limited(client, admin_user):
    url = reverse("accounts:password-reset")
    codes = [client.post(url, {"email": "a@example.com"}).status_code for _ in range(7)]
    assert codes[:5] == [302] * 5 or 429 not in codes[:5]
    assert codes[-1] == 429


def test_password_change_signs_out_other_sessions(admin_user):
    office, home = Client(), Client()
    for c in (office, home):
        c.force_login(admin_user)
    new = "Another-pass-2026!"
    response = office.post(
        reverse("accounts:password-change"),
        {"old_password": PASSWORD, "new_password1": new, "new_password2": new},
    )
    assert response.status_code == 302
    assert office.get(reverse("core:dashboard")).status_code == 200  # this session kept
    stale = home.get(reverse("core:dashboard"))
    assert stale.status_code == 302 and reverse("accounts:login") in stale.url


def test_deactivated_user_is_signed_out(admin_client, make_user):
    user = make_user("reception", username="desk1")
    c = Client()
    c.force_login(user)
    assert c.get(reverse("core:dashboard")).status_code == 200
    user.is_active = False
    user.save()
    response = c.get(reverse("core:dashboard"))
    assert response.status_code == 302 and reverse("accounts:login") in response.url


def test_no_patient_data_in_logs(caplog, admin_client, patient):
    import logging

    with caplog.at_level(logging.DEBUG):
        admin_client.get(reverse("patients:detail", args=[patient.pk]))
        admin_client.get(reverse("search:suggest"), {"q": patient.full_name})
    text = caplog.text
    assert patient.full_name not in text
    assert patient.phone not in text
