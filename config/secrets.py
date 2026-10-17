"""Secret key file for customer machines (docs/PROJECT_NOTES.md §3 and §15)."""

import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def load_or_create_secret_key(path: Path, *, create: bool) -> str:
    """Read the key file; with create=True make it first (owner read/write only).

    O_EXCL makes the first writer win if two processes start at once; the other reads it."""
    path = Path(path)
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_urlsafe(50))
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as err:
        raise ImproperlyConfigured(f"Cannot read the secret key file {path}: {err}") from err
    if len(key) < 50:
        raise ImproperlyConfigured(f"The secret key in {path} is too short (need 50+ characters).")
    return key
