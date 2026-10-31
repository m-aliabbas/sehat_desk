"""Local-day filters (apps/core/dates.py)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core.dates import in_days, on_day, start_of_day

KARACHI = ZoneInfo("Asia/Karachi")
DAY = date(2026, 11, 1)


@pytest.fixture(autouse=True)
def local_time():
    with timezone.override(KARACHI):
        yield


def test_a_day_runs_from_local_midnight_to_the_next():
    kwargs = on_day("created_at", DAY)
    assert kwargs["created_at__gte"] == datetime(2026, 11, 1, tzinfo=KARACHI)
    assert kwargs["created_at__lt"] == datetime(2026, 11, 2, tzinfo=KARACHI)


def test_either_end_of_a_range_may_be_open():
    assert in_days("at") == {}
    assert set(in_days("at", DAY)) == {"at__gte"}
    assert in_days("at", end=DAY) == {"at__lt": start_of_day(date(2026, 11, 2))}


@pytest.mark.django_db
def test_range_finds_the_same_rows_as_the_date_lookup(org):
    from apps.core.models import ErrorReport

    def row(hour, day=1):
        report = ErrorReport.objects.create(source=ErrorReport.Source.SERVER, path="/x")
        ErrorReport.objects.filter(pk=report.pk).update(
            occurred_at=datetime(2026, 11, day, hour, 30, tzinfo=KARACHI)
        )

    row(0), row(23), row(0, day=2)
    rows = ErrorReport.objects.all()
    assert rows.filter(occurred_at__date=DAY).count() == 2
    assert rows.filter(**on_day("occurred_at", DAY)).count() == 2


@pytest.mark.django_db
def test_dashboard_does_not_call_python_for_every_row(client, admin_user, branch):
    client.force_login(admin_user)
    with CaptureQueriesContext(connection) as queries:
        assert client.get("/").status_code == 200
    assert not [q for q in queries if "django_datetime" in q["sql"]]
