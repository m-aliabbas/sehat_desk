"""Run the time-based notification checks for every active branch.

    python manage.py notifications_sweep

Optional: schedule it (Windows Task Scheduler / launchd) every few minutes. The app also runs
the checks itself while staff use it (NotificationSweepMiddleware).
"""

from django.core.management.base import BaseCommand

from apps.notifications.sweeps import run
from apps.organizations.models import Branch


class Command(BaseCommand):
    help = "Raise reminders, stock and expiry notifications for every active branch."

    def handle(self, *args, **options):
        branches = Branch.objects.filter(is_active=True).select_related("organization")
        for branch in branches:
            run(branch)
        self.stdout.write(self.style.SUCCESS(f"Checked {branches.count()} branch(es)."))
