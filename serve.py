"""Production entry point: serves SehatDesk with Waitress.

Pure Python, so it runs the same on Windows and macOS.
Run migrations first: python manage.py migrate
"""

import argparse
import logging
import os
import threading
import time

from waitress import serve

logger = logging.getLogger("sehatdesk.serve")


def _backup_loop(hours: float):
    """Automatic backups: one now if the last is older than `hours`, then check hourly."""
    from apps.core import backup

    while True:
        try:
            if backup.backup_due(hours):
                info = backup.create_backup(key=backup.configured_key())
                logger.warning("automatic backup written name=%s", info.name)
        except Exception:  # keep serving; the System page shows the last good backup
            logger.exception("automatic backup failed")
        time.sleep(3600)


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    parser = argparse.ArgumentParser(description="Serve SehatDesk with Waitress.")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind address (never 127.0.0.1 for LAN use)"
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEHATDESK_PORT", "8000")))
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    from django.conf import settings

    from config.wsgi import application

    if settings.SEHATDESK_BACKUP_HOURS > 0:
        threading.Thread(
            target=_backup_loop, args=(settings.SEHATDESK_BACKUP_HOURS,), daemon=True
        ).start()
    serve(application, host=args.host, port=args.port, threads=args.threads)


if __name__ == "__main__":
    main()
