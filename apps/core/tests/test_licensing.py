"""Device licence (apps/core/licensing.py): manual approval, 30-day local token, blocking."""

import json
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


def test_new_device_waits_for_manual_approval(enforced, monkeypatch):
    document = FakeDocument()
    use_document(monkeypatch, document)
    result = licensing.refresh()
    assert result["active"] is False and result["message"] == licensing.WAITING
    assert document.written["approved"] is False
    assert document.written["expire_date"] > in_days(29)
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


def test_approved_device_gets_its_time_from_firebase(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(100)}))
    assert licensing.refresh()["days_remaining"] == 100


def test_good_token_needs_no_server(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(10)}))
    licensing.refresh()
    fail_firebase(monkeypatch)
    monkeypatch.setattr(licensing, "_verdict", None)
    assert licensing.status()["active"] is True  # the answer right after a restart
    assert licensing.refresh()["days_remaining"] == 10


def _after_token_ends(monkeypatch):
    fernet = licensing._cache_fernet()
    token = json.loads(fernet.decrypt(licensing._cache_path().read_bytes()))
    token["verified_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    licensing._cache_path().write_bytes(fernet.encrypt(json.dumps(token).encode()))


def test_server_changes_apply_when_the_token_ends(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(100)}))
    licensing.refresh()
    use_document(monkeypatch, FakeDocument({"approved": True, "disabled": True}))
    assert licensing.refresh()["active"] is True  # the token still holds
    _after_token_ends(monkeypatch)
    assert licensing.refresh()["active"] is False


def test_ended_token_gets_a_new_one_from_the_server(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(100)}))
    licensing.refresh()
    _after_token_ends(monkeypatch)
    assert licensing.refresh()["active"] is True
    assert licensing._load_cache("device-1") is not None


def test_ended_token_without_internet_blocks(enforced, monkeypatch):
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(100)}))
    licensing.refresh()
    fail_firebase(monkeypatch)
    _after_token_ends(monkeypatch)
    assert licensing.refresh()["message"] == licensing.NO_INTERNET


def test_licence_end_date_in_the_token_is_enforced(enforced, monkeypatch):
    licensing._save_cache("device-1", in_days(-1))
    fail_firebase(monkeypatch)
    assert licensing.refresh()["active"] is False


def test_first_start_offline_blocks_and_asks_for_internet(enforced, monkeypatch):
    fail_firebase(monkeypatch)
    result = licensing.refresh()
    assert result["active"] is False
    assert result["message"] == licensing.NO_INTERNET


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
    licensing._cache_path().unlink()  # the token ran out, so the server answers again
    use_document(monkeypatch, FakeDocument({"approved": True, "expire_date": in_days(20)}))
    licensing.refresh()
    assert (
        "until the licence ends" not in admin_client.get(reverse("core:updates")).content.decode()
    )


def test_not_enforced_outside_installed_copies(settings):
    settings.SEHATDESK_LICENSE_ENFORCED = False
    assert licensing.status() == {"enforced": False, "active": True}
