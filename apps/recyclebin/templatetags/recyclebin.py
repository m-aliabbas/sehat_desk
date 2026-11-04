from django import template

from apps.recyclebin.registry import KINDS

register = template.Library()


@register.simple_tag
def edit_link(kind_key, obj):
    """{% edit_link "ward" w as url %}: the record's edit page (its own, or the shared one)."""
    kind = KINDS.get(kind_key)
    return kind.edit_link(obj) if kind else None
