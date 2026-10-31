"""Filter a datetime column by local calendar day.

Use these instead of `**on_day("field", day`. On SQLite that lookup calls back into Python for every
row in the table), so busy pages use more CPU as the data grows. A plain range runs inside the
database and can use an index, on PostgreSQL too. The day is in the current time zone, the same
as `__date`.
"""

from datetime import date, datetime, time, timedelta

from django.utils import timezone


def start_of_day(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


def on_day(field: str, day: date) -> dict:
    return in_days(field, day, day)


def in_days(field: str, start: date | None = None, end: date | None = None) -> dict:
    """Filter kwargs for start to end, both days included. Either end may be left open."""
    kwargs = {}
    if start:
        kwargs[f"{field}__gte"] = start_of_day(start)
    if end:
        kwargs[f"{field}__lt"] = start_of_day(end + timedelta(days=1))
    return kwargs
