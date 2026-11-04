from django import template
from django.urls import reverse
from django.utils.html import format_html

from apps.flow.services import pop_pending_print

register = template.Library()


@register.simple_tag(takes_context=True)
def pending_print(context):
    """A prescription waiting to print on this PC (flow.services.print_at_doctor/_pharmacy):
    a hidden frame loads it with ?print=1, which prints to this PC's printer."""
    request = context.get("request")
    pk = pop_pending_print(request) if request is not None else None
    if not pk:
        return ""
    url = reverse("prescriptions:print", args=[pk])
    return format_html(
        '<iframe title="Prescription" src="{}?print=1" class="absolute h-0 w-0 border-0" '
        "data-pending-print></iframe>",
        url,
    )
