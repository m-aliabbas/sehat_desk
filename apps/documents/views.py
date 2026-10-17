"""Document upload, signed file access and archive (no hard delete)."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import permission_required
from apps.audit import services as audit
from apps.core.scoping import get_scoped_or_404

from . import services
from .forms import ArchiveForm, UploadForm
from .models import Document


def _back(request, fallback):
    nxt = request.POST.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else fallback


def _linked(request, model, field):
    value = request.POST.get(field, "")
    if not value.isdigit():
        return None
    # Documents are organization-wide like patients, so links from any branch are valid.
    return get_scoped_or_404(model, request, all_branches=True, pk=value)


@require_POST
@permission_required("document.upload")
def upload(request, pk):
    from apps.encounters.models import Encounter
    from apps.ipd.models import Admission
    from apps.lab.models import LabOrder
    from apps.patients.models import Patient

    patient = get_scoped_or_404(Patient, request, pk=pk)
    fallback = f"{reverse('patients:detail', args=[pk])}?tab=documents"
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a file to upload.")
        return redirect(_back(request, fallback))
    try:
        doc = services.upload(
            request=request,
            patient=patient,
            file=form.cleaned_data["file"],
            title=form.cleaned_data["title"],
            category=form.cleaned_data["category"],
            encounter=_linked(request, Encounter, "encounter"),
            admission=_linked(request, Admission, "admission"),
            lab_order=_linked(request, LabOrder, "lab_order"),
        )
    except services.DocumentError as err:
        messages.error(request, err.message)
    else:
        audit.record(
            request,
            "document.upload",
            doc,
            summary=f"{patient.mrn} {doc.category} {doc.content_type} {doc.size}",
        )
        messages.success(request, f"{doc.title} uploaded.")
    return redirect(_back(request, fallback))


@require_GET
@permission_required("document.view")
def file(request, pk):
    doc = Document.objects.filter(pk=pk, organization=request.organization).first()
    if doc is None:
        raise Http404
    if not services.token_is_valid(request.GET.get("t", ""), doc, request.user):
        # Expired or copied link: send the user back to the record for a fresh one.
        raise PermissionDenied
    if doc.is_archived and "document.archive" not in request.sd_perms:
        raise Http404
    download = request.GET.get("download") == "1"
    audit.record(request, "document.download" if download else "document.view", doc,
                 summary=f"patient_id={doc.patient_id}")  # fmt: skip
    try:
        handle = doc.file.open("rb")
    except FileNotFoundError as err:
        raise Http404 from err
    response = FileResponse(
        handle, content_type=doc.content_type, as_attachment=download, filename=doc.original_name
    )
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_POST
@permission_required("document.archive")
def archive(request, pk):
    doc = get_object_or_404(Document, pk=pk, organization=request.organization)
    form = ArchiveForm(request.POST)
    if form.is_valid():
        try:
            services.archive(doc, request=request, reason=form.cleaned_data["reason"])
        except services.DocumentError as err:
            messages.error(request, err.message)
        else:
            audit.record(
                request,
                "document.archive",
                doc,
                changes={"archived": [False, True]},
                summary=form.cleaned_data["reason"][:200],
            )
            messages.success(request, f"{doc.title} archived. It can be restored from Archived.")
    else:
        messages.error(request, "Give the reason for archiving the document.")
    return redirect(
        _back(request, f"{reverse('patients:detail', args=[doc.patient_id])}?tab=documents")
    )


@require_POST
@permission_required("document.archive")
def restore(request, pk):
    doc = get_object_or_404(Document, pk=pk, organization=request.organization)
    try:
        services.restore(doc, request=request)
    except services.DocumentError as err:
        messages.error(request, err.message)
    else:
        audit.record(request, "document.restore", doc, changes={"archived": [True, False]})
        messages.success(request, f"{doc.title} restored.")
    return redirect(
        _back(request, f"{reverse('patients:detail', args=[doc.patient_id])}?tab=documents")
    )
