"""Database updates that cannot strand a hospital's data.

Used by the launcher on every start and by a restore. A failed migration puts the database back as
it was, and a database from a newer version is refused, because older code cannot read it safely.
"""

import sqlite3
from pathlib import Path

from django.apps import apps
from django.db import connection
from django.db.migrations.loader import MigrationLoader

from apps.core import backup


class NewerDatabase(backup.BackupError):
    """The data was written by a newer SehatDesk than this one."""


class MigrationFailed(backup.BackupError):
    """A database update failed; the message says what state the data is in."""


def _unknown(applied, loader) -> list[str]:
    ours = {c.label for c in apps.get_app_configs() if c.name.startswith("apps.")}
    known = set(loader.disk_migrations)
    for migration in loader.disk_migrations.values():
        known.update(migration.replaces)
    return sorted(f"{a}.{n}" for a, n in applied if a in ours and (a, n) not in known)


def newer_migrations(loader) -> list[str]:
    """Migrations recorded in the live database that this version does not have."""
    return _unknown(loader.applied_migrations, loader)


def refuse_newer_file(path: Path) -> None:
    """Refuse a SQLite file that a newer version wrote, before it replaces anything."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        applied = db.execute("SELECT app, name FROM django_migrations").fetchall()
    finally:
        db.close()
    if _unknown(applied, MigrationLoader(None)):
        raise NewerDatabase(
            "This backup is from a newer version of SehatDesk. Update this computer first."
        )


def migrate_or_roll_back(snapshot: Path | None, *, undone: str) -> None:
    """Run migrations. If they fail, copy `snapshot` back over the database and raise."""
    from django.core.management import call_command

    from apps.core.backup_jobs import _copy_into_live_database

    try:
        call_command("migrate", interactive=False, verbosity=1)
    except Exception as err:
        connection.close()
        if snapshot is None:
            raise MigrationFailed("The database could not be prepared.") from err
        try:
            _copy_into_live_database(snapshot)
        except Exception as copy_err:
            raise MigrationFailed(
                "The database update failed and could not be undone. Restore the newest safety "
                "backup before using SehatDesk."
            ) from copy_err
        raise MigrationFailed(undone) from err
