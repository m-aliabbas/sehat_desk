"""Backups and restore (Phase 29).

The restore drill runs the real commands in a subprocess against a temporary data folder:
migrate, add a patient and a document, back up, lose the data, restore, and check that
everything came back.
"""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.core import backup

KEY = backup.decode_key(backup.generate_key())


# --- Encryption --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 10, backup.CHUNK, backup.CHUNK * 2 + 17])
def test_encrypt_round_trip(tmp_path, size):
    plain = tmp_path / "plain.bin"
    plain.write_bytes(os.urandom(size))
    backup.encrypt_file(plain, tmp_path / "x.enc", KEY)
    backup.decrypt_file(tmp_path / "x.enc", tmp_path / "out.bin", KEY)
    assert (tmp_path / "out.bin").read_bytes() == plain.read_bytes()
    assert plain.read_bytes()[:64] not in (tmp_path / "x.enc").read_bytes() or size == 0


def _sealed(tmp_path, size=backup.CHUNK * 2 + 5):
    plain = tmp_path / "plain.bin"
    plain.write_bytes(os.urandom(size))
    backup.encrypt_file(plain, tmp_path / "x.enc", KEY)
    return tmp_path / "x.enc"


def test_wrong_key_is_refused(tmp_path):
    with pytest.raises(backup.BackupError, match="Wrong backup key"):
        backup.decrypt_file(
            _sealed(tmp_path), tmp_path / "o", backup.decode_key(backup.generate_key())
        )


def test_tampered_backup_is_refused(tmp_path):
    enc = _sealed(tmp_path)
    data = bytearray(enc.read_bytes())
    data[200] ^= 1
    enc.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError, match="changed"):
        backup.decrypt_file(enc, tmp_path / "o", KEY)


def test_truncated_backup_is_refused(tmp_path):
    enc = _sealed(tmp_path)
    data = enc.read_bytes()
    first_chunk_end = len(backup.MAGIC) + 16 + backup.CHUNK + 16
    enc.write_bytes(data[:first_chunk_end])  # drop the last chunks cleanly
    with pytest.raises(backup.BackupError, match="incomplete"):
        backup.decrypt_file(enc, tmp_path / "o", KEY)


def test_bad_keys_are_explained():
    with pytest.raises(backup.BackupError, match="32 bytes"):
        backup.decode_key("c2hvcnQ=")


# --- Retention and safety ------------------------------------------------------------------


def test_prune_keeps_newest(tmp_path):
    for day in range(1, 6):
        (tmp_path / f"sehatdesk-2026010{day}-020000.zip").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a backup")
    removed = backup.prune(tmp_path, keep=2)
    assert sorted(p.name for p in removed) == [
        "sehatdesk-20260101-020000.zip",
        "sehatdesk-20260102-020000.zip",
        "sehatdesk-20260103-020000.zip",
    ]
    assert (tmp_path / "notes.txt").exists()
    assert [b.name for b in backup.list_backups(tmp_path)] == [
        "sehatdesk-20260105-020000.zip",
        "sehatdesk-20260104-020000.zip",
    ]


def test_archive_paths_cannot_escape(tmp_path):
    archive = tmp_path / "sehatdesk-evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", '{"files": {"../../etc/passwd": "x"}, "database_file": "d"}')
        zf.writestr("../../etc/passwd", "x")
    with pytest.raises(backup.BackupError, match="Unsafe path"):
        backup.verify_backup(archive)


def test_system_page_lists_backups_and_backup_now_needs_permission(
    client, admin_client, make_user, settings, tmp_path
):
    settings.SEHATDESK_BACKUP_DIR = str(tmp_path)
    (tmp_path / "sehatdesk-20260101-020000.zip.enc").write_bytes(b"x")
    page = admin_client.get(reverse("core:system")).content.decode()
    assert "sehatdesk-20260101-020000.zip.enc" in page and "Back up now" in page
    client.force_login(make_user("reception"))
    assert client.post(reverse("core:backup-now")).status_code == 403


# --- Restore drill ---------------------------------------------------------------------------


def _manage(data_dir: Path, *args, env_extra=None, code=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SEHATDESK_", "DJANGO_"))}
    env.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        SEHATDESK_DATA_DIR=str(data_dir),
        SEHATDESK_ENV_FILE=str(data_dir.parent / "empty.env"),
        SEHATDESK_NOTIFICATION_SWEEP_SECONDS="0",
        **(env_extra or {}),
    )
    (data_dir.parent / "empty.env").write_text("")
    cmd = [sys.executable, "-c", code] if code else [sys.executable, "manage.py", *args]
    result = subprocess.run(
        cmd, cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=300
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


SEED = """
import django; django.setup()
from apps.accounts.services import create_organization_with_admin
from apps.patients.services import register_patient
from django.conf import settings
org, branch, user = create_organization_with_admin(
    org_name="Drill Hospital", branch_name="Main", branch_code="MAIN", city="Lahore",
    admin_data={"username": "drill", "password": "Drill-pass-2026!"})
register_patient(organization=org, branch=branch, user=user,
    data={"full_name": "Restore Drill", "gender": "female", "phone": "03001112223"})
docs = settings.DATA_DIR / "private" / "documents" / "1"
docs.mkdir(parents=True)
(docs / "scan.pdf").write_bytes(b"%PDF-1.4 drill")
"""

COUNT = """
import django; django.setup()
from apps.patients.models import Patient
print(Patient.objects.filter(full_name="Restore Drill").count())
"""

LOSE = """
import django; django.setup()
import shutil
from django.conf import settings
from apps.patients.models import Patient
Patient.objects.all().update(full_name="Changed")
shutil.rmtree(settings.DATA_DIR / "private")
"""


@pytest.mark.parametrize("encrypted", [False, True])
def test_restore_drill(tmp_path, encrypted):
    data = tmp_path / "data"
    extra = {"SEHATDESK_BACKUP_KEY": backup.generate_key()} if encrypted else {}
    _manage(data, "migrate", "-v0", env_extra=extra)
    _manage(data, env_extra=extra, code=SEED)

    out = _manage(data, "backup", env_extra=extra)
    archive = Path(out.split("Backup written: ")[1].split()[0])
    assert archive.exists() and archive.name.endswith(".zip.enc" if encrypted else ".zip")
    assert "OK:" in _manage(data, "backup", "--verify", str(archive), env_extra=extra)

    _manage(data, env_extra=extra, code=LOSE)
    assert _manage(data, env_extra=extra, code=COUNT).strip() == "0"

    out = _manage(data, "restore", str(archive), "--yes", env_extra=extra)
    assert "Restored" in out
    assert _manage(data, env_extra=extra, code=COUNT).strip() == "1"
    assert (data / "private" / "documents" / "1" / "scan.pdf").read_bytes() == b"%PDF-1.4 drill"
    assert list(data.glob("pre-restore-*/sehatdesk.sqlite3"))  # the replaced data was kept


def test_restore_needs_confirmation(tmp_path):
    data = tmp_path / "data"
    _manage(data, "migrate", "-v0")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SEHATDESK_", "DJANGO_"))}
    env.update(SEHATDESK_DATA_DIR=str(data), SEHATDESK_ENV_FILE=str(tmp_path / "empty.env"))
    result = subprocess.run(
        [sys.executable, "manage.py", "restore", "whatever.zip"],
        cwd=settings.BASE_DIR, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert result.returncode != 0 and "--yes" in result.stderr
