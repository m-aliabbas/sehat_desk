"""Raise and read in-app notifications.

    notify(org, Event.LOW_STOCK, "5 products are low", branch=branch,
           permission="pharmacy.inventory.manage", url=..., dedupe_key=f"low-stock-{day}")

Recipients can be named users, holders of a system role (roles=["nurse"]) or holders of a
permission code, in the given branch (a role assigned for all branches counts everywhere).
notify() never raises: a failed notification is logged by ID and the caller carries on.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User

from .models import Event, Level, Notification

logger = logging.getLogger(__name__)


def recipients(organization, *, branch=None, users=(), roles=(), permission=None):
    """Active users of the organization matching any of: users, roles, permission."""
    ids = {u.pk for u in users if u is not None}
    base = User.objects.filter(organization=organization, is_active=True)
    branch_q = Q(assignments__branch__isnull=True)
    if branch is not None:
        branch_q |= Q(assignments__branch=branch)
    scoped = base.filter(branch_q, assignments__role__is_active=True)
    if roles:
        ids |= set(
            scoped.filter(assignments__role__system_key__in=roles).values_list("pk", flat=True)
        )
    if permission:
        ids |= set(
            scoped.filter(assignments__role__permissions__code=permission).values_list(
                "pk", flat=True
            )
        )
    return base.filter(pk__in=ids)


def notify(
    organization,
    event: str,
    title: str,
    *,
    body: str = "",
    url: str = "",
    branch=None,
    level: str = Level.INFO,
    users=(),
    roles=(),
    permission: str | None = None,
    exclude=None,
    dedupe_key: str = "",
) -> int:
    """Create one notification per recipient. Returns how many were created."""
    try:
        people = recipients(
            organization, branch=branch, users=users, roles=roles, permission=permission
        )
        if exclude is not None:
            people = people.exclude(pk=exclude.pk)
        rows = [
            Notification(
                organization=organization,
                branch=branch,
                recipient=person,
                event=event,
                level=level,
                title=title[:160],
                body=body[:300],
                url=url[:300],
                dedupe_key=dedupe_key[:120],
            )
            for person in people
        ]
        if dedupe_key:
            seen = set(
                Notification.objects.filter(
                    dedupe_key=dedupe_key[:120], recipient__in=[r.recipient for r in rows]
                ).values_list("recipient_id", flat=True)
            )
            rows = [r for r in rows if r.recipient_id not in seen]
        # ignore_conflicts: a parallel request raising the same deduped alert is not an error.
        Notification.objects.bulk_create(rows, ignore_conflicts=bool(dedupe_key))
        return len(rows)
    except Exception:  # noqa: BLE001 - notifications must never break the user's action
        logger.exception("notification failed event=%s org=%s", event, organization.pk)
        return 0


def for_user(user, organization):
    return Notification.objects.filter(recipient=user, organization=organization)


def unread_count(user, organization) -> int:
    return for_user(user, organization).filter(read_at__isnull=True).count()


def mark_read(notification) -> None:
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at", "updated_at"])


def mark_all_read(user, organization) -> int:
    return (
        for_user(user, organization)
        .filter(read_at__isnull=True)
        .update(read_at=timezone.now(), updated_at=timezone.now())
    )


EVENT_ICONS = {
    Event.APPOINTMENT_UPCOMING: "calendar",
    Event.APPOINTMENT_CANCELLED: "calendar",
    Event.LAB_READY: "flask",
    Event.LAB_CRITICAL: "alert-triangle",
    Event.LOW_STOCK: "package",
    Event.EXPIRY: "package",
    Event.REMINDER: "bell",
    Event.GENERAL: "info",
}
