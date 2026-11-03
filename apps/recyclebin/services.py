"""Delete to the Recycle bin, restore, and erase for good (only records nothing else uses).

Deleting needs `records.delete` plus the module's own permission (registry). Opening the bin,
restoring and erasing need `admin.recyclebin.manage`. Every step is audit-logged.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.utils import timezone

from apps.audit import services as audit

from .registry import KINDS, Kind, kind_of

DELETE = "records.delete"
MANAGE_BIN = "admin.recyclebin.manage"


class BinError(ValidationError):
    pass


def get_kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError as err:
        raise BinError("That kind of record cannot be deleted.") from err


def can_delete(request, kind: Kind) -> bool:
    return {DELETE, kind.manage_perm} <= set(request.sd_perms)


def deletable_keys(request) -> set[str]:
    """Kinds this person may delete, for templates (`can_delete.product`)."""
    return {key for key, kind in KINDS.items() if can_delete(request, kind)}


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
