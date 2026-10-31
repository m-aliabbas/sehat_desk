"""The server prints 80 mm slips itself: no helper and no print window, on any PC.

The page is drawn by the Edge or Chrome on the server PC, turned into printer commands, and sent to
a network printer's IP or a printer installed or shared on the server PC. The html is patient data:
it is never stored or logged. Other paper sizes are not sent here.
"""

import re
import socket

from django.utils.html import escape

from print_helper import printers, render

from .models import TokenPrinter

COOKIE = "sd_token_printer"
COOKIE_AGE = 60 * 60 * 24 * 365 * 5
RAW_PORT = 9100  # what network receipt printers listen on
_NETWORK = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?$")
# A browser sent this page and the server draws it: it may load nothing from disk or network.
_SEALED = (
    "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
    "style-src 'unsafe-inline'; img-src data:; font-src data:\">"
)


def is_network(address: str) -> bool:
    return bool(_NETWORK.match(address))


def installed_printers() -> list[str]:
    """Printers on the server PC, offered when adding one. Never stops the page."""
    try:
        return printers.list_printers()
    except Exception:  # driver and spooler calls fail in many ways
        return []


def printers_for(organization):
    return TokenPrinter.objects.filter(organization=organization)


def chosen(request) -> TokenPrinter | None:
    """This PC's choice, kept in its browser. With one printer there is nothing to choose."""
    options = list(printers_for(request.organization))
    wanted = request.COOKIES.get(COOKIE, "")
    for printer in options:
        if str(printer.pk) == wanted:
            return printer
    return options[0] if len(options) == 1 else None


def _deliver(address: str, path) -> None:
    network = _NETWORK.match(address)
    if not network:
        printers.send_raw(address, path)
        return
    target = (network.group(1), int(network.group(2) or RAW_PORT))
    with socket.create_connection(target, timeout=5) as connection:
        connection.sendall(path.read_bytes())


def send(printer: TokenPrinter, html: str) -> str:
    """Print now. Returns "" or what went wrong, in words for the desk."""
    if "<head>" not in html:
        return "The page could not be prepared."
    sealed = html.replace("<head>", "<head>" + _SEALED, 1)
    try:
        render.run_job("thermal", sealed, lambda _kind, path: _deliver(printer.address, path))
    except OSError:
        return f"{printer} did not answer. Check that it is on and connected."
    except Exception as err:  # printer drivers fail in many ways; the print window takes over
        return str(err)[:200] or "The printer refused the job."
    return ""


def send_test(printer: TokenPrinter) -> str:
    return send(
        printer,
        "<!doctype html><html><head><style>@page { size: 80mm auto; margin: 0 }</style></head>"
        "<body style='font:bold 16px Arial;text-align:center;width:72mm;margin:0 auto'>"
        f"<p>SehatDesk test print</p><p>{escape(printer.name)}</p></body></html>",
    )
