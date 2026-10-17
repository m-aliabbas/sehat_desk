"""Notification center (design screen 38) and the message templates / delivery log."""

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.forms import StyledFormMixin

from . import channels, services
from .models import Channel, Event, MessageDelivery, MessageTemplate, Notification


def _mine(request):
    return services.for_user(request.user, request.organization)


@require_GET
def center(request):
    show = request.GET.get("show", "all")
    event = request.GET.get("event", "")
    qs = _mine(request)
    if show == "unread":
        qs = qs.filter(read_at__isnull=True)
    if event in Event.values:
        qs = qs.filter(event=event)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    for n in page.object_list:
        n.icon = services.EVENT_ICONS.get(n.event, "bell")
    return render(
        request,
        "notifications/center.html",
        {
            "page_obj": page,
            "show": show,
            "event": event,
            "events": Event.choices,
            "unread": _mine(request).filter(read_at__isnull=True).count(),
        },
    )


def _safe(url):
    return url if url.startswith("/") and not url.startswith("//") else ""


@require_POST
def open_notification(request, pk):
    """Mark read and go to the linked record (its own view checks permissions)."""
    note = get_object_or_404(Notification, pk=pk, recipient=request.user)
    services.mark_read(note)
    return redirect(_safe(note.url) or "notifications:center")


@require_POST
def mark_read(request, pk):
    note = get_object_or_404(Notification, pk=pk, recipient=request.user)
    services.mark_read(note)
    return redirect(_safe(request.POST.get("next", "")) or "notifications:center")


@require_POST
def mark_all_read(request):
    n = services.mark_all_read(request.user, request.organization)
    messages.success(request, f"{n} notification{'s' if n != 1 else ''} marked as read.")
    return redirect(_safe(request.POST.get("next", "")) or "notifications:center")


# --- External message templates and delivery log (admin) ----------------------------------------


class TemplateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = ["event", "channel", "subject", "body", "is_active"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3})}
        labels = {"is_active": "In use"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].choices = [
            (k, v) for k, v in Event.choices if k in channels.PLACEHOLDERS
        ]


@permission_required("admin.organization.manage")
def message_settings(request):
    org = request.organization
    edit = None
    if request.GET.get("edit", "").isdigit():
        edit = get_object_or_404(MessageTemplate, pk=request.GET["edit"], organization=org)
    form = TemplateForm(request.POST or None, instance=edit)
    if request.method == "POST" and form.is_valid():
        tpl = form.save(commit=False)
        tpl.organization = org
        clash = MessageTemplate.objects.filter(
            organization=org, event=tpl.event, channel=tpl.channel
        ).exclude(pk=tpl.pk)
        if clash.exists():
            form.add_error(None, "A template for this event and channel already exists. Edit it.")
        else:
            tpl.save()
            audit.record(request, "notifications.template.save", tpl, summary=str(tpl))
            messages.success(request, "Message template saved.")
            return redirect("notifications:messages")
    return render(
        request,
        "notifications/messages.html",
        {
            "form": form,
            "editing": edit,
            "templates": MessageTemplate.objects.filter(organization=org),
            "deliveries": MessageDelivery.objects.filter(organization=org)[:50],
            "channels": [(c, label, channels.is_configured(c)) for c, label in Channel.choices],
            "placeholders": {Event(k).label: v for k, v in channels.PLACEHOLDERS.items()},
            "settings_tab": "messages",
            "breadcrumbs": [
                {"label": "Settings", "url": reverse("organizations:settings")},
                {"label": "Patient messages"},
            ],
        },
    )
