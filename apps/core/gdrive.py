"""Google Drive backups: sign in once, then a backup is uploaded at fixed times every day.

Each run writes a normal encrypted backup (apps/core/backup.py) and uploads it to the
"SehatDesk Backups" folder of the connected Google account. The worker thread started by serve.py
calls `run_if_due` every minute. A time missed because the PC was off runs when it next starts,
and a failed upload (no internet) is retried every RETRY_MINUTES until it succeeds.

Sign-in uses the Google desktop client in g1.json.enc, whose redirect goes to localhost, so the
Google page opens in a browser on the server computer. Login and status live in the data folder,
not the database: a restore must not overwrite which account the backups go to.
"""

import json
import logging
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.core import backup

logger = logging.getLogger(__name__)

FOLDER_NAME = "SehatDesk Backups"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
LOGIN_TIMEOUT_SECONDS = 120
LOGIN_PORTS = (8080, 8090)
RETRY_MINUTES = 15
SESSION_FILE = "google_drive_session.enc"
STATE_FILE = "google_drive_state.json"

_lock = threading.Lock()  # one sign-in or upload at a time
_busy = ""  # "connecting" | "uploading", shown on the Backup page


class GoogleDriveError(Exception):
    pass


# --- Stored login and status --------------------------------------------------------------


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(settings.SEHATDESK_GDRIVE_CREDENTIALS_KEY.encode())


def _session_path() -> Path:
    return Path(settings.DATA_DIR) / SESSION_FILE


def _state_path() -> Path:
    return Path(settings.DATA_DIR) / STATE_FILE


def _load_client() -> dict:
    from cryptography.fernet import InvalidToken

    try:
        payload = json.loads(
            _fernet().decrypt(Path(settings.SEHATDESK_GDRIVE_CLIENT_FILE).read_bytes())
        )
        client = payload.get("installed") or payload["web"]
        return {f: client[f] for f in ("client_id", "client_secret", "auth_uri", "token_uri")}
    except (OSError, ValueError, KeyError, InvalidToken) as err:
        raise GoogleDriveError("Google Drive sign-in is not set up in this build.") from err


def _load_session() -> dict | None:
    try:
        session = json.loads(_fernet().decrypt(_session_path().read_bytes()))
    except FileNotFoundError:
        return None
    except Exception:  # unreadable login: treated as not connected, staff sign in again
        logger.warning("saved Google Drive login could not be read")
        return None
    return session if isinstance(session.get("credentials"), str) else None


def _save_session(credentials_json: str, account: dict) -> None:
    payload = json.dumps({"credentials": credentials_json, "account": account}).encode()
    path = _session_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(_fernet().encrypt(payload))
    tmp.replace(path)


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _update_state(**changes) -> None:
    state = {**_load_state(), **changes}
    path = _state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def _when(state: dict, key: str) -> datetime | None:
    raw = state.get(key)
    return datetime.fromisoformat(raw) if raw else None


# --- Schedule -----------------------------------------------------------------------------


def daily_times() -> list[tuple[int, int]]:
    """SEHATDESK_GDRIVE_TIMES (default 15:00, 22:00) as sorted (hour, minute) pairs."""
    return sorted(
        (int(t.split(":")[0]), int(t.split(":")[1])) for t in settings.SEHATDESK_GDRIVE_TIMES
    )


def _at(day: datetime, hour: int, minute: int) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def last_slot(now: datetime) -> datetime | None:
    """The most recent scheduled time at or before `now`."""
    slots = [_at(now - timedelta(days=d), h, m) for d in (0, 1) for h, m in daily_times()]
    return max((s for s in slots if s <= now), default=None)


def next_slot(now: datetime) -> datetime | None:
    slots = [_at(now + timedelta(days=d), h, m) for d in (0, 1) for h, m in daily_times()]
    return min((s for s in slots if s > now), default=None)


def backup_due(now: datetime | None = None) -> bool:
    now = now or timezone.localtime()
    slot = last_slot(now)
    if slot is None or _load_session() is None:
        return False
    state = _load_state()
    # Only times after sign-in count; before it there was nowhere to upload to.
    for key in ("last_success", "connected_at"):
        done = _when(state, key)
        if done and done >= slot:
            return False
    attempt = _when(state, "last_attempt")
    return not (attempt and now - attempt < timedelta(minutes=RETRY_MINUTES))


# --- Google calls -------------------------------------------------------------------------


