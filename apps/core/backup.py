"""Backups and restore (Phase 29). See docs/BACKUP_AND_RECOVERY.md for the procedures.

A backup is one file, `sehatdesk-<yyyymmdd-hhmmss>.zip` (or `.zip.enc` when encrypted), holding:
  database.sqlite3       consistent snapshot (SQLite online backup API, safe while staff work)
                         or database.pgdump (pg_dump custom format) on PostgreSQL
  private/documents/...  patient documents
  media/...              logos, patient photos, lab attachments
  manifest.json          when, what, app migrations, and a SHA-256 for every file

The secret key is NOT included: restoring without it only signs everyone out. Keep backups
encrypted (SEHATDESK_BACKUP_KEY) whenever they leave the server (USB, cloud).

Encryption: AES-256-GCM in 1 MiB chunks. Each chunk is authenticated with its index and a
last-chunk flag, so a modified, reordered or truncated file fails to decrypt.
"""

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

MAGIC = b"SDBK1\n"
CHUNK = 1024 * 1024
NAME_PREFIX = "sehatdesk-"
FORMAT_VERSION = 1


class BackupError(Exception):
    pass


@dataclass
class BackupInfo:
    path: Path
    name: str
    created: datetime
    size: int
    encrypted: bool


# --- Settings ---------------------------------------------------------------------------


def backup_dir() -> Path:
    return Path(getattr(settings, "SEHATDESK_BACKUP_DIR", "") or settings.DATA_DIR / "backups")


def keep_count() -> int:
    return int(getattr(settings, "SEHATDESK_BACKUP_KEEP", 14))


def configured_key() -> bytes | None:
    raw = getattr(settings, "SEHATDESK_BACKUP_KEY", "")
    return decode_key(raw) if raw else None


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def decode_key(text: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(text.strip().encode())
    except (ValueError, TypeError) as err:
        raise BackupError("The backup key is not valid base64.") from err
    if len(key) != 32:
        raise BackupError("The backup key must be 32 bytes (use: manage.py backup --generate-key).")
    return key


# --- Encryption -------------------------------------------------------------------------


def _aad(index: int, last: bool) -> bytes:
    return MAGIC + struct.pack(">Q?", index, last)


def encrypt_file(src: Path, dst: Path, key: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC)
        index = 0
        chunk = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK)
            last = not nxt
            nonce = os.urandom(12)
            sealed = aes.encrypt(nonce, chunk, _aad(index, last))
            fout.write(nonce + struct.pack(">I", len(sealed)) + sealed)
            if last:
                break
            chunk, index = nxt, index + 1


def decrypt_file(src: Path, dst: Path, key: bytes) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupError("Not an encrypted SehatDesk backup.")
        index, finished = 0, False
        while True:
            head = fin.read(16)
            if not head:
                break
            if len(head) < 16 or finished:
                raise BackupError("The backup file is damaged (unexpected data).")
            nonce, size = head[:12], struct.unpack(">I", head[12:])[0]
            sealed = fin.read(size)
            # The last chunk is only known by trying: it must authenticate as last.
            for last in (False, True):
                try:
                    fout.write(aes.decrypt(nonce, sealed, _aad(index, last)))
                    finished = last
                    break
                except InvalidTag:
                    continue
            else:
                raise BackupError("Wrong backup key, or the backup file was changed.")
            index += 1
        if not finished:
            raise BackupError("The backup file is incomplete (it was cut short).")


# --- Create -----------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _snapshot_database(target_dir: Path) -> str:
    """Write a consistent database copy into target_dir; returns the file name."""
    if connection.vendor == "sqlite":
        src_path = settings.DATABASES["default"]["NAME"]
        out = target_dir / "database.sqlite3"
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return out.name
    if connection.vendor == "postgresql":
        if not shutil.which("pg_dump"):
            raise BackupError("pg_dump is not installed on this server.")
        db = settings.DATABASES["default"]
        out = target_dir / "database.pgdump"
        env = {**os.environ, "PGPASSWORD": str(db.get("PASSWORD") or "")}
        cmd = [
            "pg_dump",
            "-Fc",
            "-f",
            str(out),
            "-h",
            str(db.get("HOST") or "localhost"),
            "-p",
            str(db.get("PORT") or 5432),
            "-U",
            str(db.get("USER") or ""),
            db["NAME"],
        ]
        subprocess.run(cmd, check=True, env=env, capture_output=True)
        return out.name
    raise BackupError(f"Backups are not supported for the {connection.vendor} database.")


def _folders():
    data = Path(settings.DATA_DIR)
    return [
        ("private/documents", data / "private" / "documents"),
        ("media", Path(settings.MEDIA_ROOT)),
    ]


def _migrations() -> dict:
    with connection.cursor() as cursor:
        cursor.execute("SELECT app, name FROM django_migrations")
        rows = cursor.fetchall()
    latest: dict = {}
    for app, name in rows:
        latest[app] = max(latest.get(app, ""), name)
    return latest


