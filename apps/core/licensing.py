"""Device licence, the same scheme as vibe_pos: one Firestore document per device.

The first start registers the device with a one-month licence. To extend or stop a device, edit
its document (`expire_date`, `approved`, `disabled`) in the Firebase console. The answer is kept in
an encrypted file in the data folder, so a computer can stay offline for a month. No patient data
is ever sent. Only installed copies enforce this (SEHATDESK_LICENSE_ENFORCED).
"""

import base64
import hashlib
import json
import logging
import platform
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse

from launcher.common import version as installed_version

logger = logging.getLogger(__name__)

CACHE_FILE = "license_cache.enc"
OFFLINE_DAYS = 30
DEFAULT_TERM_DAYS = 30
WARN_DAYS = 5
TIMEOUT_SECONDS = 15
RECHECK_ACTIVE_SECONDS = 6 * 3600
RECHECK_BLOCKED_SECONDS = 5 * 60

CHECKING = "Checking the licence."
NO_INTERNET = "Connect this computer to the internet once to check the licence."

_lock = threading.Lock()
_verdict: dict | None = None  # the last answer from Firebase or the cache


# --- Device -------------------------------------------------------------------------------


def _machine_id() -> str:
    """Stays the same across Windows updates, user accounts and network adapters."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                return winreg.QueryValueEx(key, "MachineGuid")[0]
        if sys.platform == "darwin":
            output = subprocess.check_output(  # noqa: S603
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], text=True, timeout=5
            )
            for line in output.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
    except (OSError, IndexError, subprocess.SubprocessError):
        logger.warning("Machine ID unavailable; using the network address")
    return str(uuid.getnode())


def device_id() -> str:
    payload = {"system": platform.system(), "machine": platform.machine(), "id": _machine_id()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# --- Local cache --------------------------------------------------------------------------


def _cache_path() -> Path:
    return Path(settings.DATA_DIR) / CACHE_FILE


def _cache_fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _save_cache(device: str, expires_at: datetime) -> None:
    payload = {
        "device": device,
        "expire_date": expires_at.isoformat(),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    try:
        _cache_path().write_bytes(_cache_fernet().encrypt(json.dumps(payload).encode()))
    except OSError:
        logger.warning("Licence cache could not be written")


def _load_cache(device: str) -> datetime | None:
    """The cached expiry, if this device was verified online in the last month."""
    try:
        payload = json.loads(_cache_fernet().decrypt(_cache_path().read_bytes()))
        verified_at = _parse_expiry(payload["verified_at"])
        expires_at = _parse_expiry(payload["expire_date"])
    except (OSError, ValueError, KeyError, InvalidToken):
        return None
    if payload.get("device") != device or not verified_at or not expires_at:
        return None
    if datetime.now(UTC) >= verified_at + timedelta(days=OFFLINE_DAYS):
        return None
    return expires_at


def _parse_expiry(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --- Firebase -----------------------------------------------------------------------------


def _load_credentials() -> dict:
    fernet = Fernet(settings.SEHATDESK_LICENSE_CREDENTIALS_KEY.encode())
    encrypted = Path(settings.SEHATDESK_LICENSE_CREDENTIALS_FILE).read_bytes()
    return json.loads(fernet.decrypt(encrypted))


def _firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore

    try:
        app = firebase_admin.get_app()
    except ValueError:
        app = firebase_admin.initialize_app(credentials.Certificate(_load_credentials()))
    return firestore.client(app)


def _register(document) -> datetime:
    """A device seen for the first time gets the default term."""
    from firebase_admin import firestore

    expires_at = datetime.now(UTC) + timedelta(days=DEFAULT_TERM_DAYS)
    document.set(
        {
            "approved": True,
            "disabled": False,
            "created_at": firestore.SERVER_TIMESTAMP,
            "expire_date": expires_at,
            "app_version": installed_version(),
            "fingerprint_payload": {
                "system": platform.system(),
                "node": socket.gethostname(),
                "machine": platform.machine(),
            },
        },
        merge=True,
        timeout=TIMEOUT_SECONDS,
    )
    return expires_at


def _ask_firebase(device: str) -> tuple[datetime | None, str]:
    """(expiry, "") when licensed, else (None, the reason). Raises when Firebase cannot answer."""
    document = (
        _firestore_client().collection(settings.SEHATDESK_LICENSE_COLLECTION).document(device)
    )
    snapshot = document.get(retry=None, timeout=TIMEOUT_SECONDS)
    if not snapshot.exists:
        return _register(document), ""
    data = snapshot.to_dict() or {}
    expires_at = _parse_expiry(data.get("expire_date"))
    if data.get("disabled"):
        return None, "This computer's licence has been disabled."
    if data.get("approved") is not True:
        return None, "This computer's licence is not approved."
    if expires_at is None:
        return None, "This computer's licence has no valid end date."
    return expires_at, ""


# --- Status -------------------------------------------------------------------------------


def refresh() -> dict:
    """Ask Firebase now. If it cannot be reached, the cache decides."""
    global _verdict
    device = device_id()
    with _lock:
        try:
            expires_at, reason = _ask_firebase(device)
            if expires_at:
                _save_cache(device, expires_at)
            else:
                _cache_path().unlink(missing_ok=True)
        except Exception as err:  # no internet, firewall, bad credential: keep serving from cache
            logger.warning("Licence check failed: %s", type(err).__name__)
            expires_at = _load_cache(device)
            reason = "" if expires_at else NO_INTERNET
        _verdict = {"expires_at": expires_at, "reason": reason}
    return status()


def status() -> dict:
    """{enforced, active, device_id, expires_at, days_remaining, warn, message}. Never blocks on
    the network: until the first answer arrives, only the cache can say the licence is good."""
    if not settings.SEHATDESK_LICENSE_ENFORCED:
        return {"enforced": False, "active": True}
    device = device_id()
    verdict = _verdict
    if verdict is None:
        cached = _load_cache(device)
        verdict = {"expires_at": cached, "reason": "" if cached else CHECKING}
    expires_at = verdict["expires_at"]
    now = datetime.now(UTC)
    active = bool(expires_at) and now < expires_at
    message = verdict["reason"]
    if expires_at and not active:
        message = "This computer's licence has ended."
    days = max(0, ceil((expires_at - now).total_seconds() / 86400)) if expires_at else None
    return {
        "enforced": True,
        "active": active,
        "device_id": device,
        "expires_at": expires_at,
        "days_remaining": days,
        "warn": active and days <= WARN_DAYS,
        "message": message,
        "checking": message == CHECKING,
    }


def worker_loop() -> None:
    """Started by serve.py: checks at start, then keeps checking while the server runs."""
    while True:
        try:
            active = refresh()["active"]
        except Exception:  # keep the server up; the last answer stays in force
            logger.exception("Licence check failed")
            active = False
        time.sleep(RECHECK_ACTIVE_SECONDS if active else RECHECK_BLOCKED_SECONDS)


class LicenseMiddleware:
    """Without a licence every page leads to the licence page. Backups keep running."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.SEHATDESK_LICENSE_ENFORCED or status()["active"]:
            return self.get_response(request)
        page = reverse("core:license")
        if request.path == page or request.path.startswith(("/health/", "/static/")):
            return self.get_response(request)
        if request.headers.get("HX-Request"):
            return HttpResponse(status=204, headers={"HX-Redirect": page})
        return HttpResponseRedirect(page)