def _build_auth(credentials_json: str | None = None):
    from pydrive2.auth import GoogleAuth

    client = _load_client()
    store = {"credentials": credentials_json} if credentials_json else {}
    auth = GoogleAuth(
        settings={
            "client_config_backend": "settings",
            "client_config": {
                **client,
                "redirect_uri": "http://localhost:8080/",
                "revoke_uri": None,
            },
            "save_credentials": True,
            "save_credentials_backend": "dictionary",
            "save_credentials_dict": store,
            "save_credentials_key": "credentials",
            "get_refresh_token": True,
            "oauth_scope": [DRIVE_SCOPE],
        },
        http_timeout=LOGIN_TIMEOUT_SECONDS,
    )
    return auth


def _authorize_in_browser(auth) -> None:
    """Open Google's page on this computer and wait for the redirect back to localhost."""
    from oauth2client.tools import ClientRedirectHandler, ClientRedirectServer

    server, port = None, None
    for candidate in LOGIN_PORTS:
        try:
            server, port = (
                ClientRedirectServer(("127.0.0.1", candidate), ClientRedirectHandler),
                candidate,
            )
            break
        except OSError:
            continue
    if server is None:
        raise GoogleDriveError(
            "Local ports 8080 and 8090 are busy, so Google sign-in cannot start."
        )
    try:
        auth.GetFlow()
        auth.flow.redirect_uri = f"http://localhost:{port}/"
        if not webbrowser.open(auth.GetAuthUrl(), new=1, autoraise=True):
            raise GoogleDriveError("The browser could not be opened on the server computer.")
        server.timeout = LOGIN_TIMEOUT_SECONDS
        server.handle_request()
        params = getattr(server, "query_params", {})
        if not params.get("code"):
            raise GoogleDriveError("Google sign-in was cancelled or timed out.")
        auth.Auth(params["code"])
    finally:
        server.server_close()


def _account(drive) -> dict:
    user = drive.GetAbout().get("user") or {}
    return {"name": user.get("displayName"), "email": user.get("emailAddress")}


def _authorized_drive():
    """Returns the Drive client, refreshing and re-saving the stored login."""
    from pydrive2.drive import GoogleDrive

    session = _load_session()
    if session is None:
        raise GoogleDriveError("Connect a Google account first.")
    try:
        auth = _build_auth(session["credentials"])
        auth.LoadCredentials()
        if auth.access_token_expired:
            auth.Refresh()
        auth.Authorize()
        _save_session(auth.credentials.to_json(), session.get("account") or {})
    except Exception as err:
        raise GoogleDriveError(
            "The Google login could not be refreshed. Check the internet, or disconnect and "
            "sign in again."
        ) from err
    return GoogleDrive(auth)


