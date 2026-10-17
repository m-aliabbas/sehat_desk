"""Secure upload, signed access and archiving for patient documents.

Upload checks, in order: size limit, extension allow-list, and the file's real type read from
its first bytes ("magic numbers"), which must match the extension. The browser's declared
content type is ignored, because any client can lie about it. Lab result attachments use the
same checks (validate_upload).

Access: templates never link to a file directly. signed_url() makes a link bound to one user
that expires (SEHATDESK_DOCUMENT_LINK_SECONDS). The file view checks the signature, the
signed-in user, the organization and the document.view permission again, then audits it.
"""

import hashlib

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Document

# Detected type -> allowed file extensions.
ALLOWED_TYPES = {
    "application/pdf": {"pdf"},
    "image/png": {"png"},
    "image/jpeg": {"jpg", "jpeg"},
    "image/webp": {"webp"},
}
ACCEPT_ATTR = "application/pdf,image/png,image/jpeg,image/webp"
SIGNER_SALT = "sehatdesk.documents.file"


class DocumentError(ValidationError):
    pass


def _need(request, code, action):
    if code not in request.sd_perms:
        raise DocumentError(f"You are not allowed to {action}.")


def max_bytes() -> int:
    return settings.SEHATDESK_DOCUMENT_MAX_MB * 1024 * 1024


def sniff(head: bytes) -> str | None:
    """The file type from its leading bytes, or None if it is not an allowed type."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_upload(upload, *, limit: int | None = None) -> str:
    """Check an uploaded file and return its detected content type."""
    limit = limit or max_bytes()
    if upload is None or not upload.size:
        raise DocumentError("Choose a file to upload.")
    if upload.size > limit:
        raise DocumentError(f"The file is too large. The limit is {limit // (1024 * 1024)} MB.")
    name = upload.name or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in set().union(*ALLOWED_TYPES.values()):
        raise DocumentError("Only PDF, PNG, JPEG and WebP files can be uploaded.")
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    detected = sniff(head)
    if detected is None or ext not in ALLOWED_TYPES[detected]:
        raise DocumentError("The file's content does not match its type. Upload the original file.")
    return detected


def _sha256(upload) -> str:
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


@transaction.atomic
def upload(
    *,
    request,
    patient,
    file,
    title: str = "",
    category: str = Document.Category.OTHER,
    encounter=None,
    admission=None,
    lab_order=None,
) -> Document:
    _need(request, "document.upload", "upload documents")
    org = request.organization
    if patient.organization_id != org.id:
        raise DocumentError("That patient is not in this hospital.")
    for linked, label in ((encounter, "consultation"), (admission, "admission"),
                          (lab_order, "lab order")):  # fmt: skip
        if linked is not None and (
            linked.organization_id != org.id or linked.patient_id != patient.pk
        ):
            raise DocumentError(f"That {label} belongs to another patient.")
    content_type = validate_upload(file)
    if category not in Document.Category.values:
        category = Document.Category.OTHER
    original = (file.name or "document").rsplit("/", 1)[-1][:200]
    return Document.objects.create(
        organization=org,
        branch=request.branch,
        patient=patient,
        encounter=encounter,
        admission=admission,
        lab_order=lab_order,
        category=category,
        title=(title.strip() or original.rsplit(".", 1)[0])[:160],
        file=file,
        original_name=original,
        content_type=content_type,
        size=file.size,
        sha256=_sha256(file),
        uploaded_by=request.user,
    )


@transaction.atomic
def archive(document: Document, *, request, reason: str) -> Document:
    _need(request, "document.archive", "archive documents")
    if not reason.strip():
        raise DocumentError("Give the reason for archiving the document.")
    document = Document.objects.select_for_update(of=("self",)).get(pk=document.pk)
    if document.is_archived:
        raise DocumentError("This document is already archived.")
    document.archived_at = timezone.now()
    document.archived_by = request.user
    document.archive_reason = reason.strip()[:200]
    document.save(update_fields=["archived_at", "archived_by", "archive_reason", "updated_at"])
    return document


@transaction.atomic
def restore(document: Document, *, request) -> Document:
    _need(request, "document.archive", "restore documents")
    document = Document.objects.select_for_update(of=("self",)).get(pk=document.pk)
    if not document.is_archived:
        raise DocumentError("This document is not archived.")
    document.archived_at = None
    document.archived_by = None
    document.archive_reason = ""
    document.save(update_fields=["archived_at", "archived_by", "archive_reason", "updated_at"])
    return document


# --- Signed access -----------------------------------------------------------------------------


def _signer():
    return signing.TimestampSigner(salt=SIGNER_SALT)


def signed_url(document: Document, user, *, download: bool = False) -> str:
    token = _signer().sign(f"{document.pk}.{user.pk}")
    url = f"{reverse('documents:file', args=[document.pk])}?t={token}"
    return url + "&download=1" if download else url


def token_is_valid(token: str, document: Document, user) -> bool:
    try:
        value = _signer().unsign(token, max_age=settings.SEHATDESK_DOCUMENT_LINK_SECONDS)
    except signing.BadSignature:  # includes SignatureExpired
        return False
    return value == f"{document.pk}.{user.pk}"


# --- Panels on other screens -------------------------------------------------------------------


def panel_context(request, patient, *, encounter=None, admission=None, lab_order=None) -> dict:
    """Context for templates/documents/_panel.html on the patient, consultation, admission or
    lab screens. Empty unless the user may view documents."""
    from .forms import UploadForm

    can_view = "document.view" in request.sd_perms
    if not can_view:
        return {"doc_can_view": False}
    show_archived = request.GET.get("archived") == "1" and "document.archive" in request.sd_perms
    qs = Document.objects.filter(organization=request.organization, patient=patient)
    if encounter is not None:
        qs = qs.filter(encounter=encounter)
    if admission is not None:
        qs = qs.filter(admission=admission)
    if lab_order is not None:
        qs = qs.filter(lab_order=lab_order)
    qs = qs.archived() if show_archived else qs.active()
    docs = list(
        qs.select_related("uploaded_by", "archived_by", "encounter", "admission", "lab_order")
    )
    for d in docs:
        d.view_url = signed_url(d, request.user)
        d.download_url = signed_url(d, request.user, download=True)
    return {
        "doc_can_view": True,
        "doc_patient": patient,
        "documents": docs,
        "doc_show_archived": show_archived,
        "doc_can_upload": "document.upload" in request.sd_perms,
        "doc_can_archive": "document.archive" in request.sd_perms,
        "doc_form": UploadForm(),
        "doc_accept": ACCEPT_ATTR,
        "doc_max_mb": settings.SEHATDESK_DOCUMENT_MAX_MB,
        "doc_link": {
            "encounter": encounter.pk if encounter else "",
            "admission": admission.pk if admission else "",
            "lab_order": lab_order.pk if lab_order else "",
        },
    }
