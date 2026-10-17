import json
import logging

import pytest
from django.db import transaction
from django.test import Client
from django.urls import reverse

from apps.core.db import critical_write, locked
from apps.core.models import ErrorReport
from apps.core.observability import (
    JsonFormatter,
    RedactFilter,
    RequestIdFilter,
    metrics,
    redact,
    request_id_var,
)

# --- Redaction -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "patient phone 03001234567",
        "patient phone 0300-1234567",
        "patient phone +923001234567",
        "cnic 35202-1234567-1",
        "cnic 3520212345671",
        "email ayesha@example.com",
    ],
)
def test_redact_masks_identifiers(text):
    out = redact(text)
    assert not any(ch.isdigit() for ch in out.split(" ", 2)[-1]) or "[" in out
    assert "1234567" not in out and "@example" not in out


def test_redact_keeps_ids_and_amounts():
    assert redact("invoice=42 amount=1500 branch=3") == "invoice=42 amount=1500 branch=3"


def test_redact_filter_covers_format_args():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "sent to %s", ("03001234567",), None)
    RedactFilter().filter(record)
    assert record.getMessage() == "sent to [phone]"


def test_json_formatter_has_request_id_and_drops_exception_message():
    token = request_id_var.set("abc12345")
    try:
        try:
            raise ValueError("Ayesha Khan 03001234567")
        except ValueError:
            import sys

            record = logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info()
            )
        RequestIdFilter().filter(record)
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["request_id"] == "abc12345"
    assert "builtins.ValueError" in entry["exception"]
    assert "Ayesha" not in entry["exception"]


# --- Request IDs and metrics -----------------------------------------------------------


def test_every_response_has_a_request_id(client, db):
    response = client.get(reverse("core:health-live"))
    assert len(response["X-Request-ID"]) == 32


def test_wellformed_incoming_request_id_is_kept(client, db):
    response = client.get(reverse("core:health-live"), HTTP_X_REQUEST_ID="proxy-req-0001")
    assert response["X-Request-ID"] == "proxy-req-0001"


def test_malformed_incoming_request_id_is_replaced(client, db):
    response = client.get(reverse("core:health-live"), HTTP_X_REQUEST_ID="<script>")
    assert response["X-Request-ID"] != "<script>"


def test_metrics_open_on_loopback_only(client, admin_user, make_user):
    client.get(reverse("core:health-live"))
    response = client.get(reverse("core:health-metrics"))  # test client is 127.0.0.1
    assert response.status_code == 200
    assert b"sehatdesk_requests_total" in response.content

    remote = Client(REMOTE_ADDR="192.168.1.20")
    assert remote.get(reverse("core:health-metrics")).status_code == 404
    remote.force_login(make_user("reception"))
    assert remote.get(reverse("core:health-metrics")).status_code == 404
    remote.force_login(admin_user)
    assert remote.get(reverse("core:health-metrics")).status_code == 200


# --- Error reports -----------------------------------------------------------------------


@pytest.fixture
def boom_url(settings):
    """A view that raises, mounted for this test only."""
    from django.urls import path

    import config.urls

    def boom(request):
        raise RuntimeError("Ayesha Khan 03001234567")

    module = type(config.urls)("boom_urls")
    module.urlpatterns = [path("boom/", boom), *config.urls.urlpatterns]
    module.handler500 = config.urls.handler500
    settings.ROOT_URLCONF = module
    return "/boom/?q=Ayesha"


def test_server_exception_is_reported_without_patient_data(admin_user, boom_url):
    c = Client(raise_request_exception=False)
    c.force_login(admin_user)
    response = c.get(boom_url)
    assert response.status_code == 500
    report = ErrorReport.objects.get()
    assert report.source == "server"
    assert report.error_type == "builtins.RuntimeError"
    assert report.path == "/boom/"  # no query string
    assert report.user == admin_user and report.organization == admin_user.organization
    assert "Ayesha" not in report.stack and "0300" not in report.stack
    assert report.request_id == response["X-Request-ID"]
    assert report.request_id.encode() in response.content  # shown on the 500 page


def test_browser_error_is_stored_redacted(client, db):
    payload = {
        "type": "TypeError",
        "message": "x is undefined for 03001234567",
        "source": "http://testserver/static/js/app.js?v=1",
        "line": 10,
        "column": 4,
        "page": "/patients/5/?q=ayesha",
    }
    response = client.post(
        reverse("core:client-error"),
        json.dumps(payload),
        content_type="application/json",
        HTTP_ORIGIN="http://testserver",
    )
    assert response.status_code == 204
    report = ErrorReport.objects.get()
    assert report.source == "browser"
    assert report.message == "x is undefined for [phone]"
    assert report.path == "/patients/5/"
    assert report.stack == "http://testserver/static/js/app.js:10:4"


def test_browser_error_needs_same_origin(client, db):
    response = client.post(
        reverse("core:client-error"),
        "{}",
        content_type="application/json",
        HTTP_ORIGIN="http://evil.example",
    )
    assert response.status_code == 400
    assert not ErrorReport.objects.exists()


def test_browser_errors_are_rate_limited(client, db):
    from django.core.cache import cache

    cache.clear()
    codes = [
        client.post(
            reverse("core:client-error"),
            "{}",
            content_type="application/json",
            HTTP_ORIGIN="http://testserver",
        ).status_code
        for _ in range(25)
    ]
    assert codes.count(204) == 20 and codes[-1] == 429


def test_system_page_needs_permission(client, admin_user, make_user):
    client.force_login(make_user("reception"))
    assert client.get(reverse("core:system")).status_code == 403
    client.force_login(admin_user)
    response = client.get(reverse("core:system"))
    assert response.status_code == 200
    assert b"System health" in response.content


def test_system_page_hides_other_organizations_errors(client, admin_user, org):
    from apps.organizations.models import Organization

    other = Organization.objects.create(name="Other hospital")
    ErrorReport.objects.create(source="server", error_type="OtherOrgError", organization=other)
    ErrorReport.objects.create(source="server", error_type="OwnOrgError", organization=org)
    client.force_login(admin_user)
    content = client.get(reverse("core:system")).content
    assert b"OwnOrgError" in content and b"OtherOrgError" not in content


def test_metrics_count_requests(client, db):
    metrics.reset()
    client.get(reverse("core:health-live"))
    assert metrics.snapshot()["requests"][("GET", "2xx")] == 1


# --- Transaction helper ---------------------------------------------------------------


def test_locked_requires_a_transaction(org, monkeypatch):
    # Tests run inside a transaction; pretend we are in autocommit (a flush-based
    # transactional_db fixture is impossible: audit rows refuse deletion by design).
    with monkeypatch.context() as m:
        m.setattr(transaction.get_connection(), "in_atomic_block", False)
        with pytest.raises(RuntimeError):
            locked(org)


def test_critical_write_rolls_back_everything(org):
    @critical_write
    def rename_then_fail():
        fresh = locked(org)
        fresh.name = "Changed"
        fresh.save()
        raise ValueError("stop")

    with pytest.raises(ValueError):
        rename_then_fail()
    org.refresh_from_db()
    assert org.name == "Sehat Medical Centre"


def test_locked_by_model_and_pk(org):
    with transaction.atomic():
        assert locked(type(org), org.pk) == org
