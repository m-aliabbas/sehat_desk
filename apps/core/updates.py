"""Application updates: Settings → Updates checks GitHub for a newer Windows installer.

The download runs in a background thread and is kept only if its SHA-256 matches the release's
SHA256SUMS.txt. Installing opens the installer on the server computer, where Windows asks for
administrator approval. Status is a file in the data folder, like backup_jobs.
"""

import hashlib
import json
import logging
import os
import re
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import certifi
from django.conf import settings
from django.utils import timezone

from launcher.common import version as installed_version

logger = logging.getLogger(__name__)

RELEASE_API = "https://api.github.com/repos/m-aliabbas/sehat_desk/releases/latest"
ALLOWED_DOWNLOAD_PREFIX = "https://github.com/m-aliabbas/sehat_desk/releases/download/"
INSTALLER_NAME = re.compile(r"^SehatDesk-Setup-(\d+\.\d+\.\d+)\.exe$")
CHECKSUMS_NAME = "SHA256SUMS.txt"
STATE_FILE = "update_job.json"
TIMEOUT_SECONDS = 30
CHUNK_BYTES = 1024 * 1024

_lock = threading.Lock()


class UpdateError(Exception):
    """A message that is safe to show to the administrator."""


class JobBusy(Exception):
    pass


def current_version() -> str:
    return installed_version()


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def is_newer(candidate: str, current: str) -> bool:
    """A development copy has no version number to compare, so nothing counts as newer."""
    try:
        return _version_key(candidate) > _version_key(current)
    except ValueError:
        return False


def can_install_here() -> bool:
    """Only an installed Windows copy can run the installer."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _open(url: str, timeout: int = TIMEOUT_SECONDS):
    request = urllib.request.Request(url, headers={"User-Agent": "SehatDesk-Updater"})
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(request, timeout=timeout, context=context)  # noqa: S310


def latest_release() -> dict:
    """{version, name, url, checksums_url, notes, published} of the newest published installer."""
    try:
        with _open(RELEASE_API) as response:
            release = json.load(response)
    except (OSError, ValueError) as err:
        logger.warning("Update check failed: %s", err)
        raise UpdateError(
            "Could not reach the update server. Check this computer's internet connection."
        ) from err
    assets = {a.get("name"): a.get("browser_download_url", "") for a in release.get("assets", [])}
    for name, url in assets.items():
        match = INSTALLER_NAME.match(name or "")
        checksums_url = assets.get(CHECKSUMS_NAME, "")
        if match and url.startswith(ALLOWED_DOWNLOAD_PREFIX):
            if not checksums_url.startswith(ALLOWED_DOWNLOAD_PREFIX):
                break
            return {
                "version": match.group(1),
                "name": name,
                "url": url,
                "checksums_url": checksums_url,
                "notes": (release.get("body") or "")[:2000],
                "published": (release.get("published_at") or "")[:10],
            }
    raise UpdateError("The latest release has no Windows installer yet.")


# --- Status -------------------------------------------------------------------------------


def _state_path() -> Path:
    return Path(settings.DATA_DIR) / STATE_FILE


def updates_dir() -> Path:
    path = Path(settings.DATA_DIR) / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _set_status(state: str, message: str, **extra) -> None:
    payload = {"state": state, "message": message, **extra}
    if state != "running":
        payload["finished"] = timezone.localtime().isoformat()
    path = _state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def status() -> dict:
    """{state: running|done|failed, message, version, name}; empty if nothing has run."""
    state = _read_state()
    if state.get("state") == "running" and not _lock.locked():
        state.update(state="failed", message="Stopped when the server stopped. Please try again.")
    return state


def downloaded_installer() -> Path | None:
    """The verified installer, if the last download finished and is newer than this copy."""
    state = status()
    name = state.get("name", "")
    if state.get("state") != "done" or not INSTALLER_NAME.match(name):
        return None
    if not is_newer(state.get("version", ""), current_version()):
        return None
    path = updates_dir() / name
    return path if path.is_file() else None


# --- Download and install -----------------------------------------------------------------


def _expected_sha256(release: dict) -> str:
    with _open(release["checksums_url"]) as response:
        text = response.read(64 * 1024).decode("ascii", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == release["name"]:
            return parts[0].lower()
    raise UpdateError("The release has no checksum for its installer, so it was not downloaded.")


def _download(release: dict) -> str:
    folder = updates_dir()
    target = folder / release["name"]
    partial = target.with_name(target.name + ".part")
    expected = _expected_sha256(release)
    digest = hashlib.sha256()
    try:
        with _open(release["url"], timeout=120) as response, partial.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            reported = -1
            while chunk := response.read(CHUNK_BYTES):
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                percent = received * 100 // total if total else 0
                if percent >= reported + 5:
                    reported = percent
                    _set_status(
                        "running",
                        f"Downloading version {release['version']}: {percent}%.",
                        version=release["version"],
                    )
        if digest.hexdigest() != expected:
            raise UpdateError("The download was damaged (checksum mismatch). Please try again.")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    for old in folder.glob("SehatDesk-Setup-*.exe"):
        if old != target:
            old.unlink(missing_ok=True)
    return f"Version {release['version']} is downloaded and verified."


def start_download(release: dict) -> None:
    if not _lock.acquire(blocking=False):
        raise JobBusy("A download is already running.")
    _set_status("running", f"Downloading version {release['version']}.", version=release["version"])

    def runner():
        extra = {"version": release["version"], "name": release["name"]}
        try:
            _set_status("done", _download(release), **extra)
        except UpdateError as err:
            _set_status("failed", str(err), **extra)
        except OSError as err:
            logger.warning("Update download failed: %s", err)
            _set_status("failed", "The download failed. Please try again.", **extra)
        except Exception:
            logger.exception("Update download failed")
            _set_status("failed", "The download failed. See the server log.", **extra)
        finally:
            _lock.release()

    threading.Thread(target=runner, daemon=True).start()


def launch_installer(path: Path) -> None:
    """Open the installer on this computer. Windows shows its administrator prompt there."""
    if not can_install_here():
        raise UpdateError("Installing from here works only on an installed Windows copy.")
    os.startfile(path)  # noqa: S606  # only exists on Windows
