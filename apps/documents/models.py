"""Patient documents and attachments (Phase 18).

Files live in the "documents" storage (settings.STORAGES), never under the public media route.
They are stored under a random name, so no patient detail appears in a path, and are served
only through a signed, expiring, permission-checked link (see services.signed_url).

A document is never deleted by the app: it is archived with a reason, audited, and can be
restored. Each document belongs to one patient and may also point at the consultation,
admission or lab order it came from.
"""

import uuid

from django.conf import settings
from django.core.files.storage import storages
from django.db import models

from apps.core.models import ArchivableModel


def document_storage():
    return storages["documents"]


def document_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"{instance.organization_id}/{uuid.uuid4().hex}.{ext}"


class Document(ArchivableModel):
    class Category(models.TextChoices):
        REPORT = "report", "Outside report"
        IMAGING = "imaging", "Imaging / scan"
        LAB = "lab", "Lab printout"
        REFERRAL = "referral", "Referral letter"
        CONSENT = "consent", "Consent form"
        DISCHARGE = "discharge", "Discharge papers"
        IDENTITY = "identity", "ID / insurance card"
        PRESCRIPTION = "prescription", "Old prescription"
        OTHER = "other", "Other"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="+"
    )
    branch = models.ForeignKey(
        "organizations.Branch", on_delete=models.PROTECT, related_name="+",
        help_text="Where it was uploaded. Patient documents are visible in every branch.",
    )  # fmt: skip
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="documents"
    )
    encounter = models.ForeignKey(
        "encounters.Encounter", on_delete=models.PROTECT, null=True, blank=True,
        related_name="documents",
    )  # fmt: skip
    admission = models.ForeignKey(
        "ipd.Admission", on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    lab_order = models.ForeignKey(
        "lab.LabOrder", on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    title = models.CharField(max_length=160)
    file = models.FileField(upload_to=document_path, storage=document_storage, max_length=255)
    original_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=60)  # detected from the file's bytes
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    archive_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["patient", "-created_at"])]

    def __str__(self):
        return self.title

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def size_label(self) -> str:
        if self.size >= 1024 * 1024:
            return f"{self.size / (1024 * 1024):.1f} MB"
        return f"{max(1, round(self.size / 1024))} KB"

    @property
    def source_label(self) -> str:
        if self.lab_order_id:
            return f"Lab order {self.lab_order.number}"
        if self.admission_id:
            return f"Admission {self.admission.number}"
        if self.encounter_id:
            return f"Consultation {self.encounter.number}"
        return "Patient record"
