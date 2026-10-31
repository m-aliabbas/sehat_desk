"""Failed updates, newer databases and destructive migrations (apps/core/safe_migrate.py)."""

import sqlite3

import pytest
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import DeleteModel, RemoveField

from apps.core import safe_migrate

# A migration that drops data needs a reviewed entry here, plus a release note.
ALLOWED_DESTRUCTIVE = set()


def test_no_migration_drops_data_without_review():
    found = {
        key
        for key, migration in MigrationLoader(None).disk_migrations.items()
        if migration.__module__.startswith("apps.")
        and any(isinstance(op, RemoveField | DeleteModel) for op in migration.operations)
    }
    assert found <= ALLOWED_DESTRUCTIVE


def test_failed_migration_restores_the_snapshot(settings, tmp_path, monkeypatch):
    live = tmp_path / "live.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    for path, marker in ((live, "half migrated"), (snapshot, "before")):
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE t (v TEXT)")
        db.execute("INSERT INTO t VALUES (?)", (marker,))
        db.commit()
        db.close()
    monkeypatch.setitem(settings.DATABASES["default"], "NAME", str(live))
    from django.core import management

    def fail(*args, **kwargs):
        raise RuntimeError("bad migration")

    monkeypatch.setattr(management, "call_command", fail)
    with pytest.raises(safe_migrate.MigrationFailed, match="not changed"):
        safe_migrate.migrate_or_roll_back(snapshot, undone="Your data was not changed.")
    db = sqlite3.connect(live)
    assert db.execute("SELECT v FROM t").fetchone()[0] == "before"
    db.close()


def _migrations_file(path, app, name):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
    db.execute("INSERT INTO django_migrations VALUES (?, ?)", (app, name))
    db.commit()
    db.close()
    return path


def test_backup_from_a_newer_version_is_refused(tmp_path):
    newer = _migrations_file(tmp_path / "n.sqlite3", "patients", "9999_future")
    with pytest.raises(safe_migrate.NewerDatabase):
        safe_migrate.refuse_newer_file(newer)


def test_known_or_third_party_migrations_are_accepted(tmp_path):
    safe_migrate.refuse_newer_file(
        _migrations_file(tmp_path / "a.sqlite3", "patients", "0001_initial")
    )
    safe_migrate.refuse_newer_file(_migrations_file(tmp_path / "b.sqlite3", "axes", "9999_other"))
