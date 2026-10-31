"""The server process (sehat_desk.exe --server): update the database, then serve.

A safety backup is taken before any migration of an existing database. If that backup or a
migration fails, the database is put back, the server does not start and the data stays as it was.
A database from a newer version is refused, so an old installer cannot damage it.
"""

import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from launcher import common

KEEP_SAFETY_BACKUPS = 3
LAN_CHECK_SECONDS = 60


def redirect_output() -> None:
    """A windowed exe has no console: send stray output and tracebacks to a log file."""
    path = common.log_dir() / "server.log"
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        path.unlink()
    stream = open(path, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
    sys.stdout = sys.stderr = stream


def migrate_safely() -> None:
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from apps.core import backup, safe_migrate

    executor = MigrationExecutor(connection)
    if safe_migrate.newer_migrations(executor.loader):
        raise safe_migrate.NewerDatabase(
            "This data is from a newer version of SehatDesk. Install the latest version."
        )
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not plan:
        return
    if executor.loader.applied_migrations:
        common.write_status("Saving a safety backup before updating")
        safety = backup.backup_dir() / "pre-update"
        backup.create_backup(key=backup.shared_or_configured_key(), out_dir=safety)
        backup.prune(safety, keep=KEEP_SAFETY_BACKUPS)
        common.write_status("Updating the database")
        with tempfile.TemporaryDirectory(dir=settings.DATA_DIR) as tmp:
            snapshot = None
            if connection.vendor == "sqlite":
                snapshot = Path(tmp) / backup._snapshot_database(Path(tmp))
            safe_migrate.migrate_or_roll_back(
                snapshot, undone="The update could not be applied. Your data was not changed."
            )
    else:
        common.write_status("Preparing the database (first start)")
        call_command("migrate", interactive=False, verbosity=1)


def prepare(port: int) -> None:
    common.configure_env(port)
    import django

    django.setup()


def allow_current_lan_ips() -> None:
    """A new DHCP address must work without a restart, or Django answers it with 400. Same-origin
    form posts pass CSRF through the Host header, so only this list needs the new address."""
    from django.conf import settings

    for ip in common.lan_ips():
        if ip not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.append(ip)


def _follow_lan_address() -> None:
    while True:
        time.sleep(LAN_CHECK_SECONDS)
        try:
            allow_current_lan_ips()
        except Exception:  # keep serving on the addresses already allowed
            traceback.print_exc()


def run(port: int) -> int:
    redirect_output()
    try:
        common.write_status("Starting")
        prepare(port)
        migrate_safely()
        common.write_status("Starting the web server")
    except Exception as err:
        from apps.core.backup import BackupError

        traceback.print_exc()
        message = (
            str(err) if isinstance(err, BackupError) else "The database could not be prepared."
        )
        common.write_status(f"error: {message}")
        return 1

    import serve

    threading.Thread(target=_follow_lan_address, daemon=True).start()
    try:
        serve.run("0.0.0.0", port)
    except Exception:
        traceback.print_exc()
        common.write_status("error: The web server could not start.")
        return 1
    return 0


def manage(argv: list[str]) -> int:
    """sehat_desk.exe --manage restore FILE --yes: Django commands for an installed copy.
    The exe has no console, so output goes to logs/server.log."""
    redirect_output()
    prepare(common.PORTS[0])
    from django.core.management import execute_from_command_line

    execute_from_command_line(["manage.py", *argv])
    return 0


def self_test(out_file: str) -> int:
    """Run by the build on the finished exe: proves the bundle has what it needs. Returns non-zero
    if anything fails. PDF is required on Windows, where the bundle carries its libraries."""
    import json

    prepare(common.PORTS[0])
    report = {}

    def check(name, func):
        try:
            report[name] = func() or "ok"
        except Exception as err:  # a report, so every failure is captured
            report[name] = f"FAILED: {err!r}"

    def templates():
        from django.template.loader import get_template

        get_template("core/system.html")
        get_template("layouts/auth.html")

    def static():
        from django.conf import settings

        if not (common.resource_dir() / "staticfiles" / "css" / "app.css").is_file():
            raise FileNotFoundError(settings.STATIC_ROOT)

    def drive_client():
        import pydrive2.auth  # noqa: F401
        import pydrive2.drive  # noqa: F401
        from oauth2client.tools import ClientRedirectServer  # noqa: F401

        from apps.core import gdrive

        gdrive._load_client()

    def license_client():
        from firebase_admin import credentials, firestore  # noqa: F401

        from apps.core import licensing

        credentials.Certificate(licensing._load_credentials())
        return f"device {licensing.device_id()[:12]}"

    def mdns():
        import zeroconf  # noqa: F401

        return ",".join(common.lan_ips()) or "no LAN address"

    def tk():
        import tkinter  # noqa: F401

    def pdf():
        from apps.core.pdf import weasyprint_html

        html = weasyprint_html()
        if html is None:
            raise ImportError("WeasyPrint native libraries missing")
        output = html(string="<p>test</p>").write_pdf(uncompressed_pdf=True)
        if b"/Font" not in output:
            raise ValueError("PDF has no fonts")

    def migrations():
        from django.db.migrations.loader import MigrationLoader

        return f"{len(MigrationLoader(None).disk_migrations)} on disk"

    for name, func in (
        ("templates", templates),
        ("static", static),
        ("drive_client", drive_client),
        ("license_client", license_client),
        ("mdns", mdns),
        ("tk", tk),
        ("migrations", migrations),
        ("pdf", pdf),
    ):
        check(name, func)
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    required = [k for k in report if k != "pdf" or sys.platform == "win32"]
    return 1 if any(str(report[k]).startswith("FAILED") for k in required) else 0
