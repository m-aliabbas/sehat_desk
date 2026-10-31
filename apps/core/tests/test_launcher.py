"""Launcher helpers and the safe-update path (launcher/, PROJECT_NOTES §3)."""

import socket
import sys

import pytest

from launcher import common, main, server


def test_address_has_no_port_on_80():
    assert common.base_url("sehatdesk.local", 80) == "http://sehatdesk.local"
    assert common.base_url("sehatdesk.local", 8000) == "http://sehatdesk.local:8000"


def test_data_folder_is_appdata_on_windows(monkeypatch):
    monkeypatch.delenv("SEHATDESK_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "/users/x/AppData/Local")
    assert common.data_dir().as_posix() == "/users/x/AppData/Local/SehatDesk"


def test_env_allows_this_computer_and_lan_address(monkeypatch, tmp_path):
    for name in (
        "SEHATDESK_ENV_FILE",
        "SEHATDESK_ALLOWED_HOSTS",
        "SEHATDESK_CSRF_TRUSTED_ORIGINS",
        "SEHATDESK_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEHATDESK_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(common, "lan_ips", lambda: ["192.168.1.50"])
    common.configure_env(8000)
    import os

    assert "sehatdesk.local" in os.environ["SEHATDESK_ALLOWED_HOSTS"]
    assert "192.168.1.50" in os.environ["SEHATDESK_ALLOWED_HOSTS"]
    assert "http://192.168.1.50:8000" in os.environ["SEHATDESK_CSRF_TRUSTED_ORIGINS"]
    assert os.environ["SEHATDESK_ENV"] == "production"
    for name in ("SEHATDESK_ENV", "SEHATDESK_ALLOWED_HOSTS", "SEHATDESK_CSRF_TRUSTED_ORIGINS"):
        os.environ.pop(name)


def test_busy_port_is_skipped(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        sock.listen()
        busy = sock.getsockname()[1]
        assert common.port_free(busy) is False
        monkeypatch.setattr(common, "PORTS", (busy, 0))
        assert common.choose_port() == 0


def test_status_file_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("SEHATDESK_DATA_DIR", str(tmp_path))
    common.write_status("Updating the database")
    assert common.read_status() == "Updating the database"


def _fake_plan(monkeypatch, plan, applied):
    from django.db.migrations import executor as executor_module

    class Fake:
        def __init__(self, connection):
            self.loader = type(
                "L",
                (),
                {
                    "graph": type("G", (), {"leaf_nodes": staticmethod(lambda: [])}),
                    "applied_migrations": applied,
                },
            )

        def migration_plan(self, nodes):
            return plan

    monkeypatch.setattr(executor_module, "MigrationExecutor", Fake)


@pytest.fixture
def steps(monkeypatch, settings, tmp_path):
    settings.DATA_DIR = tmp_path
    monkeypatch.setenv("SEHATDESK_DATA_DIR", str(tmp_path))
    from django.core import management

    from apps.core import backup

    done = []
    monkeypatch.setattr(management, "call_command", lambda *a, **k: done.append("migrate"))
    monkeypatch.setattr(backup, "create_backup", lambda **k: done.append("backup"))
    return done


def test_update_backs_up_before_migrating(monkeypatch, steps):
    _fake_plan(monkeypatch, ["m"], {"x": 1})
    server.migrate_safely()
    assert steps == ["backup", "migrate"]


def test_first_start_has_nothing_to_back_up(monkeypatch, steps):
    _fake_plan(monkeypatch, ["m"], {})
    server.migrate_safely()
    assert steps == ["migrate"]


def test_no_pending_migrations_does_nothing(monkeypatch, steps):
    _fake_plan(monkeypatch, [], {"x": 1})
    server.migrate_safely()
    assert steps == []


def test_failed_backup_stops_the_update(monkeypatch, steps):
    from apps.core import backup

    _fake_plan(monkeypatch, ["m"], {"x": 1})

    def fail(**kwargs):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "create_backup", fail)
    with pytest.raises(backup.BackupError):
        server.migrate_safely()
    assert steps == []


def test_only_windows_uses_the_single_instance_mutex():
    if sys.platform != "win32":
        assert main._already_running() is False
