"""Keep lab orders in step with their invoice: paying it moves the order out of
"payment pending" without the billing app knowing about the lab."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.billing.models import Invoice


@receiver(post_save, sender=Invoice, dispatch_uid="lab_refresh_on_invoice_save")
def refresh_lab_orders(sender, instance, update_fields=None, **kwargs):
    if update_fields is not None and "status" not in update_fields:
        return
    from .services import refresh_for_invoice

    refresh_for_invoice(instance)
