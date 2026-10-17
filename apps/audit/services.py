"""Write audit events: audit.record(request, "staff.create", obj, changes=...)."""

import logging

from django.forms.models import model_to_dict

from .models import AuditLog

logger = logging.getLogger(__name__)


def client_ip(request):
    # Direct LAN connections only; no proxy headers are trusted.
    return request.META.get("REMOTE_ADDR") if request is not None else None


def record(
    request,
    action: str,
    instance=None,
    *,
    changes: dict | None = None,
    summary: str = "",
    actor=None,
    organization=None,
):
    """Store one audit event. Never raises: a failed audit write is logged, not shown to users."""
    user = getattr(request, "user", None)
    if actor is None and user is not None and user.is_authenticated:
        actor = user
    if organization is None:
        organization = getattr(request, "organization", None) or getattr(
            actor, "organization", None
        )
    try:
        return AuditLog.objects.create(
            organization=organization,
            branch=getattr(request, "branch", None),
            actor=actor,
            action=action,
            entity=type(instance).__name__ if instance is not None else "",
            record_id=str(instance.pk) if instance is not None else "",
            summary=summary[:255],
            changes=changes or {},
            ip_address=client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT", "")[:255] if request else ""),
        )
    except Exception:  # noqa: BLE001 - auditing must not break the user's action
        logger.exception("audit write failed for action=%s", action)
        return None


def record_ai_acceptance(request, instance, *, feature: str, draft_id="", fields=(), summary=""):
    """A clinician accepted an AI draft into a record (PRD: AI output is a draft only).

    Call this only from the view that saves the clinician-reviewed text, never automatically.
    Stores the feature, the draft reference and which fields were filled, not the text itself.
    """
    return record(
        request,
        "ai.draft_accept",
        instance,
        changes={"feature": feature, "draft": str(draft_id), "fields": list(fields)},
        summary=summary or f"{feature} draft accepted",
    )


def diff(before: dict, instance, fields) -> dict:
    """Changed fields between a snapshot (from snapshot()) and the saved instance."""
    after = snapshot(instance, fields)
    return {f: [before.get(f), after.get(f)] for f in fields if before.get(f) != after.get(f)}


def snapshot(instance, fields) -> dict:
    data = model_to_dict(instance, fields=fields)
    return {
        k: (str(v) if v is not None and not isinstance(v, (bool, int, str)) else v)
        for k, v in data.items()
    }