def _folder_id(drive) -> str:
    query = (
        f"title='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    found = drive.ListFile({"q": query}).GetList()
    if found:
        return found[0]["id"]
    folder = drive.CreateFile(
        {"title": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}
    )
    folder.Upload()
    return folder["id"]


def _upload(drive, folder_id: str, path: Path) -> None:
    item = drive.CreateFile(
        {
            "title": path.name,
            "mimeType": "application/octet-stream",
            "parents": [{"id": folder_id}],
        }
    )
    item.SetContentFile(str(path))
    try:
        item.Upload()
    finally:
        # pydrive2 leaves the file open; Windows could then not prune this backup later.
        if getattr(item, "content", None):
            item.content.close()


def _prune_remote(drive, folder_id: str) -> None:
    """Move the oldest uploads beyond SEHATDESK_GDRIVE_KEEP to Drive's trash (recoverable)."""
    keep = settings.SEHATDESK_GDRIVE_KEEP
    if keep <= 0:
        return
    query = f"'{folder_id}' in parents and trashed=false"
    files = drive.ListFile({"q": query, "orderBy": "createdDate desc"}).GetList()
    uploads = [f for f in files if f.get("title", "").startswith(backup.NAME_PREFIX)]
    for old in uploads[keep:]:
        old.Trash()


# --- What the views, the worker and the command call ---------------------------------------


def connect() -> None:
    """Sign in with Google. Blocks until approved or timed out."""
    from pydrive2.drive import GoogleDrive

    global _busy
    if not _lock.acquire(blocking=False):
        raise GoogleDriveError("Another Google Drive task is running.")
    _busy = "connecting"
    try:
        auth = _build_auth()
        _authorize_in_browser(auth)
        account = _account(GoogleDrive(auth))
        _save_session(auth.credentials.to_json(), account)
        _update_state(
            connected_at=timezone.localtime().isoformat(), last_error="", last_attempt=None
        )
        logger.warning("google drive connected")
    except GoogleDriveError as err:
        _update_state(last_error=str(err))
        raise
    except Exception as err:
        logger.exception("google drive sign-in failed")
        _update_state(last_error="Google sign-in did not complete. Please try again.")
        raise GoogleDriveError("Google sign-in did not complete. Please try again.") from err
    finally:
        _busy = ""
        _lock.release()


def disconnect() -> None:
    """Forget the login. Backups already in Drive stay there."""
    with _lock:
        _session_path().unlink(missing_ok=True)
        _update_state(last_error="", connected_at=None, last_attempt=None)


def run_backup() -> str:
    """Write an encrypted backup and upload it. Returns the file name."""
    global _busy
    if not _lock.acquire(blocking=False):
        raise GoogleDriveError("Another Google Drive task is running.")
    _busy = "uploading"
    _update_state(last_attempt=timezone.localtime().isoformat())
    try:
        drive = _authorized_drive()
        info = backup.create_backup(key=backup.shared_or_configured_key())
        folder_id = _folder_id(drive)
        _upload(drive, folder_id, info.path)
        _prune_remote(drive, folder_id)
    except GoogleDriveError as err:
        _update_state(last_error=str(err))
        raise
    except Exception as err:
        logger.exception("google drive backup failed")
        message = "The backup could not be uploaded. It will be tried again."
        _update_state(last_error=message)
        raise GoogleDriveError(message) from err
    else:
        _update_state(
            last_success=timezone.localtime().isoformat(), last_file=info.name, last_error=""
        )
        logger.warning("google drive backup uploaded name=%s", info.name)
        return info.name
    finally:
        _busy = ""
        _lock.release()


def list_backups() -> list[dict]:
    """Backups in the app's Drive folder, newest first, for the restore list."""
    try:
        drive = _authorized_drive()
        query = f"'{_folder_id(drive)}' in parents and trashed=false"
        files = drive.ListFile({"q": query, "orderBy": "createdDate desc"}).GetList()
    except GoogleDriveError:
        raise
    except Exception as err:
        logger.exception("google drive list failed")
        raise GoogleDriveError("Google Drive backups could not be loaded.") from err
    return [
        {
            "id": f["id"],
            "name": f["title"],
            "created": f.get("createdDate", "")[:16].replace("T", " "),
            "size": int(f.get("fileSize") or 0),
        }
        for f in files
        if f.get("title", "").startswith(backup.NAME_PREFIX)
    ]


def download_backup(file_id: str, folder: Path) -> Path:
    """Download one backup, only if it is in the app's Drive folder."""
    try:
        drive = _authorized_drive()
        item = drive.CreateFile({"id": file_id})
        item.FetchMetadata(fields="id,title,parents")
        parents = {p.get("id") for p in item.get("parents") or []}
        name = Path(item.get("title") or "").name
        if _folder_id(drive) not in parents or not name.startswith(backup.NAME_PREFIX):
            raise GoogleDriveError("That file is not a SehatDesk backup.")
        target = Path(folder) / name
        item.GetContentFile(str(target))
    except GoogleDriveError:
        raise
    except Exception as err:
        logger.exception("google drive download failed")
        raise GoogleDriveError("The backup could not be downloaded.") from err
    return target


def _in_background(task) -> None:
    def runner():
        try:
            task()
        except GoogleDriveError:
            pass  # already saved to the status the Backup page shows
        finally:
            connection.close()

    threading.Thread(target=runner, daemon=True).start()


def start_connect() -> None:
    _in_background(connect)


def start_backup() -> None:
    _in_background(run_backup)


def run_if_due() -> bool:
    if not backup_due():
        return False
    try:
        run_backup()
    except GoogleDriveError:
        pass  # saved to the status; retried after RETRY_MINUTES
    return True


def worker_loop() -> None:
    """Body of the background thread started by serve.py."""
    while True:
        try:
            run_if_due()
        except Exception:  # keep the worker alive; the Backup page shows the last good upload
            logger.exception("google drive worker error")
        finally:
            connection.close()
        time.sleep(60)


def status() -> dict:
    """Everything the Backup page shows."""
    try:
        _load_client()
        configured = True
    except GoogleDriveError:
        configured = False
    session = _load_session() if configured else None
    state = _load_state()
    return {
        "configured": configured,
        "connected": session is not None,
        "account": (session or {}).get("account") or {},
        "folder": FOLDER_NAME,
        "busy": _busy,
        "times": [f"{h:02d}:{m:02d}" for h, m in daily_times()],
        "next_run": next_slot(timezone.localtime()) if session else None,
        "last_success": _when(state, "last_success"),
        "last_file": state.get("last_file", ""),
        "last_error": state.get("last_error", ""),
    }
