from django import template

from ..exports import format_cell

register = template.Library()


@register.filter
def cell(row, column):
    """{{ row|cell:column }} -> formatted value of column.key in a row dict."""
    return format_cell(row.get(column.key) if row else None, column.kind)


@register.filter
def fmt(value, kind):
    return format_cell(value, kind)


@register.simple_tag
def bar_width(value, chart):
    """Percentage width of a bar relative to the largest value in the chart."""
    top = max((float(d["value"]) for d in chart), default=0)
    return round(float(value) * 100 / top, 1) if top else 0
