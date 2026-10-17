"""Error reporting and request context (see observability.py for the building blocks)."""

import logging
import sys
import time
from datetime import timedelta

from django.core.signals import got_request_exception
from django.db import DatabaseError
from django.utils import timezone

from .models import ErrorReport
from .observability import metrics, new_request_id, redact, request_id_var, safe_traceback

logger = logging.getLogger(__name__)

# Prune old reports on roughly one insert in fifty; cheap with the occurred_at index.
_PRUNE_EVERY = 50
_insert_count = 0


class RequestContextMiddleware:
    """First middleware: request ID, response header, request metrics."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = new_request_id(request.headers.get("X-Request-ID"))
        token = request_id_var.set(request.request_id)
        started = time.perf_counter()
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        metrics.observe(request.method, response.status_code, time.perf_counter() - started)
        response["X-Request-ID"] = request.request_id
        return response


def _context(request):
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    return {
        "request_id": getattr(request, "request_id", "")[:64],
        "organization": getattr(request, "organization", None),
        "branch": getattr(request, "branch", None),
        "user": user if user is not None and user.is_authenticated else None,
        "method": request.method[:10],
        "path": request.path[:300],  # never the query string: it can hold search text
        "view_name": (match.view_name if match else "")[:150],
    }


def _save(**fields):
    global _insert_count
    try:
        ErrorReport.objects.create(**fields)
        _insert_count += 1
        if _insert_count % _PRUNE_EVERY == 0:
            cutoff = timezone.now() - timedelta(days=ErrorReport.RETENTION_DAYS)
            ErrorReport.objects.filter(occurred_at__lt=cutoff).delete()
    except DatabaseError:
        # The database itself may be the problem. The log line below still has the details.
        logger.warning("could not store error report request_id=%s", fields.get("request_id"))


def record_server_error(request, exc_info=None):
    exc_info = exc_info or sys.exc_info()
    if exc_info[0] is None:
        return
    metrics.count_exception()
    _save(
        source=ErrorReport.Source.SERVER,
        error_type=f"{exc_info[0].__module__}.{exc_info[0].__qualname__}"[:200],
        stack=safe_traceback(exc_info),
        **_context(request),
    )


def record_browser_error(request, data: dict):
    metrics.count_client_error()
    page = str(data.get("page") or "").split("?")[0].split("#")[0][:300]
    source = str(data.get("source") or "").split("?")[0][:200]
    line, col = str(data.get("line") or "")[:8], str(data.get("column") or "")[:8]
    ctx = _context(request)
    ctx.update(path=page, method="", view_name="")
    _save(
        source=ErrorReport.Source.BROWSER,
        error_type=redact(str(data.get("type") or "Error"))[:200],
        message=redact(str(data.get("message") or ""))[:300],
        stack=f"{source}:{line}:{col}" if source else "",
        **ctx,
    )


def _on_exception(sender, request=None, **kwargs):
    if request is not None:
        record_server_error(request)


def connect():
    got_request_exception.connect(_on_exception, dispatch_uid="sehatdesk-error-report")
