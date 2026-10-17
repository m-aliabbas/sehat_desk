"""Logging, request correlation and basic metrics for an on-premise install.

- Every request gets an ID (`X-Request-ID`), carried into every log line and error report, so a
  staff member's "it failed at 10:42" can be matched to the log.
- `RedactFilter` masks phone numbers, CNICs and emails in log messages. It is a safety net:
  code must still log IDs and event names only (CLAUDE.md).
- `JsonFormatter` writes one JSON object per line for the log file on deployed machines.
- `metrics` keeps request counts and latency in process memory (one Waitress process per
  hospital), exported at /health/metrics/ in Prometheus text format.
"""

import contextvars
import json
import logging
import re
import threading
import traceback
import uuid
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id(incoming: str | None = None) -> str:
    """Reuse a well-formed incoming ID (from a proxy), otherwise make one."""
    if incoming and _REQUEST_ID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex


# --- PHI redaction ------------------------------------------------------------------

_REDACTIONS = [
    # CNIC: 12345-1234567-1 or 13 digits
    (re.compile(r"\b\d{5}-?\d{7}-?\d\b"), "[cnic]"),
    # Pakistani mobiles: 03001234567, 0300-1234567, +923001234567, 923001234567
    (re.compile(r"(?<!\d)(?:\+?92|0)3\d{2}[- ]?\d{7}(?!\d)"), "[phone]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactFilter(logging.Filter):
    """Masks identifiers in the final message. Formats args first so they are covered too."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format string: leave the record as it is
            return True
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg, record.args = cleaned, None
        return True


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            entry["exception"] = safe_traceback(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def safe_traceback(exc_info) -> str:
    """Exception type and stack frames only. Exception messages can quote patient data
    (a validation message with a name), so they are dropped."""
    exc_type, _exc, tb = exc_info
    frames = traceback.extract_tb(tb)
    lines = [f'  File "{f.filename}", line {f.lineno}, in {f.name}' for f in frames[-15:]]
    return "\n".join([*lines, f"{exc_type.__module__}.{exc_type.__qualname__}"])


# --- Metrics -----------------------------------------------------------------------

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.started_at = datetime.now(UTC)
            self.requests: dict[tuple[str, str], int] = {}
            self.buckets = [0] * len(LATENCY_BUCKETS)
            self.latency_sum = 0.0
            self.latency_count = 0
            self.exceptions = 0
            self.client_errors = 0

    def observe(self, method: str, status: int, seconds: float):
        key = (method, f"{status // 100}xx")
        with self._lock:
            self.requests[key] = self.requests.get(key, 0) + 1
            self.latency_sum += seconds
            self.latency_count += 1
            for i, bound in enumerate(LATENCY_BUCKETS):
                if seconds <= bound:
                    self.buckets[i] += 1

    def count_exception(self):
        with self._lock:
            self.exceptions += 1

    def count_client_error(self):
        with self._lock:
            self.client_errors += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "started_at": self.started_at,
                "requests": dict(self.requests),
                "buckets": list(self.buckets),
                "latency_sum": self.latency_sum,
                "latency_count": self.latency_count,
                "exceptions": self.exceptions,
                "client_errors": self.client_errors,
            }

    def prometheus(self) -> str:
        s = self.snapshot()
        out = [
            "# HELP sehatdesk_requests_total HTTP requests by method and status class.",
            "# TYPE sehatdesk_requests_total counter",
        ]
        for (method, status), n in sorted(s["requests"].items()):
            out.append(f'sehatdesk_requests_total{{method="{method}",status="{status}"}} {n}')
        out += [
            "# HELP sehatdesk_request_seconds Request latency.",
            "# TYPE sehatdesk_request_seconds histogram",
        ]
        for bound, n in zip(LATENCY_BUCKETS, s["buckets"], strict=True):
            out.append(f'sehatdesk_request_seconds_bucket{{le="{bound}"}} {n}')
        out += [
            f'sehatdesk_request_seconds_bucket{{le="+Inf"}} {s["latency_count"]}',
            f"sehatdesk_request_seconds_sum {s['latency_sum']:.6f}",
            f"sehatdesk_request_seconds_count {s['latency_count']}",
            "# TYPE sehatdesk_exceptions_total counter",
            f"sehatdesk_exceptions_total {s['exceptions']}",
            "# TYPE sehatdesk_client_errors_total counter",
            f"sehatdesk_client_errors_total {s['client_errors']}",
            "# TYPE sehatdesk_process_start_time_seconds gauge",
            f"sehatdesk_process_start_time_seconds {s['started_at'].timestamp():.0f}",
        ]
        return "\n".join(out) + "\n"


metrics = Metrics()


class SafeTextFormatter(logging.Formatter):
    """Text log lines whose tracebacks leave out exception messages (deployed machines)."""

    def formatException(self, ei) -> str:  # noqa: N802 (logging API name)
        return safe_traceback(ei)
