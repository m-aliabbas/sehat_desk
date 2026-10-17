"""python manage.py backup                 make a backup now (encrypted if SEHATDESK_BACKUP_KEY)
python manage.py backup --list          list backups, newest first
python manage.py backup --verify FILE   check a backup can be restored (checksums, database)
python manage.py backup --generate-key  print a new encryption key for SEHATDESK_BACKUP_KEY
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core import backup


class Command(BaseCommand):
    help = "Create, list or verify SehatDesk backups."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--list", action="store_true")
        group.add_argument("--verify", metavar="FILE")
        group.add_argument("--generate-key", action="store_true")
        parser.add_argument("--out", help="Folder to write to (default: the backup folder)")

    def handle(self, *args, list=False, verify=None, generate_key=False, out=None, **options):
        try:
            if generate_key:
                self.stdout.write(backup.generate_key())
                self.stderr.write(
                    "Set this as SEHATDESK_BACKUP_KEY and store a copy away from this computer."
                )
                return
            if list:
                for b in backup.list_backups():
                    flag = "encrypted" if b.encrypted else "plain"
                    self.stdout.write(f"{b.name}  {b.size / 1e6:8.1f} MB  {flag}")
                return
            key = backup.configured_key()
            if verify:
                manifest = backup.verify_backup(verify, key=key)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"OK: {len(manifest['files'])} files, created {manifest['created']}"
                    )
                )
                return
            info = backup.create_backup(key=key, out_dir=out)
        except backup.BackupError as err:
            raise CommandError(str(err)) from err
        note = "" if info.encrypted else " (not encrypted: set SEHATDESK_BACKUP_KEY)"
        self.stdout.write(self.style.SUCCESS(f"Backup written: {info.path}{note}"))
