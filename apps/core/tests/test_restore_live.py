"""Background backup and restore into a running database (apps/core/backup_jobs.py)."""

import sqlite3
import time
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core import backup, backup_jobs


def _db(path: Path, marker: str) -> Path:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
    db.execute("CREATE TABLE accounts_user (name TEXT)")
    db.execute("INSERT INTO accounts_user VALUES (?)", (marker,))
    db.commit()
    db.close()
    return path


def test_check_rejects_another_apps_database(tmp_path):
    other = tmp_path / "pos.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE products (id INT)")
    db.commit()
    db.close()
    with pytest.raises(backup.BackupError, match="not a SehatDesk"):
        backup_jobs.check_sqlite(other)


def test_check_rejects_a_non_database(tmp_path):
    junk = tmp_path / "x.sqlite3"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(backup.BackupError):
        backup_jobs.check_sqlite(junk)


def test_copy_replaces_live_wal_database_while_a_connection_stays_open(
    settings, tmp_path, monkeypatch
):
    live = _db(tmp_path / "live.sqlite3", "current")
    source = _db(tmp_path / "source.sqlite3", "restored")
    monkeypatch.setitem(settings.DATABASES["default"], "NAME", str(live))
    other_staff = sqlite3.connect(live)
    assert other_staff.execute("SELECT name FROM accounts_user").fetchall() == [("current",)]
    try:
        backup_jobs._copy_into_live_database(source)
        assert other_staff.execute("SELECT name FROM accounts_user").fetchall() == [("restored",)]
    finally:
        other_staff.close()


def test_upload_refuses_other_file_types(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    with pytest.raises(backup.BackupError):
        backup_jobs.save_upload(SimpleUploadedFile("virus.exe", b"x"))


def test_local_restore_only_accepts_listed_backups(settings, tmp_path):
    settings.SEHATDESK_BACKUP_DIR = str(tmp_path)
    with pytest.raises(backup.BackupError):
        backup_jobs.local_backup("../secret.key")


def test_failed_job_is_reported_and_lock_released(settings, tmp_path, monkeypatch):
    settings.DATA_DIR = tmp_path
    monkeypatch.setattr(backup_jobs.connection, "close", lambda: None)

    def fail():
        raise backup.BackupError("disk full")

    backup_jobs._start("backup", "Writing", fail)
    for _ in range(200):
        if not backup_jobs._lock.locked():
            break
        time.sleep(0.02)
    assert backup_jobs.status()["state"] == "failed"
    assert backup_jobs.status()["message"] == "disk full"


def test_stale_running_status_shows_as_failed(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    backup_jobs._set_status("restore", "running", "Restoring")
    assert backup_jobs.status()["state"] == "failed"


def test_maintenance_page_only_during_restore(rf):
    middleware = backup_jobs.RestoreMaintenanceMiddleware(lambda r: "page")
    assert middleware(rf.get("/patients/")) == "page"
    backup_jobs.restoring.set()
    try:
        assert middleware(rf.get("/patients/")).status_code == 503
        assert middleware(rf.get("/health/ready/")) == "page"
    finally:
        backup_jobs.restoring.clear()


@pytest.mark.django_db
def test_restore_needs_typed_confirmation(admin_client, settings, tmp_path, monkeypatch):
    settings.DATA_DIR = tmp_path
    started = []
    monkeypatch.setattr(backup_jobs, "start_restore", lambda *a, **k: started.append(a))
    upload = SimpleUploadedFile("x.sqlite3", b"x")
    admin_client.post(reverse("core:restore-upload"), {"file": upload, "confirm": "no"})
    assert started == []
    upload = SimpleUploadedFile("x.sqlite3", b"x")
    admin_client.post(reverse("core:restore-upload"), {"file": upload, "confirm": "RESTORE"})
    assert len(started) == 1


@pytest.mark.django_db
def test_backup_cards_show_for_admin(admin_client, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    assert b"Restore from a file" in admin_client.get(reverse("core:backups")).content
