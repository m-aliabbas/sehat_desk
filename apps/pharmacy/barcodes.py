"""Barcodes: the code a new product gets when it has none, and Code 128 labels as inline SVG.

Generated codes are valid EAN-13 numbers starting 20, the range GS1 keeps for in-store use, so they
cannot clash with a manufacturer's code. Labels are drawn here because the app runs offline.
"""

from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from apps.core.models import next_number

_PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 "
    "122132 122231 113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 "
    "321221 312212 322112 322211 212123 212321 232121 111323 131123 131321 112313 132113 132311 "
    "211313 231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 231131 213113 "
    "213311 213131 311123 311321 331121 312113 312311 332111 314111 221411 431111 111224 111422 "
    "121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 "
    "241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 "
    "412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 "
    "211214 211232 2331112"
).split()
_CODE_C, _START_B, _START_C, _STOP = 99, 104, 105, 106


def ean13_check_digit(first_twelve: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first_twelve))
    return str((10 - total % 10) % 10)


def new_barcode(organization) -> str:
    from .models import Product

    while True:
        body = f"20{next_number(organization, 'barcode'):010d}"
        code = body + ean13_check_digit(body)
        if not Product.objects.filter(organization=organization, barcode=code).exists():
            return code


def code128_widths(value: str) -> str:
    """Bar and space widths for a value, alternating and starting with a bar."""
    if not value or any(not 32 <= ord(c) <= 126 for c in value):
        raise ValueError("Code 128 labels hold plain letters, digits and symbols.")
    if value.isdigit() and len(value) >= 4:
        # Two digits per symbol: bars about 40% wider, which a 1.5 inch label needs to scan.
        lead = value[: len(value) % 2]
        codes = [_START_B, ord(lead) - 32, _CODE_C] if lead else [_START_C]
        rest = value[len(lead) :]
        codes += [int(rest[i : i + 2]) for i in range(0, len(rest), 2)]
    else:
        codes = [_START_B, *(ord(c) - 32 for c in value)]
    check = (codes[0] + sum(i * c for i, c in enumerate(codes[1:], 1))) % 103
    return "".join(_PATTERNS[c] for c in [*codes, check, _STOP])


def code128_svg(value: str, *, height: int = 40) -> SafeString:
    """An SVG of the barcode, sized in modules so it prints sharply at any width."""
    widths = code128_widths(value)
    total = sum(int(w) for w in widths)
    x, bars = 10, []  # 10 modules of quiet space each side, which scanners need
    for i, width in enumerate(widths):
        if i % 2 == 0:
            bars.append((x, width))
        x += int(width)
    rects = format_html_join(
        "", '<rect x="{}" y="0" width="{}" height="{}"/>', ((b, w, height) for b, w in bars)
    )
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {} {}" class="h-full w-full" '
        'preserveAspectRatio="none" shape-rendering="crispEdges" role="img" '
        'aria-label="Barcode {}" fill="#000">{}</svg>',
        total + 20,
        height,
        value,
        rects,
    )
