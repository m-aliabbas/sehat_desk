"""Launcher helpers and the safe-update path (launcher/, PROJECT_NOTES §3)."""

import socket
import sys

import pytest
from django.conf import settings
from django.shortcuts import resolve_url

from launcher import common, main, mdns, server


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
                    "disk_migrations": {},
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
    monkeypatch.setattr(backup, "_snapshot_database", lambda folder: "snapshot.sqlite3")
    return done


def test_update_backs_up_before_migrating(monkeypatch, steps):
    _fake_plan(monkeypatch, ["m"], {("other", "m"): 1})
    server.migrate_safely()
    assert steps == ["backup", "migrate"]


def test_first_start_has_nothing_to_back_up(monkeypatch, steps):
    _fake_plan(monkeypatch, ["m"], {})
    server.migrate_safely()
    assert steps == ["migrate"]


def test_no_pending_migrations_does_nothing(monkeypatch, steps):
    _fake_plan(monkeypatch, [], {("other", "m"): 1})
    server.migrate_safely()
    assert steps == []


def test_failed_backup_stops_the_update(monkeypatch, steps):
    from apps.core import backup

    _fake_plan(monkeypatch, ["m"], {("other", "m"): 1})

    def fail(**kwargs):
        raise backup.BackupError("disk full")

    monkeypatch.setattr(backup, "create_backup", fail)
    with pytest.raises(backup.BackupError):
        server.migrate_safely()
    assert steps == []


def test_database_from_a_newer_version_is_refused(monkeypatch, steps):
    from apps.core import safe_migrate

    _fake_plan(monkeypatch, [], {("patients", "9999_future"): 1})
    with pytest.raises(safe_migrate.NewerDatabase):
        server.migrate_safely()
    assert steps == []


def test_only_windows_uses_the_single_instance_mutex():
    if sys.platform != "win32":
        assert main._already_running() is False


class _FakeInfo:
    def __init__(self, addresses):
        self.addresses = addresses

    def parsed_addresses(self):
        return self.addresses


class _FakeZeroconf:
    def __init__(self, found):
        self.found = found

    def get_service_info(self, *args, **kwargs):
        return self.found


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        (None, None),
        (_FakeInfo(["192.168.1.50"]), None),  # this computer's own earlier record
        (_FakeInfo(["192.168.1.77"]), "192.168.1.77"),
    ],
)
def test_finds_another_server_on_the_network(found, expected):
    advertiser = mdns.Advertiser(80)
    advertiser.ips = ["192.168.1.50"]
    advertiser.zc = _FakeZeroconf(found)
    assert advertiser._find_other_server() == expected


def test_clients_may_keep_the_address_for_longer_than_the_default():
    info = mdns.Advertiser(80)._service_info(["192.168.1.50"])
    assert info.host_ttl == mdns.HOST_TTL_SECONDS == 4 * 3600


def test_new_lan_address_is_allowed_without_a_restart(monkeypatch, settings):
    settings.ALLOWED_HOSTS = ["sehatdesk.local", "192.168.1.50"]
    monkeypatch.setattr(common, "lan_ips", lambda: ["192.168.1.60", "192.168.1.50"])
    server.allow_current_lan_ips()
    assert settings.ALLOWED_HOSTS == ["sehatdesk.local", "192.168.1.50", "192.168.1.60"]


@pytest.mark.django_db
def test_pages_are_compressed_for_slow_wifi(client):
    response = client.get(
        resolve_url(settings.LOGIN_URL), follow=True, headers={"accept-encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response["Content-Encoding"] == "gzip"
