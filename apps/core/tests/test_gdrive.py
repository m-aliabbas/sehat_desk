"""Google Drive backups: schedule, upload, retention and the Backup page. Google is faked."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core import backup, gdrive


class FakeFile(dict):
    def __init__(self, drive, meta):
        super().__init__(meta)
        self.drive, self.content, self.trashed = drive, None, False

    def SetContentFile(self, path):
        self.content = open(path, "rb")  # noqa: SIM115 - closed by the code under test

    def Upload(self):
        self["id"] = f"id-{len(self.drive.files)}"
        self.drive.files.append(self)

    def Trash(self):
        self.trashed = True


class FakeDrive:
    def __init__(self):
        self.files = []

    def CreateFile(self, meta):
        return FakeFile(self, meta)

    def ListFile(self, params):
        folders = "mimeType" in params["q"]
        found = [
            f
            for f in self.files
            if ("mimeType" in f and "folder" in f["mimeType"]) == folders and not f.trashed
        ]
        return SimpleNamespace(GetList=lambda: list(reversed(found)))


@pytest.fixture
def data_dir(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SEHATDESK_GDRIVE_TIMES = ["15:00", "22:00"]
    settings.SEHATDESK_GDRIVE_KEEP = 2
    return tmp_path


@pytest.fixture
def connected(data_dir):
    gdrive._save_session("{}", {"email": "owner@example.com"})
    return data_dir


@pytest.fixture
def drive(monkeypatch, connected):
    fake = FakeDrive()
    monkeypatch.setattr(gdrive, "_authorized_drive", lambda: fake)

    def fake_backup(*, key=None, **kwargs):
        name = f"{backup.NAME_PREFIX}{len(fake.files)}.zip.enc"
        path = connected / name
        path.write_bytes(b"x")
        assert key
        return SimpleNamespace(path=path, name=name)

    monkeypatch.setattr(backup, "create_backup", fake_backup)
    return fake


def at(hour, minute=0, day=10):
    return timezone.make_aware(datetime(2026, 11, day, hour, minute))


def test_shipped_client_file_decrypts_with_shared_key():
    assert gdrive._load_client()["client_id"]


def test_shared_key_is_a_valid_backup_key(settings, tmp_path):
    settings.SEHATDESK_BACKUP_KEY = ""
    key = backup.shared_or_configured_key()
    plain, sealed, out = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    plain.write_bytes(b"patient data")
    backup.encrypt_file(plain, sealed, key)
    assert b"patient data" not in sealed.read_bytes()
    backup.decrypt_file(sealed, out, key)
    assert out.read_bytes() == b"patient data"


def test_slots_are_3pm_and_10pm(data_dir):
    assert gdrive.last_slot(at(16)) == at(15)
    assert gdrive.last_slot(at(23)) == at(22)
    assert gdrive.last_slot(at(9)) == at(22, day=9)
    assert gdrive.next_slot(at(16)) == at(22)
    assert gdrive.next_slot(at(23)) == at(15, day=11)


def test_not_due_until_connected(data_dir):
    assert gdrive.backup_due(at(16)) is False


def test_times_before_sign_in_are_not_due(connected):
    gdrive._update_state(connected_at=at(15, 30).isoformat())
    assert gdrive.backup_due(at(16)) is False
    assert gdrive.backup_due(at(22, 1)) is True


def test_missed_time_runs_when_server_next_starts(connected):
    gdrive._update_state(connected_at=at(9).isoformat(), last_success=at(10).isoformat())
    assert gdrive.backup_due(at(21)) is True  # PC was off at 15:00


def test_done_slot_is_not_repeated(connected):
    gdrive._update_state(last_success=at(15, 1).isoformat())
    assert gdrive.backup_due(at(16)) is False


def test_failure_retries_after_a_wait(connected):
    gdrive._update_state(last_attempt=at(15).isoformat())
    assert gdrive.backup_due(at(15, 5)) is False
    assert gdrive.backup_due(at(15, gdrive.RETRY_MINUTES + 1)) is True


def test_run_backup_uploads_encrypted_file_and_records_it(drive):
    name = gdrive.run_backup()
    uploaded = [f for f in drive.files if f["title"] == name]
    assert uploaded and uploaded[0].content.closed  # Windows must be able to prune the file
    assert gdrive.status()["last_file"] == name
    assert gdrive.status()["last_error"] == ""


def test_old_uploads_go_to_trash(drive):
    for _ in range(3):
        gdrive.run_backup()
    uploads = [f for f in drive.files if f.get("title", "").startswith(backup.NAME_PREFIX)]
    assert [f.trashed for f in uploads] == [True, False, False]


def test_failed_upload_is_reported_and_releases_the_lock(drive, monkeypatch):
    monkeypatch.setattr(gdrive, "_upload", lambda *a: 1 / 0)
    with pytest.raises(gdrive.GoogleDriveError):
        gdrive.run_backup()
    status = gdrive.status()
    assert status["last_error"] and status["busy"] == ""
    assert not gdrive._lock.locked()


def test_disconnect_forgets_login_but_keeps_history(drive):
    gdrive.run_backup()
    gdrive.disconnect()
    status = gdrive.status()
    assert status["connected"] is False and status["last_success"]


def test_login_file_is_not_plain_text(connected):
    assert b"owner@example.com" not in Path(gdrive._session_path()).read_bytes()


@pytest.mark.django_db
def test_backup_page_shows_the_card(admin_client, data_dir):
    response = admin_client.get(reverse("core:backups"))
    assert b"Google Drive backup" in response.content
    assert b"Connect Google account" in response.content


@pytest.mark.django_db
def test_connect_needs_the_manage_permission(make_user, client, data_dir):
    client.force_login(make_user("reception"))
    assert client.post(reverse("core:gdrive-connect")).status_code == 403
    assert not gdrive._session_path().exists()


@pytest.mark.django_db
def test_backup_button_starts_upload_and_audits(admin_client, connected, monkeypatch):
    calls = []
    monkeypatch.setattr(gdrive, "start_backup", lambda: calls.append(1))
    response = admin_client.post(reverse("core:gdrive-backup"))
    assert response.status_code == 302 and calls == [1]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name",
    [
        "gdrive-connect",
        "gdrive-disconnect",
        "gdrive-backup",
        "backup-now",
        "restore-upload",
        "restore-local",
        "restore-drive",
    ],
)
def test_only_admin_can_use_backup_actions(make_user, client, data_dir, name):
    client.force_login(make_user("reception"))
    assert client.post(reverse(f"core:{name}")).status_code == 403


@pytest.mark.django_db
def test_backup_page_is_in_tabs_and_all_menu(admin_client, data_dir):
    url = reverse("core:backups").encode()
    assert url in admin_client.get(reverse("core:system")).content
    assert url in admin_client.get(reverse("core:menu")).content
