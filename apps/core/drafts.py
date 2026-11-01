"""Unsaved forms kept on the server for their owner, so a stray Back or a closed tab loses nothing.

static/js/app.js (sdDrafts) sends what is typed and offers it back on the same page. Each person
sees only their own. The page itself still checks permissions when the draft is restored and saved.
"""

import json
from datetime import timedelta

from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import FormDraft

KEEP_DAYS = 14
MAX_PER_USER = 50
MAX_BYTES = 256 * 1024


def _mine(request):
    return FormDraft.objects.filter(user=request.user, organization=request.organization)


def _local_path(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//") and len(value) <= 300


def _purge(request):
    _mine(request).filter(updated_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)).delete()
    extra = _mine(request).values_list("pk", flat=True)[MAX_PER_USER:]
    FormDraft.objects.filter(pk__in=list(extra)).delete()


@require_GET
def for_page(request):
    drafts = _mine(request).filter(path=request.GET.get("path", ""))
    return JsonResponse(
        {
            "drafts": [
                {
                    "form": d.form_key,
                    "data": d.data,
                    "saved": timezone.localtime(d.updated_at).strftime("%d %b, %H:%M"),
                }
                for d in drafts
            ]
        }
    )


@require_POST
def save(request):
    path, form_key = request.POST.get("path", ""), request.POST.get("form", "")[:200]
    raw = request.POST.get("data", "")
    if not _local_path(path) or not form_key or len(raw) > MAX_BYTES:
        return HttpResponseBadRequest()
    try:
        data = json.loads(raw)
    except ValueError:
        return HttpResponseBadRequest()
    FormDraft.objects.update_or_create(
        user=request.user,
        path=path,
        form_key=form_key,
        defaults={
            "organization": request.organization,
            "title": request.POST.get("title", "")[:200],
            "data": data,
        },
    )
    _purge(request)
    return JsonResponse({"saved": timezone.localtime().strftime("%H:%M")})


@require_POST
def discard(request):
    drafts = _mine(request).filter(path=request.POST.get("path", ""))
    if request.POST.get("form"):
        drafts = drafts.filter(form_key=request.POST["form"])
    drafts.delete()
    if request.POST.get("back"):
        return redirect("core:drafts")
    return JsonResponse({"ok": True})


@require_GET
def page(request):
    _purge(request)
    return render(request, "core/drafts.html", {"drafts": _mine(request), "keep_days": KEEP_DAYS})
