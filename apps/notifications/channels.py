"""External message channels: SMS, WhatsApp and email (foundation; providers come later).

    send_external(org, Event.LAB_READY, Channel.SMS, to="03001234567",
                  context={"patient_name": ..., "order_number": ...})

1. The hospital's MessageTemplate for (event, channel) is used, or the built-in default.
2. Only the placeholders listed for the event are filled in; others stay as typed. Templates
   are plain text, never Django templates, so a template cannot read other data.
3. A MessageDelivery row records the message and its status. Channels without a provider are
   recorded as "skipped", so nothing is silently lost.

Email works now through Django's mail settings (SEHATDESK_EMAIL_HOST). SMS and WhatsApp need
a provider adapter: subclass ChannelAdapter, implement send(), and point
SEHATDESK_SMS_ADAPTER / SEHATDESK_WHATSAPP_ADAPTER at its dotted path.
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.module_loading import import_string

from .models import Channel, Event, MessageDelivery, MessageTemplate

logger = logging.getLogger(__name__)

PLACEHOLDERS = {
    Event.APPOINTMENT_UPCOMING: ["patient_name", "hospital", "doctor", "date", "time"],
    Event.APPOINTMENT_CANCELLED: ["patient_name", "hospital", "doctor", "date", "time"],
    Event.LAB_READY: ["patient_name", "hospital", "order_number"],
    Event.LAB_CRITICAL: ["hospital", "order_number"],
    Event.GENERAL: ["patient_name", "hospital", "message"],
}

DEFAULTS = {
    Event.APPOINTMENT_UPCOMING: (
        "Appointment reminder",
        "Dear {patient_name}, this is a reminder of your appointment with {doctor} at "
        "{hospital} on {date} at {time}.",
    ),
    Event.APPOINTMENT_CANCELLED: (
        "Appointment cancelled",
        "Dear {patient_name}, your appointment with {doctor} at {hospital} on {date} at "
        "{time} has been cancelled. Please call us to rebook.",
    ),
    Event.LAB_READY: (
        "Your lab report is ready",
        "Dear {patient_name}, your lab report {order_number} is ready at {hospital}.",
    ),
    Event.GENERAL: ("Message from {hospital}", "{message}"),
}


class ChannelNotConfigured(Exception):
    pass


@dataclass
class SendResult:
    provider: str
    message_id: str = ""


class ChannelAdapter:
    """Base class for a provider. send() raises on failure."""

    name = "base"

    def send(self, *, to: str, subject: str, body: str) -> SendResult:
        raise ChannelNotConfigured(f"{self.name} has no provider set up")


class EmailAdapter(ChannelAdapter):
    name = "email"

    def send(self, *, to, subject, body):
        if not getattr(settings, "EMAIL_CONFIGURED", False):
            raise ChannelNotConfigured("email is not set up (SEHATDESK_EMAIL_HOST)")
        send_mail(subject, body, None, [to], fail_silently=False)
        return SendResult(provider="smtp")


class NotConfiguredAdapter(ChannelAdapter):
    def __init__(self, name):
        self.name = name


def adapter_for(channel: str) -> ChannelAdapter:
    if channel == Channel.EMAIL:
        return EmailAdapter()
    path = getattr(settings, f"SEHATDESK_{channel.upper()}_ADAPTER", "")
    if path:
        return import_string(path)()
    return NotConfiguredAdapter(channel)


def is_configured(channel: str) -> bool:
    if channel == Channel.EMAIL:
        return bool(getattr(settings, "EMAIL_CONFIGURED", False))
    return bool(getattr(settings, f"SEHATDESK_{channel.upper()}_ADAPTER", ""))


class _Fill(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(organization, event: str, channel: str, context: dict) -> tuple[str, str]:
    """(subject, body) for an event and channel, with the allowed placeholders filled in."""
    template = MessageTemplate.objects.filter(
        organization=organization, event=event, channel=channel, is_active=True
    ).first()
    subject, body = (template.subject, template.body) if template else DEFAULTS.get(event, ("", ""))
    allowed = set(PLACEHOLDERS.get(event, []))
    values = _Fill({k: str(v) for k, v in context.items() if k in allowed})
    values.setdefault("hospital", organization.name)
    try:
        return subject.format_map(values)[:160], body.format_map(values)
    except (ValueError, IndexError):  # a stray brace in a hospital's template
        return subject[:160], body


def send_external(organization, event: str, channel: str, *, to: str, context: dict):
    subject, body = render(organization, event, channel, context)
    delivery = MessageDelivery.objects.create(
        organization=organization,
        event=event,
        channel=channel,
        to=to[:160],
        subject=subject,
        body=body,
    )
    adapter = adapter_for(channel)
    delivery.attempts += 1
    delivery.provider = adapter.name[:40]
    try:
        result = adapter.send(to=to, subject=subject, body=body)
    except ChannelNotConfigured as err:
        delivery.status = MessageDelivery.Status.SKIPPED
        delivery.error = str(err)[:300]
    except Exception as err:  # noqa: BLE001 - recorded on the delivery, never raised
        logger.warning("external message failed delivery=%s channel=%s", delivery.pk, channel)
        delivery.status = MessageDelivery.Status.FAILED
        delivery.error = type(err).__name__
    else:
        delivery.status = MessageDelivery.Status.SENT
        delivery.provider = result.provider[:40]
        delivery.provider_message_id = result.message_id[:120]
        delivery.sent_at = timezone.now()
    delivery.save()
    return delivery


def record_status(delivery: MessageDelivery, status: str, *, error: str = "") -> None:
    """For provider callbacks (delivery receipts) once a provider is wired in."""
    if status not in MessageDelivery.Status.values:
        raise ValueError(status)
    delivery.status = status
    delivery.error = error[:300]
    delivery.save(update_fields=["status", "error", "updated_at"])
