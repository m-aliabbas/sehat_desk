"""Print routing: a PC running the print helper (station) and the documents waiting for it."""

from django.conf import settings
from django.db import models

from apps.core.scoping import OrgScopedModel


class Paper(models.TextChoices):
    THERMAL = "thermal", "80 mm receipts and tokens"
    LABEL = "label", "Labels and stickers"
    A5 = "a5", "A5 (prescriptions, slips)"
    A4 = "a4", "A4 (invoices, reports, ledgers)"


class PrintStation(OrgScopedModel):
    """One PC. A browser uses the station on its own IP address, so staff choose nothing."""

    name = models.CharField(max_length=80)
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    printers = models.JSONField(default=list, blank=True)
    printer_sizes = models.JSONField(default=dict, blank=True)  # {printer: [[width_mm, height_mm]]}
    manual_routes = models.JSONField(default=list, blank=True)  # papers an admin set by hand
    route_thermal = models.CharField(max_length=200, blank=True)
    route_label = models.CharField(max_length=200, blank=True)
    route_a5 = models.CharField(max_length=200, blank=True)
    route_a4 = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def printer_for(self, paper: str) -> str:
        route = getattr(self, f"route_{paper}", "")
        if not route and paper == Paper.A5 and paper not in self.manual_routes:
            return self.route_a4  # an A5 page prints at true size on the A4 printer
        return route


class PrintEnrollment(OrgScopedModel):
    """While open, a print helper on the hospital network can connect itself."""

    open_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="one_print_enrollment_per_org")
        ]


class PrintJob(OrgScopedModel):
    """Holds the page only until the helper collects it; the html is blanked at that moment."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        SENDING = "sending", "Printing"
        DONE = "done", "Printed"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Not collected"

    station = models.ForeignKey(PrintStation, on_delete=models.CASCADE, related_name="jobs")
    paper = models.CharField(max_length=10, choices=Paper.choices)
    printer = models.CharField(max_length=200)
    html = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=["station", "status", "created_at"])]
