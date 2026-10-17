"""Pharmacy display helpers. Load with {% load pharmacy %}."""

from django import template

register = template.Library()


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
