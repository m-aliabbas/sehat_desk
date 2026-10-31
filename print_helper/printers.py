"""This PC's printers: list them, send printer commands, print a PDF at its true size."""

import re
import subprocess
import sys
from pathlib import Path


def list_printers() -> list[str]:
    if sys.platform == "win32":
        import win32print

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return sorted(p[2] for p in win32print.EnumPrinters(flags))
    out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, check=False).stdout
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def _windows_sizes(printer: str) -> list[list[int]]:
    import win32con
    import win32print

    handle = win32print.OpenPrinter(printer)
    try:
        port = win32print.GetPrinter(handle, 2)["pPortName"]
    finally:
        win32print.ClosePrinter(handle)
    found = win32print.DeviceCapabilities(printer, port, win32con.DC_PAPERSIZE)
    # Windows reports tenths of a millimetre.
    return [[round(size["x"] / 10), round(size["y"] / 10)] for size in found]


NAMED_SIZES = {"a4": [210, 297], "a5": [148, 210], "letter": [216, 279], "legal": [216, 356]}


def parse_cups_sizes(options: str) -> list[list[int]]:
    """The PageSize line of `lpoptions -l`. Custom sizes are like w226h3276, in points."""
    line = next((ln for ln in options.splitlines() if ln.startswith("PageSize")), "")
    sizes = []
    for word in line.partition(":")[2].split():
        word = word.lstrip("*").lower()
        points = re.fullmatch(r"w(\d+)h(\d+)", word)
        if word in NAMED_SIZES:
            sizes.append(NAMED_SIZES[word])
        elif points:
            sizes.append([round(int(v) * 25.4 / 72) for v in points.groups()])
    return sizes


def paper_sizes(printer: str) -> list[list[int]]:
    if sys.platform == "win32":
        return _windows_sizes(printer)
    out = subprocess.run(
        ["lpoptions", "-p", printer, "-l"], capture_output=True, text=True, check=False
    ).stdout
    return parse_cups_sizes(out)


def sizes_of(available: list[str]) -> dict:
    """A printer that will not answer gets no sizes: the server then goes by its name."""
    sizes = {}
    for printer in available:
        try:
            sizes[printer] = paper_sizes(printer)
        except Exception:  # driver calls fail in many ways; this must never stop printing
            sizes[printer] = []
    return sizes


def _lp(printer: str, path: Path, *options: str) -> None:
    done = subprocess.run(
        ["lp", "-d", printer, *options, str(path)], capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise RuntimeError("The printer refused the job.")


def send_raw(printer: str, path: Path) -> None:
    if sys.platform != "win32":
        _lp(printer, path, "-o", "raw")
        return
    import win32print

    handle = win32print.OpenPrinter(printer)
    try:
        win32print.StartDocPrinter(handle, 1, ("SehatDesk", None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, path.read_bytes())
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    finally:
        win32print.ClosePrinter(handle)


def _print_pdf_windows(printer: str, path: Path) -> None:
    import pypdfium2
    import win32con
    import win32ui
    from PIL import ImageWin

    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
    dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
    sheet_w = dc.GetDeviceCaps(win32con.HORZRES)
    sheet_h = dc.GetDeviceCaps(win32con.VERTRES)
    document = pypdfium2.PdfDocument(str(path))
    dc.StartDoc("SehatDesk")
    try:
        for i in range(len(document)):
            page = document[i].render(scale=dpi_x / 72).to_pil().convert("RGB")
            # A sticker loaded sideways in the driver: turn the page, never shrink it.
            if (page.width > page.height) != (sheet_w > sheet_h):
                page = page.rotate(90, expand=True)
            width = min(page.width, sheet_w)
            height = min(round(page.height * dpi_y / dpi_x), sheet_h)
            dc.StartPage()
            ImageWin.Dib(page).draw(dc.GetHandleOutput(), (0, 0, width, height))
            dc.EndPage()
        dc.EndDoc()
    except Exception:
        dc.AbortDoc()
        raise
    finally:
        document.close()
        dc.DeleteDC()


def print_pdf(printer: str, path: Path) -> None:
    if sys.platform == "win32":
        _print_pdf_windows(printer, path)
    else:
        _lp(printer, path)


def send(printer: str, kind: str, path: Path) -> None:
    if kind == "raw":
        send_raw(printer, path)
    else:
        print_pdf(printer, path)
