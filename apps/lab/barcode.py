"""Code 39 barcodes as inline SVG, for sample tube labels. No library and no internet needed;
any USB barcode scanner reads Code 39. Accessions use only Code 39 characters (A–Z, 0–9, -)."""

from django.utils.html import format_html, format_html_join

# Each character: 9 elements (bar, space, bar, ...), "1" = wide, "0" = narrow.
_PATTERNS = {
    "0": "000110100", "1": "100100001", "2": "001100001", "3": "101100000",
    "4": "000110001", "5": "100110000", "6": "001110000", "7": "000100101",
    "8": "100100100", "9": "001100100", "A": "100001001", "B": "001001001",
    "C": "101001000", "D": "000011001", "E": "100011000", "F": "001011000",
    "G": "000001101", "H": "100001100", "I": "001001100", "J": "000011100",
    "K": "100000011", "L": "001000011", "M": "101000010", "N": "000010011",
    "O": "100010010", "P": "001010010", "Q": "000000111", "R": "100000110",
    "S": "001000110", "T": "000010110", "U": "110000001", "V": "011000001",
    "W": "111000000", "X": "010010001", "Y": "110010000", "Z": "011010000",
    "-": "010000101", ".": "110000100", " ": "011000100", "*": "010010100",
}  # fmt: skip
NARROW, WIDE = 2, 5


def code39_svg(value: str, height: int = 50):
    text = "*" + value.upper() + "*"
    bars, x = [], 0
    for char in text:
        pattern = _PATTERNS.get(char)
        if pattern is None:
            raise ValueError(f"{char!r} cannot be encoded in Code 39")
        for i, wide in enumerate(pattern):
            width = WIDE if wide == "1" else NARROW
            if i % 2 == 0:
                bars.append((x, width))
            x += width
        x += NARROW  # gap between characters
    rects = format_html_join(
        "", '<rect x="{}" y="0" width="{}" height="{}" />', ((bx, w, height) for bx, w in bars)
    )
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {} {}" height="{}" '
        'role="img" aria-label="Barcode {}" fill="currentColor">{}</svg>',
        x, height, height, value, rects,
    )  # fmt: skip
