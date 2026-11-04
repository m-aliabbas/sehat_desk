"""Recycle bin screen, the delete endpoints every list's bin button and "Delete selected" bar
post to, and the shared edit page. Editing needs the module's permission, deleting also the
area's delete code (both checked in services); the bin needs `admin.recyclebin.manage`."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required

from . import services
from .forms import edit_form
from .registry import KINDS


def _back(request, fallback: str):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
        return redirect(nxt)
    return redirect(fallback)


def _kind(key):
    try:
        return services.get_kind(key)
    except services.BinError as err:
        raise Http404 from err


# Anyone who may edit some kind reaches the view; the kind's own permission is checked inside.
EDIT_PERMS = tuple(sorted({code for k in KINDS.values() for code in k.manage_perms}))


@permission_required(*EDIT_PERMS, any_of=True)
def edit(request, kind, pk):
    """Edit a record of any kind with `edit_fields` (pencil on its list). Permission is per kind:
    the module's manage permission, checked here and in `services.save_edit`."""
    kind = _kind(kind)
    if not kind.edit_fields:
        raise Http404
    if not services.can_edit(request, kind):
        raise PermissionDenied
    obj = kind.model_class.objects.filter(organization=request.organization, pk=pk).first()
    if obj is None:
        raise Http404
    form = edit_form(kind, request)(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        try:
            obj = services.save_edit(request, form, kind)
        except services.BinError as err:
            messages.error(request, err.message)
        else:
            messages.success(request, f"{obj} saved.")
            return _back(request, reverse(kind.list_url))
    return render(
        request,
        "recyclebin/edit.html",
        {
            "form": form,
            "obj": obj,
            "kind": kind,
            "can_delete": services.can_delete(request, kind),
            "cancel_url": reverse(kind.list_url),
        },
    )


@require_POST
@permission_required(*services.DELETE_PERMS, any_of=True)
def delete_one(request, kind, pk):
    kind = _kind(kind)
    obj = kind.model_class.objects.filter(organization=request.organization, pk=pk).first()
    if obj is None:
        raise Http404
    try:
        services.delete(request, obj, reason=request.POST.get("reason", ""))
    except services.BinError as err:
        if kind.clear_url:
            return redirect(kind.clear_url, pk=obj.pk)
        messages.error(request, err.message)
        return _back(request, kind.url_for(obj))
    messages.success(request, f"{obj} moved to the Recycle bin.")
    return _back(request, kind.list_url)


@require_POST
@permission_required(*services.DELETE_PERMS, any_of=True)
def delete_selected(request, kind):
    kind = _kind(kind)
    ids = [i for i in request.POST.getlist("ids") if i.isdigit()]
    if not ids:
        messages.error(request, "Tick the rows to delete first.")
        return _back(request, kind.list_url)
    if not services.can_delete(request, kind):
        messages.error(request, "You are not allowed to delete these.")
        return _back(request, kind.list_url)
    done, problems = services.delete_many(request, kind, ids)
    if done:
        messages.success(request, f"{done} moved to the Recycle bin.")
    for problem in problems[:5]:
        messages.error(request, problem)
    return _back(request, kind.list_url)


@require_GET
@permission_required(services.MANAGE_BIN)
def bin_list(request):
    kind_key = request.GET.get("kind", "")
    q = request.GET.get("q", "").strip()
    return render(
        request,
        "recyclebin/list.html",
        {
            "rows": services.deleted(request, kind_key if kind_key in KINDS else "", q),
            "kinds": KINDS.values(),
            "kind": kind_key,
            "q": q,
        },
    )


def _act(request, kind, pk, action):
    kind = _kind(kind)
    obj = services.find(request, kind, pk)
    if obj is None:
        raise Http404
    try:
        label = action(request, obj)
    except services.BinError as err:
        messages.error(request, err.message)
        return None
    return label


@require_POST
@permission_required(services.MANAGE_BIN)
def restore(request, kind, pk):
    obj = _act(request, kind, pk, services.restore)
    if obj is not None:
        messages.success(request, f"{obj} restored.")
    return redirect("recyclebin:list")


@require_POST
@permission_required(services.MANAGE_BIN)
def erase(request, kind, pk):
    label = _act(request, kind, pk, services.erase)
    if label is not None:
        messages.success(request, f"{label} erased for good.")
    return redirect("recyclebin:list")


@require_POST
@permission_required(services.MANAGE_BIN)
def restore_selected(request):
    """Ticked rows come as "kind:pk"."""
    done = 0
    for value in request.POST.getlist("ids"):
        key, _, pk = value.partition(":")
        if key in KINDS and pk.isdigit() and _act(request, key, pk, services.restore):
            done += 1
    if done:
        messages.success(request, f"{done} restored.")
    return redirect("recyclebin:list")
