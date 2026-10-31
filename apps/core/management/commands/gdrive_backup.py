"""python manage.py gdrive_backup            back up and upload to Google Drive now
python manage.py gdrive_backup --connect  sign in with Google (opens a browser on this computer)
python manage.py gdrive_backup --status   show the connected account and last upload
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core import gdrive


class Command(BaseCommand):
    help = "Upload a backup to Google Drive."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--connect", action="store_true")
        group.add_argument("--status", action="store_true")

    def handle(self, *args, connect=False, status=False, **options):
        if status:
            for name, value in gdrive.status().items():
                self.stdout.write(f"{name}: {value}")
            return
        try:
            if connect:
                gdrive.connect()
                self.stdout.write(self.style.SUCCESS("Google Drive connected."))
            else:
                self.stdout.write(self.style.SUCCESS(f"Uploaded {gdrive.run_backup()}."))
        except gdrive.GoogleDriveError as err:
            raise CommandError(str(err)) from err
