"""Device licence (apps/core/licensing.py): first-start term, offline month, blocking."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse

from apps.core import licensing


class FakeDocument:
    def __init__(self, data=None):
        self.data = data
        self.written = None

    def get(self, retry=None, timeout=None):
        return SimpleNamespace(exists=self.data is not None, to_dict=lambda: self.data)

    def set(self, data, merge=False, timeout=None):
        self.written = data


@pytest.fixture
def enforced(settings, tmp_path, monkeypatch):
    settings.SEHATDESK_LICENSE_ENFORCED = True
    settings.DATA_DIR = tmp_path
    monkeypatch.setattr(licensing, "_verdict", None)
    monkeypatch.setattr(licensing, "device_id", lambda: "device-1")
    return settings


def use_document(monkeypatch, document):
    collection = SimpleNamespace(document=lambda name: document)
    client = SimpleNamespace(collection=lambda name: collection)
    monkeypatch.setattr(licensing, "_firestore_client", lambda: client)


def fail_firebase(monkeypatch):
    def fail():
        raise OSError("offline")

    monkeypatch.setattr(licensing, "_firestore_client", fail)


def in_days(days):
    return datetime.now(UTC) + timedelta(days=days)


def test_new_device_gets_one_month_approved(enforced, monkeypatch):
    document = FakeDocument()
    use_document(monkeypatch, document)
    result = licensing.refresh()
    assert result["active"] and result["days_remaining"] == 30
    assert document.written["approved"] is True
    assert not any("patient" in key for key in document.written)


@pytest.mark.parametrize(
    "data",
    [
        {"approved": True, "disabled": True, "expire_date": in_days(9)},
        {"approved": False, "expire_date": in_days(9)},
        {"approved": True},
        {"approved": True, "expire_date": in_days(-1)},
    ],
)
def test_device_is_blocked_by_its_document(enforced, monkeypatch, data):
    use_document(monkeypatch, FakeDocument(data))
    assert licensing.refresh()["active"] is False


def test_extension_in_firebase_applies(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(100)}))
    assert licensing.refresh()["days_remaining"] == 100


def test_offline_uses_the_cache_until_the_licence_ends(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(10)}))
    licensing.refresh()
    fail_firebase(monkeypatch)
    monkeypatch.setattr(licensing, "_verdict", None)
    assert licensing.status()["active"] is True  # the answer right after a restart
    assert licensing.refresh()["days_remaining"] == 10

    licensing._save_cache("device-1", in_days(-1))
    assert licensing.refresh()["active"] is False


def test_offline_month_runs_out(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(100)}))
    licensing.refresh()
    fail_firebase(monkeypatch)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=31)

    monkeypatch.setattr(licensing, "datetime", Later)
    assert licensing.refresh()["active"] is False


def test_first_start_offline_blocks_and_asks_for_internet(enforced, monkeypatch):
    fail_firebase(monkeypatch)
    result = licensing.refresh()
    assert result["active"] is False
    assert result["message"] == licensing.NO_INTERNET


def test_revoked_device_loses_its_cache(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(10)}))
    licensing.refresh()
    use_document(monkeypatch, FakeDocument({"approved": True, "disabled": True}))
    licensing.refresh()
    fail_firebase(monkeypatch)
    assert licensing.refresh()["active"] is False


def test_cache_from_another_computer_is_ignored(enforced):
    licensing._save_cache("device-2", in_days(10))
    assert licensing._load_cache("device-1") is None


def test_shipped_credential_decrypts():
    assert licensing._load_credentials()["type"] == "service_account"


@pytest.mark.django_db
def test_every_page_leads_to_the_licence_page(enforced, monkeypatch, client):
    fail_firebase(monkeypatch)
    licensing.refresh()
    page = reverse("core:license")
    assert client.get("/").url == page
    assert client.get("/", headers={"HX-Request": "true"}).headers["HX-Redirect"] == page
    response = client.get(page)
    assert response.status_code == 200
    assert "device-1" in response.content.decode()
    assert client.get(reverse("core:health-live")).status_code == 200


@pytest.mark.django_db
def test_check_again_lets_the_hospital_back_in(enforced, monkeypatch, client):
    fail_firebase(monkeypatch)
    licensing.refresh()
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(30)}))
    assert client.post(reverse("core:license")).url == "/"
    assert client.get("/").url != reverse("core:license")


@pytest.mark.django_db
def test_banner_warns_in_the_last_days(enforced, monkeypatch, admin_client):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(3)}))
    licensing.refresh()
    assert "until the licence ends" in admin_client.get(reverse("core:updates")).content.decode()
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(20)}))
    licensing.refresh()
    assert (
        "until the licence ends" not in admin_client.get(reverse("core:updates")).content.decode()
    )


def test_not_enforced_outside_installed_copies(settings):
    settings.SEHATDESK_LICENSE_ENFORCED = False
    assert licensing.status() == {"enforced": False, "active": True}
