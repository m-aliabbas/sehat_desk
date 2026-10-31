"""The server process (sehat_desk.exe --server): update the database, then serve.

A safety backup is taken before any migration of an existing database. If that backup or a
migration fails, the server does not start and the data is left as it was.
"""

import sys
import traceback

from launcher import common

KEEP_SAFETY_BACKUPS = 3


def redirect_output() -> None:
    """A windowed exe has no console: send stray output and tracebacks to a log file."""
    path = common.log_dir() / "server.log"
    if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
        path.unlink()
    stream = open(path, "a", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
    sys.stdout = sys.stderr = stream


def migrate_safely() -> None:
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from apps.core import backup

    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not plan:
        return
    if executor.loader.applied_migrations:
        common.write_status("Saving a safety backup before updating")
        safety = backup.backup_dir() / "pre-update"
        backup.create_backup(key=backup.shared_or_configured_key(), out_dir=safety)
        backup.prune(safety, keep=KEEP_SAFETY_BACKUPS)
        common.write_status("Updating the database")
    else:
        common.write_status("Preparing the database (first start)")
    call_command("migrate", interactive=False, verbosity=1)


def prepare(port: int) -> None:
    common.configure_env(port)
    import django

    django.setup()


def run(port: int) -> int:
    redirect_output()
    try:
        common.write_status("Starting")
        prepare(port)
        migrate_safely()
        common.write_status("Starting the web server")
    except Exception:
        traceback.print_exc()
        common.write_status("error: The database could not be prepared. Your data was not changed.")
        return 1

    import serve

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
    only for things that must work; PDF libraries are reported but optional."""
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
        html(string="<p>test</p>").write_pdf()

    def migrations():
        from django.db.migrations.loader import MigrationLoader

        return f"{len(MigrationLoader(None).disk_migrations)} on disk"

    for name, func in (
        ("templates", templates),
        ("static", static),
        ("drive_client", drive_client),
        ("mdns", mdns),
        ("tk", tk),
        ("migrations", migrations),
        ("pdf", pdf),
    ):
        check(name, func)
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    required = [k for k in report if k != "pdf"]
    return 1 if any(str(report[k]).startswith("FAILED") for k in required) else 0
