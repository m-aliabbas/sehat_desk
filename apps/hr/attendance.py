"""Attendance: self check-in/out, the daily register, and the monthly sheet (Phase 34)."""

import calendar
from datetime import date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User

from .models import AttendanceRecord, PayProfile, PayrollRun

Status = AttendanceRecord.Status


class AttendanceError(ValidationError):
    pass


def branch_staff(branch):
    """Active staff who can work in this branch (an assignment here or in all branches)."""
    return (
        User.objects.filter(organization_id=branch.organization_id, is_active=True)
        .filter(Q(assignments__branch=branch) | Q(assignments__branch__isnull=True))
        .distinct()
        .order_by("first_name", "last_name", "username")
    )


def profile_for(user):
    return PayProfile.objects.filter(user=user).first()


def is_late(user, when) -> bool:
    profile = profile_for(user)
    start = profile.work_start if profile else time(9)
    grace = profile.late_grace_minutes if profile else 15
    local = timezone.localtime(when)
    limit = datetime.combine(local.date(), start) + timedelta(minutes=grace)
    return local.replace(tzinfo=None) > limit


def is_locked(branch, day: date) -> bool:
    return PayrollRun.objects.filter(
        branch=branch, year=day.year, month=day.month, status=PayrollRun.Status.FINALIZED
    ).exists()


def _require_open(branch, day):
    if is_locked(branch, day):
        raise AttendanceError(
            f"Payroll for {day:%B %Y} is finalized. Attendance for that month is locked."
        )


def today_record(user, organization):
    return AttendanceRecord.objects.filter(
        organization=organization, user=user, date=timezone.localdate()
    ).first()


@transaction.atomic
def check_in(*, request) -> AttendanceRecord:
    now = timezone.now()
    day = timezone.localdate()
    _require_open(request.branch, day)
    rec = today_record(request.user, request.organization)
    if rec and rec.check_in:
        raise AttendanceError(
            f"You already checked in at {timezone.localtime(rec.check_in):%H:%M}."
        )
    status = Status.LATE if is_late(request.user, now) else Status.PRESENT
    if rec:  # marked earlier by an admin (e.g. absent) and now arriving
        rec.check_in, rec.status, rec.self_marked, rec.marked_by = now, status, True, request.user
        rec.save(update_fields=["check_in", "status", "self_marked", "marked_by", "updated_at"])
        return rec
    return AttendanceRecord.objects.create(
        organization=request.organization,
        branch=request.branch,
        user=request.user,
        date=day,
        status=status,
        check_in=now,
        self_marked=True,
        marked_by=request.user,
    )


@transaction.atomic
def check_out(*, request) -> AttendanceRecord:
    rec = today_record(request.user, request.organization)
    if rec is None or rec.check_in is None:
        raise AttendanceError("Check in first.")
    if rec.check_out:
        raise AttendanceError(
            f"You already checked out at {timezone.localtime(rec.check_out):%H:%M}."
        )
    _require_open(rec.branch, rec.date)
    rec.check_out = timezone.now()
    rec.save(update_fields=["check_out", "updated_at"])
    return rec


@transaction.atomic
def mark(*, request, user, day: date, status: str, note="") -> tuple[AttendanceRecord | None, bool]:
    """Admin sets one person's status for a day. Returns (record, changed)."""
    if user.organization_id != request.organization.pk:
        raise AttendanceError("That staff member is not in this hospital.")
    if status not in Status.values:
        raise AttendanceError("Choose a valid attendance status.")
    if day > timezone.localdate():
        raise AttendanceError("Attendance cannot be marked for a future date.")
    _require_open(request.branch, day)
    rec = AttendanceRecord.objects.filter(
        organization=request.organization, user=user, date=day
    ).first()
    note = note.strip()[:200]
    if rec is None:
        rec = AttendanceRecord.objects.create(
            organization=request.organization,
            branch=request.branch,
            user=user,
            date=day,
            status=status,
            note=note,
            marked_by=request.user,
        )
        return rec, True
    if rec.branch_id != request.branch.pk:
        raise AttendanceError(f"{user} was marked in {rec.branch.name} that day.")
    if rec.status == status and rec.note == note:
        return rec, False
    rec.status, rec.note, rec.marked_by, rec.self_marked = status, note, request.user, False
    rec.save(update_fields=["status", "note", "marked_by", "self_marked", "updated_at"])
    return rec, True


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def working_days(year, month, weekly_off) -> int:
    first, last = month_bounds(year, month)
    return sum(
        1
        for i in range((last - first).days + 1)
        if weekly_off is None or (first + timedelta(days=i)).weekday() != weekly_off
    )


def summarize(records) -> dict:
    s = {"present": 0, "late": 0, "half_day": 0, "absent": 0, "leave": 0, "off": 0}
    for r in records:
        s[r.status] = s.get(r.status, 0) + 1
    s["present_days"] = s["present"] + s["late"] + s["half_day"] * 0.5
    s["absent_days"] = s["absent"] + s["half_day"] * 0.5
    return s


def month_sheet(branch, year: int, month: int) -> dict:
    first, last = month_bounds(year, month)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    staff = list(branch_staff(branch))
    records = AttendanceRecord.objects.filter(
        organization_id=branch.organization_id, date__range=(first, last), user__in=staff
    )
    by_user: dict = {}
    for r in records:
        by_user.setdefault(r.user_id, {})[r.date] = r
    rows = []
    for u in staff:
        marks = by_user.get(u.pk, {})
        rows.append(
            {
                "user": u,
                "cells": [marks.get(d) for d in days],
                "totals": summarize(marks.values()),
            }
        )
    return {"days": days, "rows": rows, "locked": is_locked(branch, first)}
