"""Helpers shared by the launcher window and the server process. No Django imports here."""

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOSTNAME = "sehatdesk.local"
PORTS = (80, 8000, 8001)  # 80 gives the plain http://sehatdesk.local address
STATUS_FILE = "launcher-status.txt"


def resource_dir() -> Path:
    """Where bundled files live: the PyInstaller folder, or the repository in development."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """Patient data lives here, outside the install folder, so updates never touch it."""
    override = os.environ.get("SEHATDESK_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "SehatDesk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SehatDesk"
    return Path.home() / ".local" / "share" / "SehatDesk"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lan_ips() -> list[str]:
    """This computer's IPv4 LAN addresses, the one used for outbound traffic first."""
    import ifaddr

    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # sends nothing; picks the route
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str):
                found.append(ip.ip)
    usable = [ip for ip in found if not ip.startswith(("127.", "169.254.", "0."))]
    return list(dict.fromkeys(usable))


def base_url(host: str, port: int) -> str:
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def configure_env(port: int) -> None:
    """Settings for an installed copy. Values in the optional sehatdesk.env file win."""
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    env_file = data / "sehatdesk.env"
    if env_file.exists():
        os.environ.setdefault("SEHATDESK_ENV_FILE", str(env_file))
    machine = socket.gethostname().split(".")[0]
    hosts = [HOSTNAME, "localhost", "127.0.0.1", machine, f"{machine}.local", *lan_ips()]
    defaults = {
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "SEHATDESK_ENV": "production",
        "DJANGO_DEBUG": "false",
        "SEHATDESK_DATA_DIR": str(data),
        "SEHATDESK_PORT": str(port),
        "SEHATDESK_ALLOWED_HOSTS": ",".join(hosts),
        "SEHATDESK_CSRF_TRUSTED_ORIGINS": ",".join(base_url(h, port) for h in hosts),
    }
    gtk = resource_dir() / "gtk"
    if gtk.is_dir():
        defaults["WEASYPRINT_DLL_DIRECTORIES"] = str(gtk)
        defaults["FONTCONFIG_PATH"] = str(gtk)
        defaults["FONTCONFIG_FILE"] = "fonts.conf"
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


def write_status(text: str) -> None:
    """The server process reports its start-up stage to the window through this file."""
    path = log_dir() / STATUS_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_status() -> str:
    try:
        return (log_dir() / STATUS_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def is_healthy(port: int, timeout: float = 2.0) -> bool:
    """True when a SehatDesk server answers on this computer and its database works."""
    url = f"http://127.0.0.1:{port}/health/ready/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return json.load(response).get("database") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Same rule as Waitress: without SO_REUSEADDR on Windows, a busy port fails here.
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def running_port() -> int | None:
    return next((p for p in PORTS if is_healthy(p, timeout=1.0)), None)


def choose_port() -> int | None:
    return next((p for p in PORTS if port_free(p)), None)


def version() -> str:
    for path in (resource_dir() / "VERSION", resource_dir() / "packaging" / "VERSION"):
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return "dev"
