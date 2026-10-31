"""Pharmacy display helpers. Load with {% load pharmacy %}."""

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def packing_options(context):
    """{% packing_options as options %}: UoM phrases to suggest in this hospital."""
    from apps.pharmacy.catalog import packing_suggestions

    return packing_suggestions(context["request"].organization)


@register.filter
def split(text):
    """{% for n in "tablet capsule"|split %}: a short fixed list written in a template."""
    return text.split()


@register.filter
def abs_value(number):
    return abs(number or 0)


@register.filter
def in_packs(quantity, product):
    """{{ qty|in_packs:product }} -> "25 tablets (2 strips + 5)"."""
    return product.describe_quantity(quantity or 0)


@register.filter
def as_packs(quantity, product):
    """{{ qty|as_packs:product }} -> "2 strips + 5" ("" under one pack)."""
    return product.pack_breakdown(quantity or 0)


@register.filter
def unit_label(product, count=1):
    """{{ product|unit_label:qty }} -> "tablet" / "tablets"."""
    return product.unit_label(count)


@register.filter
def pack_label(product, count=1):
    return product.pack_label(count)
