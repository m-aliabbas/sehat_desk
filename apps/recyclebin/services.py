"""Delete to the Recycle bin, restore, and erase for good (only records nothing else uses).

Deleting needs the area's delete code plus its edit permission (registry
`delete_perm`). Opening the bin,
restoring and erasing need `admin.recyclebin.manage`. Every step is audit-logged.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.utils import timezone

from apps.audit import services as audit

from .registry import KINDS, Kind, kind_of

DELETE_PERMS = tuple(sorted({k.delete_perm for k in KINDS.values()}))
MANAGE_BIN = "admin.recyclebin.manage"


class BinError(ValidationError):
    pass


def get_kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError as err:
        raise BinError("That kind of record cannot be deleted.") from err


def can_edit(request, kind: Kind) -> bool:
    held = getattr(request, "sd_perms", frozenset())
    return any(code in held for code in kind.manage_perms)


def can_delete(request, kind: Kind) -> bool:
    return kind.delete_perm in getattr(request, "sd_perms", frozenset()) and can_edit(request, kind)


def deletable_keys(request) -> set[str]:
    """Kinds this person may delete, for templates: {% if "product" in deletable %}."""
    return {key for key, kind in KINDS.items() if can_delete(request, kind)}


def editable_keys(request) -> set[str]:
    """Kinds this person may edit, for templates: {% if "ward" in editable %}."""
    return {key for key, kind in KINDS.items() if can_edit(request, kind)}


def _changes(obj, fields, before) -> dict:
    return {
        f: [str(before[f]), str(getattr(obj, f))] for f in fields if before[f] != getattr(obj, f)
    }


def save_edit(request, form, kind: Kind):
    """Save the shared edit form, audit-logging the fields that changed."""
    if not can_edit(request, kind):
        raise BinError("You are not allowed to edit this.")
    obj = form.instance
    _check_org(request, obj)
    before = {f: getattr(type(obj).objects.get(pk=obj.pk), f) for f in kind.edit_fields}
    with transaction.atomic():
        obj = form.save()
        audit.record(
            request, f"{kind.key}.update", obj, changes=_changes(obj, kind.edit_fields, before)
        )
    return obj


def _check_org(request, obj):
    if obj.organization_id != request.organization.pk:
        raise BinError("Not found.")


@transaction.atomic
def delete(request, obj, *, reason: str = ""):
    kind = kind_of(obj)
    if kind is None:
        raise BinError("That kind of record cannot be deleted.")
    if not can_delete(request, kind):
        raise BinError("You are not allowed to delete this.")
    return send_to_bin(request, obj, reason=reason)


@transaction.atomic
def send_to_bin(request, obj, *, reason: str = ""):
    """Move to the bin without the permission check: for a delete someone allowed asked for and
    an approver finishes (approvals: write off all and delete). Blockers still apply."""
    kind = kind_of(obj)
    _check_org(request, obj)
    obj = type(obj).all_objects.select_for_update(of=("self",)).get(pk=obj.pk)
    if obj.deleted_at:
        raise BinError(f"{obj} is already in the Recycle bin.")
    if kind.blocker and (why := kind.blocker(obj)):
        raise BinError(f"{obj} cannot be deleted: {why}")
    obj.deleted_at = timezone.now()
    obj.deleted_by = request.user
    obj.deleted_reason = reason.strip()[:200]
    obj.save(update_fields=["deleted_at", "deleted_by", "deleted_reason", "updated_at"])
    audit.record(request, f"{kind.key}.delete", obj, summary=f"{obj} → Recycle bin")
    return obj


def delete_many(request, kind: Kind, ids) -> tuple[int, list[str]]:
    """Each record on its own: the ones that can go, go; the others say why not."""
    done, problems = 0, []
    for obj in kind.model_class.objects.filter(organization=request.organization, pk__in=ids):
        try:
            delete(request, obj)
        except BinError as err:
            problems.append(err.message)
        else:
            done += 1
    return done, problems


def deleted(request, kind_key: str = "", q: str = "") -> list:
    """Every deleted record of this hospital, newest first, as rows for the bin screen."""
    rows = []
    for kind in KINDS.values():
        if kind_key and kind.key != kind_key:
            continue
        qs = kind.model_class.all_objects.filter(
            organization=request.organization, deleted_at__isnull=False
        ).select_related("deleted_by")
        for obj in qs:
            if q and q.lower() not in str(obj).lower():
                continue
            rows.append({"kind": kind, "obj": obj})
    rows.sort(key=lambda r: r["obj"].deleted_at, reverse=True)
    return rows


def find(request, kind: Kind, pk):
    return kind.model_class.all_objects.filter(
        organization=request.organization, deleted_at__isnull=False, pk=pk
    ).first()


def _need_bin(request):
    if MANAGE_BIN not in request.sd_perms:
        raise BinError("You are not allowed to open the Recycle bin.")


def restore(request, obj):
    _need_bin(request)
    _check_org(request, obj)
    kind = kind_of(obj)
    obj.deleted_at = None
    obj.deleted_by = None
    obj.deleted_reason = ""
    try:
        with transaction.atomic():
            obj.save(update_fields=["deleted_at", "deleted_by", "deleted_reason", "updated_at"])
    except IntegrityError as err:
        raise BinError(
            f"Another {kind.label.lower()} now has the same name or code as {obj}. "
            "Rename that one first, then restore."
        ) from err
    audit.record(request, f"{kind.key}.restore", obj, summary=str(obj))
    return obj


def erase(request, obj):
    """Remove for good, but only when no other record points at it."""
    _need_bin(request)
    _check_org(request, obj)
    kind = kind_of(obj)
    collector = Collector(using=router.db_for_write(type(obj)))
    try:
        collector.collect([obj])
    except (ProtectedError, RestrictedError) as err:
        raise BinError(f"{obj} is used by past records, so it stays in the bin.") from err
    others = sum(len(rows) for rows in collector.data.values()) - 1
    if others or any(qs.exists() for qs in collector.fast_deletes if hasattr(qs, "exists")):
        raise BinError(f"{obj} is used by past records, so it stays in the bin.")
    label = str(obj)
    audit.record(request, f"{kind.key}.erase", obj, summary=label)
    obj.delete()
    return label
