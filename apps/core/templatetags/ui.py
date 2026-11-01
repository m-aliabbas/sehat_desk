"""Template helpers for the SehatDesk design system. Load with {% load ui %}."""

from decimal import Decimal

from django import template
from django.utils.html import format_html, format_html_join, mark_safe

from apps.core.icons import ICONS

register = template.Library()

# Status value -> chip variant. Modules add their own status words here as they are built.
STATUS_VARIANTS = {
    "completed": "success",
    "active": "success",
    "paid": "success",
    "released": "success",
    "in_progress": "info",
    "in_consultation": "info",
    "scheduled": "info",
    "waiting": "info",
    "called": "warning",
    "checked_in": "warning",
    "in_queue": "warning",
    "with_doctor": "info",
    "confirmed": "info",
    "open": "success",
    "closed": "neutral",
    "skipped": "danger",
    "needs_review": "warning",
    "pending": "warning",
    "draft": "warning",
    "partially_paid": "info",
    "unpaid": "warning",
    "refunded": "neutral",
    "cancelled": "danger",
    "no_show": "danger",
    "allergy": "danger",
    "critical": "danger",
    "inactive": "neutral",
    "archived": "neutral",
    "payment_pending": "warning",
    "sample_pending": "warning",
    "sample_collected": "info",
    "collected": "info",
    "processing": "info",
    "result_entered": "info",
    "verified": "success",
    "rejected": "danger",
    "signed": "success",
    "ordered": "info",
    "finalized": "success",
    "present": "success",
    "late": "warning",
    "half_day": "info",
    "absent": "danger",
    "leave": "info",
    "off": "neutral",
    "received": "success",
    "held": "warning",
    "voided": "neutral",
    "dispensed": "success",
    "partial": "info",
    # Inpatient
    "available": "success",
    "occupied": "info",
    "reserved": "warning",
    "cleaning": "warning",
    "maintenance": "neutral",
    "blocked": "danger",
    "admitted": "info",
    "discharge_planned": "warning",
    "discharged": "neutral",
    "stopped": "neutral",
    "given": "success",
    "missed": "danger",
    "refused": "warning",
    "due": "warning",
    "overdue": "danger",
    "upcoming": "neutral",
    # Notifications / messages
    "sent": "success",
    "delivered": "success",
    "failed": "danger",
    "queued": "info",
}


@register.simple_tag
def icon(name, css_class="h-5 w-5"):
    """Render an inline SVG icon. Unknown names render nothing rather than breaking the page."""
    paths = ICONS.get(name)
    if paths is None:
        return ""
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        'stroke-linejoin="round" class="{}" aria-hidden="true">{}</svg>',
        css_class,
        mark_safe(paths),  # noqa: S308 - static strings from apps/core/icons.py, never user input
    )


_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _words(n: int) -> str:
    """Lakh and crore, as bills are read in Pakistan."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else "")
    for size, name in (
        (10_000_000, "crore"),
        (100_000, "lakh"),
        (1000, "thousand"),
        (100, "hundred"),
    ):
        if n >= size:
            rest = n % size
            return f"{_words(n // size)} {name}" + (f" {_words(rest)}" if rest else "")
    return ""


@register.filter
def in_words(amount) -> str:
    """1250.50 -> "one thousand two hundred fifty and paisa fifty"."""
    total = round(Decimal(str(amount or 0)) * 100)
    whole, paisa = divmod(abs(int(total)), 100)
    return _words(whole) + (f" and paisa {_words(paisa)}" if paisa else "")


@register.simple_tag(takes_context=True)
def past_values(context, source):
    """<datalist> of what this hospital typed before. source: "app.Model.field"."""
    from django.apps import apps

    from apps.core.forms import remember_id
    from apps.masterdata.defaults import suggestions

    label, name = source.rsplit(".", 1)
    rows = (
        apps.get_model(label)
        .objects.filter(organization=context["request"].organization)
        .exclude(**{name: ""})
        .order_by(name)
        .values_list(name, flat=True)
        .distinct()[:200]
    )
    seen = {str(r).strip().lower(): str(r).strip() for r in rows}
    for value in suggestions(source):
        seen.setdefault(value.lower(), value)
    return format_html(
        '<datalist id="{}">{}</datalist>',
        remember_id(source),
        format_html_join("", '<option value="{}"></option>', ((v,) for v in seen.values())),
    )


@register.simple_tag(takes_context=True)
def discount_reason_list(context):
    """<datalist id="discount-reasons">: what was typed before, for any discount reason box."""
    from apps.billing.services import discount_reasons

    return format_html(
        '<datalist id="discount-reasons">{}</datalist>',
        format_html_join(
            "",
            '<option value="{}"></option>',
            ((r,) for r in discount_reasons(context["request"].organization)),
        ),
    )


@register.simple_tag(takes_context=True)
def print_layout(context, document):
    """{% print_layout "invoice" as L %}: Settings → Print layouts for this document."""
    from apps.organizations.models import print_layout_for

    return print_layout_for(context["request"].organization, document)


@register.simple_tag(takes_context=True)
def field_rule(context, key):
    """{% field_rule "purchase.gst" as gst %}: "hidden", "optional" or "required"."""
    from apps.organizations.field_rules import rule

    return rule(context["request"].organization, key)


@register.simple_tag
def status_chip(status, label=None):
    """Chip for a status value: text plus color, so status never relies on color alone."""
    key = str(status).lower().replace(" ", "_").replace("-", "_")
    variant = STATUS_VARIANTS.get(key, "neutral")
    text = label or str(status).replace("_", " ").capitalize()
    return format_html('<span class="chip chip-{}">{}</span>', variant, text)


@register.filter
def initials(user):
    """Two-letter avatar initials for a user, falling back to the username."""
    full_name = (user.get_full_name() or "").strip()
    source = full_name or user.get_username()
    parts = source.split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return source[:2].upper()


@register.filter
def get_item(mapping, key):
    """{{ totals|get_item:key }}: a dict value by a variable key; empty when missing."""
    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""


@register.filter
def sum_values(values):
    """{{ row.values|sum_values }}: sum of an iterable of numbers."""
    return sum(values, 0)
