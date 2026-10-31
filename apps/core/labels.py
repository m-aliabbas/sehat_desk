"""Sticker sizes for label printers. The page must match the sticker exactly: a browser shrinks a
bigger page (A4) to fit, which prints a tiny code in one corner."""

LABEL_PAPERS = {
    "38x25": {"key": "38x25", "name": "1.5 × 1 inch", "width": "38.1mm", "height": "25.4mm"},
    "50x25": {"key": "50x25", "name": "50 × 25 mm", "width": "50mm", "height": "25mm"},
}
DEFAULT_LABEL_PAPER = "38x25"


def label_paper(key: str) -> dict:
    return LABEL_PAPERS.get(key, LABEL_PAPERS[DEFAULT_LABEL_PAPER])
