"""Vitals display helpers. Load with {% load vitals %}."""

from django import template
from django.utils import timezone
from django.utils.html import format_html, format_html_join

register = template.Library()

LEVEL_CHIP = {"critical": "chip-danger", "high": "chip-warning", "low": "chip-warning"}


@register.simple_tag
def vital_chip(row):
    """A reading with its flag word, e.g. "BP 182/96 mmHg · Critical". Normal shows no word."""
    css = LEVEL_CHIP.get(row["level"], "chip-neutral")
    if row["level"] == "normal":
        return format_html('<span class="chip {}">{} {}</span>', css, row["label"], row["value"])
    return format_html(
        '<span class="chip {}">{} {} · {}</span>',
        css,
        row["label"],
        row["value"],
        row["level_label"],
    )


@register.simple_tag
def sparkline(points, width=220, height=48):
    """Inline SVG trend line. `points` = [(when, value, level), ...] oldest first."""
    if not points:
        return ""
    values = [p[1] for p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pad = 6
    step = (width - 2 * pad) / max(len(points) - 1, 1)
    coords = [
        (pad + i * step, height - pad - (v - lo) / span * (height - 2 * pad))
        for i, v in enumerate(values)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    dots = format_html_join(
        "",
        '<circle cx="{}" cy="{}" r="3" class="{}"><title>{}: {}</title></circle>',
        (
            (f"{x:.1f}", f"{y:.1f}", "fill-teal" if p[2] == "normal" else "fill-red",
             timezone.localtime(p[0]).strftime("%d %b %H:%M"), p[1])
            for (x, y), p in zip(coords, points, strict=True)
        ),
    )  # fmt: skip
    return format_html(
        '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" aria-label="Trend of '
        '{n} readings from {lo} to {hi}" class="overflow-visible">'
        '<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="2" '
        'class="text-teal" />{dots}</svg>',
        w=width, h=height, n=len(points), lo=lo, hi=hi, line=line, dots=dots,
    )  # fmt: skip