def create_backup(*, key: bytes | None = None, out_dir: Path | None = None) -> BackupInfo:
    """Write one backup file and prune old ones. Safe to run while the server is in use."""
    out_dir = Path(out_dir or backup_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    final = out_dir / f"{NAME_PREFIX}{stamp}.zip{'.enc' if key else ''}"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        tmp = Path(tmp)
        files: dict[str, str] = {}
        db_name = _snapshot_database(tmp)
        files[db_name] = _sha256(tmp / db_name)
        archive = tmp / "backup.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.write(tmp / db_name, db_name)
            for prefix, folder in _folders():
                if not folder.exists():
                    continue
                for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                    arc = f"{prefix}/{path.relative_to(folder).as_posix()}"
                    zf.write(path, arc)
                    files[arc] = _sha256(path)
            manifest = {
                "format": FORMAT_VERSION,
                "created": timezone.now().isoformat(),
                "environment": settings.SEHATDESK_ENV,
                "database": connection.vendor,
                "database_file": db_name,
                "migrations": _migrations(),
                "files": files,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        partial = final.with_name(final.name + ".partial")
        if key:
            encrypt_file(archive, partial, key)
        else:
            shutil.move(archive, partial)
        partial.replace(final)  # never leave a half-written backup under the final name
    prune()
    return _info(final)


def _info(path: Path) -> BackupInfo:
    stat = path.stat()
    return BackupInfo(
        path=path,
        name=path.name,
        created=datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone()),
        size=stat.st_size,
        encrypted=path.name.endswith(".enc"),
    )


def list_backups(directory: Path | None = None) -> list[BackupInfo]:
    directory = Path(directory or backup_dir())
    if not directory.exists():
        return []
    found = [
        _info(p)
        for p in directory.iterdir()
        if p.name.startswith(NAME_PREFIX) and p.name.endswith((".zip", ".zip.enc"))
    ]
    return sorted(found, key=lambda b: b.name, reverse=True)


def prune(directory: Path | None = None, keep: int | None = None) -> list[Path]:
    """Keep the newest `keep` backups (SEHATDESK_BACKUP_KEEP, default 14); delete older ones."""
    keep = keep_count() if keep is None else keep
    removed = []
    for old in list_backups(directory)[keep:]:
        old.path.unlink()
        removed.append(old.path)
    return removed


def backup_due(hours: float) -> bool:
    latest = next(iter(list_backups()), None)
    return latest is None or (timezone.now() - latest.created).total_seconds() >= hours * 3600


# --- Verify and restore ------------------------------------------------------------------


def _open_archive(path: Path, key: bytes | None, workdir: Path) -> Path:
    path = Path(path)
    if path.name.endswith(".enc"):
        if key is None:
            raise BackupError("This backup is encrypted: give the backup key.")
        plain = workdir / "backup.zip"
        decrypt_file(path, plain, key)
        return plain
    return path


def verify_backup(path: Path, *, key: bytes | None = None) -> dict:
    """Decrypt (if needed), check every file's SHA-256 and the database's integrity.
    Returns the manifest. Raises BackupError on any problem."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = _open_archive(path, key, tmp)
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile as err:
            raise BackupError("The backup file is not a valid archive.") from err
        with zf:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except KeyError as err:
                raise BackupError("The backup has no manifest.") from err
            for name, digest in manifest["files"].items():
                _safe_member(name)
                h = hashlib.sha256()
                with zf.open(name) as fh:
                    for block in iter(lambda fh=fh: fh.read(CHUNK), b""):
                        h.update(block)
                if h.hexdigest() != digest:
                    raise BackupError(f"{name} does not match its checksum.")
            if manifest["database_file"].endswith(".sqlite3"):
                zf.extract(manifest["database_file"], tmp)
                db = sqlite3.connect(tmp / manifest["database_file"])
                try:
                    ok = db.execute("PRAGMA integrity_check").fetchone()[0]
                    db.execute("SELECT COUNT(*) FROM django_migrations").fetchone()
                finally:
                    db.close()
                if ok != "ok":
                    raise BackupError(f"Database integrity check failed: {ok}")
    return manifest


def _safe_member(name: str) -> None:
    if name.startswith(("/", "\\")) or ".." in Path(name).parts:
        raise BackupError(f"Unsafe path in backup: {name}")


def restore_backup(path: Path, *, key: bytes | None = None, data_dir: Path | None = None) -> Path:
    """Replace the database, documents and media with the backup's copy.

    The server must be stopped. The current files are moved to
    DATA_DIR/pre-restore-<timestamp>/ first, so a restore can itself be undone.
    Returns that folder. SQLite only; PostgreSQL dumps are restored with pg_restore.
    """
    manifest = verify_backup(path, key=key)
    if manifest["database"] != "sqlite":
        raise BackupError("PostgreSQL backups are restored with pg_restore (see the docs).")
    data_dir = Path(data_dir or settings.DATA_DIR)
    db_path = Path(settings.DATABASES["default"]["NAME"])
    connection.close()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    aside = data_dir / f"pre-restore-{stamp}"
    aside.mkdir(parents=True)
    targets = [(db_path, aside / db_path.name)] + [
        (folder, aside / prefix) for prefix, folder in _folders()
    ]
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        targets.append((side, aside / side.name))
    for src, dst in targets:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
    with tempfile.TemporaryDirectory() as tmp:
        archive = _open_archive(path, key, Path(tmp))
        with zipfile.ZipFile(archive) as zf:
            for name in manifest["files"]:
                _safe_member(name)
                if name == manifest["database_file"]:
                    target = db_path
                else:
                    prefix, rest = next(
                        (p, name[len(p) + 1 :]) for p, _ in _folders() if name.startswith(p + "/")
                    )
                    target = dict(_folders())[prefix] / rest
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    return aside
