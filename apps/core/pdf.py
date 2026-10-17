"""Load WeasyPrint for PDF exports.

    from apps.core.pdf import weasyprint_html

    HTML = weasyprint_html()   # None when the native libraries cannot be loaded
    if HTML is None:
        ...                    # fall back to the print page

On macOS, Pango/GTK from Homebrew or MacPorts live outside the default library search path,
so a conda or python.org Python cannot find them even when `brew install pango` is done. We
add those folders to DYLD_FALLBACK_LIBRARY_PATH before the first import.
"""

import os
import sys

_MAC_LIBRARY_DIRS = ("/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib")


def _add_mac_library_dirs() -> None:
    current = [p for p in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p]
    extra = [d for d in _MAC_LIBRARY_DIRS if d not in current and os.path.isdir(d)]
    if extra:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(current + extra)


def weasyprint_html():
    """Return WeasyPrint's `HTML` class, or None if WeasyPrint or its libraries are missing."""
    if sys.platform == "darwin":
        _add_mac_library_dirs()
    try:
        from weasyprint import HTML
    except (ImportError, OSError):
        return None
    return HTML
