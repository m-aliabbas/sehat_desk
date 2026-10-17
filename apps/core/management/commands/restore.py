"""python manage.py restore FILE --yes

Stop the SehatDesk server first. The current database, documents and media are moved to
DATA_DIR/pre-restore-<timestamp>/ before the backup is unpacked, so this can be undone.
Encrypted backups use SEHATDESK_BACKUP_KEY. Runs migrations afterwards, so a backup from an
older version opens on this version.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.core import backup


class Command(BaseCommand):
    help = "Restore a SehatDesk backup (server must be stopped)."

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument(
            "--yes", action="store_true", help="Confirm replacing the current data."
        )

    def handle(self, *args, file, yes=False, **options):
        if not yes:
            raise CommandError(
                "This replaces the current data. Stop the server, then run again with --yes."
            )
        try:
            aside = backup.restore_backup(file, key=backup.configured_key())
        except backup.BackupError as err:
            raise CommandError(str(err)) from err
        call_command("migrate", interactive=False, verbosity=0)
        self.stdout.write(self.style.SUCCESS(f"Restored {file}."))
        self.stdout.write(f"The previous data was kept in {aside}.")
