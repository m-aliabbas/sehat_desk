"""Security headers and small hardening helpers (Phase 26).

Content-Security-Policy: the app is offline-first and loads nothing from other hosts, so every
resource type is limited to this server. Scripts still need 'unsafe-inline' (print buttons use
onclick) and 'unsafe-eval' (Alpine.js evaluates x-data expressions); the policy's main job is to
stop injected markup from sending data anywhere else (connect-src, img-src, form-action).
"""

import functools

from django.core.cache import cache
from django.http import HttpResponse
from django.utils.cache import patch_cache_control

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and "Cache-Control" not in response.headers
            and response.get("Content-Type", "").startswith(("text/html", "application/json"))
        ):
            # Signed-in pages carry patient data: never keep them in the browser cache of a
            # shared reception PC (the Back button after sign-out must not show them).
            patch_cache_control(response, private=True, no_store=True)
        return response


def client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "")


def rate_limit(key: str, *, limit: int, seconds: int, methods=("POST",)):
    """Allow `limit` requests per client IP per `seconds` window for this view; then 429."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method in methods:
                bucket = f"rl:{key}:{client_ip(request)}"
                cache.add(bucket, 0, seconds)
                try:
                    count = cache.incr(bucket)
                except ValueError:  # expired between add and incr
                    cache.set(bucket, 1, seconds)
                    count = 1
                if count > limit:
                    response = HttpResponse(
                        "Too many attempts. Wait a few minutes and try again.", status=429
                    )
                    response["Retry-After"] = str(seconds)
                    return response
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
