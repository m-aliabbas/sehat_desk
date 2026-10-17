"""Runs the time-based notification checks (sweeps.py) while staff use the app.

There is no separate job runner on the hospital machine, so after a signed-in request the
branch's checks run if they have not run for SEHATDESK_NOTIFICATION_SWEEP_SECONDS. The
timestamp is kept in the cache; with several server processes a check may occasionally run
twice, which dedupe keys make harmless. Set the interval to 0 to turn this off (tests do).
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


class NotificationSweepMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        interval = getattr(settings, "SEHATDESK_NOTIFICATION_SWEEP_SECONDS", 300)
        branch = getattr(request, "branch", None)
        if (
            interval
            and branch is not None
            and request.method == "GET"
            and getattr(request, "user", None) is not None
            and request.user.is_authenticated
            and response.status_code < 400
        ):
            key = f"sehatdesk:notification-sweep:{branch.pk}"
            if cache.add(key, timezone.now().isoformat(), timeout=interval):
                from .sweeps import run

                run(branch)
        return response
