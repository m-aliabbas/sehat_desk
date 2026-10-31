"""Backups and restores from Settings → Backup run in a background thread, one at a time.

Staff keep working during a backup. A restore copies the chosen database into the live one through
SQLite's backup API, so the server keeps running and its connections stay valid. While it runs,
RestoreMaintenanceMiddleware answers other pages with "please wait".

The job's status is a file in the data folder, not a table: a restore replaces every table.
"""

import json
import logging
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.utils import timezone

from apps.core import backup, safe_migrate

logger = logging.getLogger(__name__)

STATE_FILE = "backup_job.json"
INCOMING_DIR = "restore-incoming"
KEEP_SAFETY_BACKUPS = 3
UPLOAD_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".zip", ".zip.enc")
REQUIRED_TABLES = ("django_migrations", "accounts_user")

_lock = threading.Lock()
restoring = threading.Event()


class JobBusy(Exception):
    pass


# --- Status -------------------------------------------------------------------------------


def _state_path() -> Path:
    return Path(settings.DATA_DIR) / STATE_FILE


def status() -> dict:
    """{kind, state: running|done|failed, message, finished}; empty if nothing has run."""
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if state.get("state") == "running" and not _lock.locked():
        # The server was stopped mid-job, so nothing is running any more.
        state.update(state="failed", message="Stopped when the server stopped. Please try again.")
    return state


def _set_status(kind: str, state: str, message: str) -> None:
    payload = {
        "kind": kind,
        "state": state,
        "message": message,
        "finished": timezone.localtime().isoformat() if state != "running" else "",
    }
    path = _state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _start(kind: str, starting: str, task) -> None:
    if not _lock.acquire(blocking=False):
        raise JobBusy("A backup or restore is already running.")
    _set_status(kind, "running", starting)

    def runner():
        try:
            _set_status(kind, "done", task())
        except backup.BackupError as err:
            _set_status(kind, "failed", str(err))
        except Exception:
            logger.exception("%s job failed", kind)
            _set_status(kind, "failed", f"The {kind} failed. See the server log.")
        finally:
            restoring.clear()
            _lock.release()
            connection.close()

    threading.Thread(target=runner, daemon=True).start()


# --- Backup -------------------------------------------------------------------------------


def _run_backup() -> str:
    info = backup.create_backup(key=backup.configured_key())
    return f"Backup written: {info.name}"


def start_backup() -> None:
    _start("backup", "Writing the backup…", _run_backup)


# --- Restore ------------------------------------------------------------------------------


def check_sqlite(path: Path) -> None:
    """Refuse anything that is not a healthy SehatDesk database (e.g. another app's .db file)."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
    except sqlite3.Error as err:
        raise backup.BackupError("This file is not a SQLite database.") from err
    if result != "ok":
        raise backup.BackupError(f"The database file is damaged: {result}")
    if not set(REQUIRED_TABLES) <= tables:
        raise backup.BackupError("This is not a SehatDesk database.")


def _copy_into_live_database(source: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(settings.DATABASES["default"]["NAME"], timeout=60)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _swap_folders(extracted: Path, stamp: str) -> None:
    """Replace documents and media with the backup's, keeping the current ones aside."""
    aside = Path(settings.DATA_DIR) / f"pre-restore-{stamp}"
    for prefix, folder in backup._folders():
        new = extracted / prefix
        if folder.exists():
            target = aside / prefix
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(target))
        if new.exists():
            folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new), str(folder))


def restore_live(path: Path) -> str:
    """Restore a .sqlite3 file (database only) or a backup .zip/.zip.enc (database, documents and
    media) into the running server. A safety backup of the current data is taken first."""
    path = Path(path)
    if connection.vendor != "sqlite":
        raise backup.BackupError("PostgreSQL servers are restored with pg_restore (see the docs).")
    is_archive = path.name.endswith((".zip", ".zip.enc"))
    key = backup.shared_or_configured_key()
    with tempfile.TemporaryDirectory(dir=settings.DATA_DIR) as tmp:
        tmp = Path(tmp)
        if is_archive:
            manifest = backup.verify_backup(path, key=key)
            if manifest["database"] != "sqlite":
                raise backup.BackupError("This backup is from a PostgreSQL server.")
            with zipfile.ZipFile(backup._open_archive(path, key, tmp)) as zf:
                for name in manifest["files"]:
                    backup._safe_member(name)
                    zf.extract(name, tmp / "extracted")
            database = tmp / "extracted" / manifest["database_file"]
        else:
            database = path
        check_sqlite(database)
        safe_migrate.refuse_newer_file(database)

        safety = backup.backup_dir() / "pre-restore"
        backup.create_backup(key=key, out_dir=safety)
        backup.prune(safety, keep=KEEP_SAFETY_BACKUPS)

        rollback = tmp / "rollback"
        rollback.mkdir()
        snapshot = rollback / backup._snapshot_database(rollback)

        restoring.set()
        _copy_into_live_database(database)
        # A backup from an older version opens on this version.
        connection.close()
        safe_migrate.migrate_or_roll_back(
            snapshot, undone="This backup could not be updated. The current data was kept."
        )
        if is_archive:
            _swap_folders(tmp / "extracted", timezone.localtime().strftime("%Y%m%d-%H%M%S"))
    return f"Restored from {path.name}. Staff may need to sign in again."


def start_restore(path: Path, *, delete_after: bool = False) -> None:
    def task():
        try:
            return restore_live(path)
        finally:
            if delete_after:
                # Reading a WAL-mode upload leaves its -wal and -shm files beside it.
                for suffix in ("", "-wal", "-shm"):
                    Path(f"{path}{suffix}").unlink(missing_ok=True)

    _start("restore", "Restoring…", task)


def start_restore_from_drive(file_id: str) -> None:
    from apps.core import gdrive

    def task():
        folder = incoming_dir()
        try:
            local = gdrive.download_backup(file_id, folder)
        except gdrive.GoogleDriveError as err:
            raise backup.BackupError(str(err)) from err
        try:
            return restore_live(local)
        finally:
            local.unlink(missing_ok=True)

    _start("restore", "Downloading from Google Drive…", task)


def incoming_dir() -> Path:
    folder = Path(settings.DATA_DIR) / INCOMING_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_upload(upload) -> Path:
    name = Path(upload.name).name
    if not name.lower().endswith(UPLOAD_SUFFIXES):
        raise backup.BackupError("Choose a .sqlite3, .db, .zip or .zip.enc file.")
    target = incoming_dir() / name
    with open(target, "wb") as out:
        for chunk in upload.chunks():
            out.write(chunk)
    return target


def local_backup(name: str) -> Path:
    """A file from the backup folder, by exact name from its listing."""
    for info in backup.list_backups():
        if info.name == name:
            return info.path
    raise backup.BackupError("That backup was not found.")


class RestoreMaintenanceMiddleware:
    """Only while the database is being replaced: nobody saves into data about to change."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if restoring.is_set() and not request.path.startswith(("/health/", "/static/")):
            return HttpResponse(
                "<!doctype html><meta http-equiv='refresh' content='5'><title>SehatDesk</title>"
                "<p style='font:16px sans-serif;margin:3rem'>SehatDesk is restoring a backup. "
                "This page will reload when it is finished.</p>",
                status=503,
            )
        return self.get_response(request)
