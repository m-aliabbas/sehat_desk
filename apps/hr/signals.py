"""Doctor commission follows the invoice: paying it in full earns the share, a refund or
cancellation reverses it, without the billing app knowing about payroll."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.billing.models import Invoice

WATCHED = {"status", "amount_paid", "amount_refunded"}


@receiver(post_save, sender=Invoice, dispatch_uid="hr_commission_on_invoice_save")
def sync_commission(sender, instance, created=False, update_fields=None, **kwargs):
    if created or (update_fields is not None and not WATCHED & set(update_fields)):
        return
    from .commission import sync_invoice

    sync_invoice(instance)
