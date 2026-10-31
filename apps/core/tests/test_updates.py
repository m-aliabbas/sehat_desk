import hashlib
import io
import json

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.core import pdf, updates

NAME = "SehatDesk-Setup-9.9.9.exe"
BASE = updates.ALLOWED_DOWNLOAD_PREFIX + "v9.9.9/"
PAYLOAD = b"installer bytes"


def _response(body: bytes):
    response = io.BytesIO(body)
    response.headers = {"Content-Length": str(len(body))}
    return response


def _github(checksum=None, url=BASE + NAME):
    checksum = checksum or hashlib.sha256(PAYLOAD).hexdigest()
    release = {
        "published_at": "2026-11-01T00:00:00Z",
        "body": "Notes",
        "assets": [
            {"name": NAME, "browser_download_url": url},
            {"name": "SHA256SUMS.txt", "browser_download_url": BASE + "SHA256SUMS.txt"},
        ],
    }

    def fake_open(requested, timeout=30):
        if requested == updates.RELEASE_API:
            return _response(json.dumps(release).encode())
        if requested.endswith("SHA256SUMS.txt"):
            return _response(f"{checksum}  {NAME}\n".encode())
        return _response(PAYLOAD)

    return fake_open


@pytest.fixture
def data_dir(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    return tmp_path


def test_version_comparison():
    assert updates.is_newer("1.0.10", "1.0.9")
    assert not updates.is_newer("1.0.2", "1.0.2")
    assert not updates.is_newer("1.0.2", "dev")


def test_latest_release_refuses_a_download_from_elsewhere(monkeypatch):
    monkeypatch.setattr(updates, "_open", _github(url="https://example.com/" + NAME))
    with pytest.raises(updates.UpdateError):
        updates.latest_release()


def test_download_keeps_a_file_whose_checksum_matches(monkeypatch, data_dir):
    monkeypatch.setattr(updates, "_open", _github())
    monkeypatch.setattr(updates, "current_version", lambda: "1.0.0")
    release = updates.latest_release()
    (updates.updates_dir() / "SehatDesk-Setup-1.0.0.exe").write_bytes(b"old")
    message = updates._download(release)
    updates._set_status("done", message, version=release["version"], name=release["name"])
    assert updates.downloaded_installer().read_bytes() == PAYLOAD
    assert [p.name for p in updates.updates_dir().iterdir()] == [NAME]


def test_download_discards_a_damaged_file(monkeypatch, data_dir):
    monkeypatch.setattr(updates, "_open", _github(checksum="0" * 64))
    with pytest.raises(updates.UpdateError):
        updates._download(updates.latest_release())
    assert list(updates.updates_dir().iterdir()) == []


def test_page_shows_a_newer_version_and_audits_the_check(admin_client, monkeypatch, data_dir):
    monkeypatch.setattr(updates, "_open", _github())
    response = admin_client.post(reverse("core:update-check"), follow=True)
    assert b"Version 9.9.9 is available" in response.content
    assert AuditLog.objects.filter(action="update.check").exists()


def test_page_reports_an_unreachable_server(admin_client, monkeypatch, data_dir):
    def offline(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(updates, "_open", offline)
    response = admin_client.post(reverse("core:update-check"), follow=True)
    assert b"Could not reach the update server" in response.content


def test_installer_file_is_404_until_downloaded(admin_client, data_dir):
    assert admin_client.get(reverse("core:update-file")).status_code == 404


def test_install_is_refused_outside_an_installed_windows_copy(admin_client, data_dir, monkeypatch):
    monkeypatch.setattr(updates, "current_version", lambda: "1.0.0")
    (updates.updates_dir() / NAME).write_bytes(PAYLOAD)
    updates._set_status("done", "ok", version="9.9.9", name=NAME)
    response = admin_client.post(reverse("core:update-install"), follow=True)
    assert b"only on an installed Windows copy" in response.content


def test_windows_pdf_libraries_are_registered(monkeypatch, tmp_path):
    added = []
    monkeypatch.setenv("WEASYPRINT_DLL_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(pdf.os, "add_dll_directory", added.append, raising=False)
    pdf._add_windows_library_dirs()
    assert added == [str(tmp_path)]
