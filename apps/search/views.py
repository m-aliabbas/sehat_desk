"""Global search: full results page, the top-bar suggestion dropdown and recent searches."""

from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from . import services


@require_GET
def search_page(request):
    q = request.GET.get("q", "").strip()
    only = request.GET.get("type", "")
    providers = services.providers_for(request)
    if only not in {p.key for p in providers}:
        only = ""
    groups = services.search(request, q, limit=25 if only else 10, only=only)
    if groups:
        services.remember(request, q)
    return render(
        request,
        "search/page.html",
        {
            "q": q,
            "only": only,
            "groups": groups,
            "providers": providers,
            "too_short": 0 < len(q) < services.MIN_LENGTH,
            "recent": services.recent(request),
        },
    )


@require_GET
def suggest(request):
    """htmx fragment under the top-bar search box (top 5 per type)."""
    q = request.GET.get("q", "").strip()
    return render(
        request,
        "search/_suggest.html",
        {
            "q": q,
            "groups": services.search(request, q, limit=5),
            "recent": [] if q else services.recent(request),
            "too_short": len(q) < services.MIN_LENGTH,
        },
    )


@require_POST
def clear_recent(request):
    request.session.pop(services.RECENT_KEY, None)
    nxt = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
        nxt = "search:page"
    return redirect(nxt)
