"""Talks to the SehatDesk server: connects itself once, then collects and prints this PC's jobs."""

import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import printers, render

log = logging.getLogger("sehatdesk.print")
DEFAULT_SERVER = "http://sehatdesk.local"
HELLO_EVERY = 60
JOIN_EVERY = 10
POLL_EVERY = 1.5


class NotConnected(Exception):
    pass


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ["LOCALAPPDATA"]) / "SehatDeskPrint"
    else:
        base = Path.home() / "Library" / "Application Support" / "SehatDeskPrint"
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    path = config_path()
    path.write_text(json.dumps(config))
    path.chmod(0o600)


def _call(server: str, route: str, *, token: str = "", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{server.rstrip('/')}{route}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise NotConnected from err
        raise


def connect(server: str) -> dict:
    """Ask to join. NotConnected until an admin allows new PCs in Settings → Printers."""
    answer = _call(server, "/print/agent/join/", body={"name": socket.gethostname()})
    config = {"server": server, "token": answer["token"], "name": answer["name"]}
    save_config(config)
    return config


def hello(config: dict, available: list[str], sizes: dict | None = None) -> None:
    body = {"printers": available}
    if sizes is not None:
        body["sizes"] = sizes
    _call(config["server"], "/print/agent/hello/", token=config["token"], body=body)


def run_one(config: dict, send=printers.send) -> bool:
    """Print the next job, if any. Reports the outcome; only the error's kind leaves this PC."""
    job = _call(config["server"], "/print/agent/next/", token=config["token"])["job"]
    if job is None:
        return False
    ok, error = True, ""
    try:
        render.run_job(
            job["paper"], job["html"], lambda kind, path: send(job["printer"], kind, path)
        )
    except Exception as err:  # any failure must reach the person waiting at the Print button
        ok = False
        error = str(err) if isinstance(err, RuntimeError) else "Could not print on this printer."
        log.warning("Print job %s failed: %s", job["id"], type(err).__name__)
    _call(
        config["server"],
        f"/print/agent/jobs/{job['id']}/",
        token=config["token"],
        body={"ok": ok, "error": error},
    )
    return True


def loop(config: dict, on_status, should_stop) -> None:
    """Runs for as long as the helper does: joining, printing, and joining again if removed."""
    last_hello = 0.0
    while not should_stop():
        try:
            if not config.get("token"):
                try:
                    config.update(connect(config["server"]))
                except NotConnected:
                    on_status("Waiting. In SehatDesk, open Settings → Printers and allow new PCs.")
                    time.sleep(JOIN_EVERY)
                    continue
                last_hello = 0.0
            if time.monotonic() - last_hello > HELLO_EVERY:
                available = printers.list_printers()
                hello(config, available, printers.sizes_of(available))
                last_hello = time.monotonic()
                on_status(
                    f"Connected as {config.get('name', '')}. {len(available)} printers found."
                )
            if run_one(config):
                continue
        except NotConnected:
            config.pop("token", None)
            save_config(config)
            on_status("This PC was removed from SehatDesk.")
        except (OSError, ValueError):
            on_status(f"Cannot reach SehatDesk at {config.get('server')}. Trying again...")
            last_hello = 0.0
        time.sleep(POLL_EVERY)
