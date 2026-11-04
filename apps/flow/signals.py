"""The visit follows what other apps save, so the queue, billing, lab, prescriptions and pharmacy
never import the flow app to tell it."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.billing.models import Invoice
from apps.lab.models import LabOrder
from apps.pharmacy.models import PharmacySale
from apps.prescriptions.models import Prescription
from apps.queue.models import QueueToken

from . import services


def _touches(update_fields, *names) -> bool:
    return update_fields is None or bool(set(names) & set(update_fields))


@receiver(post_save, sender=QueueToken, dispatch_uid="flow_token_saved")
def token_saved(sender, instance, created, update_fields=None, **kwargs):
    if created:
        services.open_visit(instance)
    elif _touches(update_fields, "status"):
        services.on_token_status(instance)


@receiver(post_save, sender=Invoice, dispatch_uid="flow_invoice_saved")
def invoice_saved(sender, instance, created, update_fields=None, **kwargs):
    if created or _touches(update_fields, "status"):
        services.sync_invoice(instance)


@receiver(post_save, sender=Prescription, dispatch_uid="flow_rx_sent")
def prescription_saved(sender, instance, update_fields=None, **kwargs):
    if not instance.sent_to_pharmacy_at or not _touches(update_fields, "sent_to_pharmacy_at"):
        return
    encounter = instance.encounter
    if encounter is not None and encounter.token_id:
        services.on_sent_on(encounter.token)


@receiver(post_save, sender=LabOrder, dispatch_uid="flow_lab_order_saved")
def lab_order_saved(sender, instance, created, update_fields=None, **kwargs):
    if created:
        if instance.encounter_id and instance.encounter.token_id:
            services.on_sent_on(instance.encounter.token)
    elif _touches(update_fields, "status"):
        services.on_lab_order_saved(instance)


@receiver(post_save, sender=PharmacySale, dispatch_uid="flow_pharmacy_sale_saved")
def pharmacy_sale_saved(sender, instance, update_fields=None, **kwargs):
    if instance.status == PharmacySale.Status.COMPLETED and _touches(update_fields, "status"):
        services.on_pharmacy_sale(instance)
