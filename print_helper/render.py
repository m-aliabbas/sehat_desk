"""Page to paper: html to PDF with the PC's own Edge or Chrome, and PDF to thermal printer commands.
Job pages hold patient data: temporary files go as soon as a job ends and nothing is logged."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DOTS_WIDE = 576  # 72 mm printable at 203 dpi
DOTS_PER_INCH = 203
BLACK_BELOW = 150
_BAND = 255  # lines per image command, the size every ESC/POS printer accepts
_NO_SCRIPTS = "<meta http-equiv='Content-Security-Policy' content=\"script-src 'none'\">"
_ROLL_PAGE = "<style>@page { size: 80mm 1200mm; margin: 0; }</style>"


def find_browser() -> str | None:
    if sys.platform == "win32":
        roots = [
            os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
        ]
        tails = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        found = [Path(r) / t for r in roots if r for t in tails]
    else:
        found = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    for path in found:
        if path.exists():
            return str(path)
    return shutil.which("chromium") or shutil.which("google-chrome")


def html_to_pdf(html: str, folder: Path, *, roll: bool = False) -> Path:
    browser = find_browser()
    if not browser:
        raise RuntimeError("Edge or Chrome is not installed on this PC.")
    if roll:
        html = html.replace("</head>", _ROLL_PAGE + "</head>", 1)
    html = html.replace("<head>", "<head>" + _NO_SCRIPTS, 1)
    page = folder / "job.html"
    page.write_text(html, encoding="utf-8")
    pdf = folder / "job.pdf"
    # No scripts and no network: the page was made complete by the browser that sent it.
    subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-pdf-header-footer",
            "--host-resolver-rules=MAP * ~NOTFOUND",
            f"--user-data-dir={folder / 'profile'}",
            f"--print-to-pdf={pdf}",
            page.as_uri(),
        ],
        check=False,
        timeout=60,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not pdf.exists() or not pdf.stat().st_size:
        raise RuntimeError("The page could not be prepared for printing.")
    return pdf


def roll_image(pdf: Path):
    """All pages as one strip, exactly as wide as the print head, ending where the content ends."""
    import pypdfium2
    from PIL import Image, ImageChops

    document = pypdfium2.PdfDocument(str(pdf))
    try:
        pages = [
            document[i].render(scale=DOTS_PER_INCH / 72).to_pil().convert("L")
            for i in range(len(document))
        ]
    finally:
        document.close()
    strips = []
    for page in pages:
        left = max((page.width - DOTS_WIDE) // 2, 0)
        crop = page.crop((left, 0, left + DOTS_WIDE, page.height))
        box = ImageChops.invert(crop).point(lambda v: 255 if v > 40 else 0).getbbox()
        if box:
            strips.append(crop.crop((0, 0, DOTS_WIDE, box[3])))
    if not strips:
        raise RuntimeError("The page is blank.")
    strip = Image.new("L", (DOTS_WIDE, sum(s.height for s in strips)), 255)
    y = 0
    for part in strips:
        strip.paste(part, (0, y))
        y += part.height
    return strip


def escpos(image) -> bytes:
    """Black dots only, no grey dithering, then feed past the cutter and cut."""
    dots = image.convert("L").point(lambda v: 255 if v < BLACK_BELOW else 0).convert("1")
    row_bytes = (dots.width + 7) // 8
    out = bytearray(b"\x1b@")  # reset
    for top in range(0, dots.height, _BAND):
        band = dots.crop((0, top, dots.width, min(top + _BAND, dots.height)))
        out += b"\x1dv0\x00"
        out += bytes((row_bytes & 255, row_bytes >> 8, band.height & 255, band.height >> 8))
        out += band.tobytes()
    out += b"\x1dV\x42\x40"  # feed to the cutter, partial cut
    return bytes(out)


def prepare(paper: str, html: str, folder: Path) -> tuple[str, Path]:
    """("raw", commands file) for the thermal roll, ("pdf", file) for every other printer."""
    if paper == "thermal":
        pdf = html_to_pdf(html, folder, roll=True)
        raw = folder / "job.bin"
        raw.write_bytes(escpos(roll_image(pdf)))
        return "raw", raw
    return "pdf", html_to_pdf(html, folder)


def run_job(paper: str, html: str, send) -> None:
    folder = Path(tempfile.mkdtemp(prefix="sehatdesk-print-"))
    try:
        kind, path = prepare(paper, html, folder)
        send(kind, path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
