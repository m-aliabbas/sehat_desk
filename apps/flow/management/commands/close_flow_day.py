"""Close patient-flow visits left open on earlier days (apps/flow close_stale_visits).

The Patient flow board does this by itself on its first view each day; schedule this command
(or run it at closing time) where nobody opens the board."""

from django.core.management.base import BaseCommand

from apps.flow.services import close_stale_visits


class Command(BaseCommand):
    help = "Close patient-flow visits still open from earlier days."

    def handle(self, *args, **options):
        closed = close_stale_visits()
        self.stdout.write(f"Closed {closed} visit(s) from earlier days.")
