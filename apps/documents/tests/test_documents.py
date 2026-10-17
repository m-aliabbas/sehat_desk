import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.documents import services
from apps.documents.models import Document
from apps.ipd.tests.helpers import make_request
from apps.organizations.models import Branch, Organization
from apps.patients.services import register_patient

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def pdf(name="report.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


@pytest.fixture
def req(rf, admin_user, org, branch):
    return make_request(rf, admin_user, org, branch)


def test_upload_stores_private_file_with_random_name(req, patient):
    doc = services.upload(request=req, patient=patient, file=pdf("CBC Ayesha.pdf"), category="lab")
    assert doc.content_type == "application/pdf" and doc.size == len(PDF)
    assert len(doc.sha256) == 64 and doc.title == "CBC Ayesha"
    assert "Ayesha" not in doc.file.name and doc.file.name.startswith(f"{req.organization.pk}/")
    assert doc.file.storage is not None and doc.file.storage.exists(doc.file.name)


@pytest.mark.parametrize(
    "name,content,message",
    [
        ("virus.exe", b"MZ\x90\x00", "Only PDF"),
        ("fake.pdf", b"MZ\x90\x00not a pdf", "does not match"),
        ("image.pdf", PNG, "does not match"),  # a real PNG renamed to .pdf
        ("empty.pdf", b"", "Choose a file"),
    ],
)
def test_upload_rejects_unsafe_files(req, patient, name, content, message):
    with pytest.raises(services.DocumentError, match=message):
        services.upload(request=req, patient=patient, file=pdf(name, content))


def test_size_limit(req, patient, settings):
    settings.SEHATDESK_DOCUMENT_MAX_MB = 1
    big = PDF + b"0" * (1024 * 1024)
    with pytest.raises(services.DocumentError, match="too large"):
        services.upload(request=req, patient=patient, file=pdf(content=big))


def test_links_must_belong_to_the_patient(req, org, branch, admin_user, patient):
    from apps.lab.models import LabOrder

    other = register_patient(
        organization=org,
        branch=branch,
        user=admin_user,
        data={"full_name": "Other", "gender": "male"},
    )
    order = LabOrder.objects.create(
        organization=org, branch=branch, number="LAB-1", patient=other, created_by=admin_user
    )
    with pytest.raises(services.DocumentError, match="another patient"):
        services.upload(request=req, patient=patient, file=pdf(), lab_order=order)


def test_signed_links_are_per_user_and_expire(
    client, admin_user, make_user, req, patient, settings
):
    doc = services.upload(request=req, patient=patient, file=pdf())
    client.force_login(admin_user)
    url = services.signed_url(doc, admin_user)
    response = client.get(url)
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content) == PDF
    assert AuditLog.objects.filter(action="document.view", record_id=str(doc.pk)).exists()
    # No token, or another user's token, is refused.
    assert client.get(reverse("documents:file", args=[doc.pk])).status_code == 403
    doctor = make_user("doctor")
    client.force_login(doctor)
    assert client.get(url).status_code == 403
    assert client.get(services.signed_url(doc, doctor)).status_code == 200
    # Expired.
    settings.SEHATDESK_DOCUMENT_LINK_SECONDS = -1
    assert client.get(services.signed_url(doc, doctor)).status_code == 403


def test_permission_and_organization_checks(client, make_user, req, patient):
    doc = services.upload(request=req, patient=patient, file=pdf())
    cashier = make_user("cashier")
    client.force_login(cashier)
    assert client.get(services.signed_url(doc, cashier)).status_code == 403  # no document.view
    other_org = Organization.objects.create(name="Other hospital")
    Branch.objects.create(organization=other_org, name="Main", code="M", is_main=True)
    from apps.accounts.services import seed_default_roles

    seed_default_roles(other_org)
    outsider = make_user("doctor", organization=other_org)
    client.force_login(outsider)
    assert client.get(services.signed_url(doc, outsider)).status_code == 404


def test_archive_is_audited_and_restorable(client, admin_user, make_user, req, patient):
    doc = services.upload(request=req, patient=patient, file=pdf())
    url = reverse("documents:archive", args=[doc.pk])
    nurse = make_user("nurse")
    client.force_login(nurse)
    assert client.post(url, {"reason": "dup"}).status_code == 403  # nurses cannot archive
    client.force_login(admin_user)
    client.post(url, {"reason": ""})
    assert not Document.objects.get(pk=doc.pk).is_archived  # a reason is required
    client.post(url, {"reason": "Wrong patient"})
    doc.refresh_from_db()
    assert doc.is_archived and doc.archive_reason == "Wrong patient"
    assert Document.objects.filter(pk=doc.pk).exists()  # never hard-deleted
    assert AuditLog.objects.filter(action="document.archive", record_id=str(doc.pk)).exists()
    # Archived documents are hidden from staff without the archive permission.
    client.force_login(nurse)
    assert client.get(services.signed_url(doc, nurse)).status_code == 404
    client.force_login(admin_user)
    client.post(reverse("documents:restore", args=[doc.pk]))
    assert not Document.objects.get(pk=doc.pk).is_archived


def test_upload_view_and_patient_tab(admin_client, patient):
    response = admin_client.post(
        reverse("documents:upload", args=[patient.pk]),
        {"file": pdf(), "title": "Referral", "category": "referral"},
    )
    assert response.status_code == 302
    doc = Document.objects.get(patient=patient)
    page = admin_client.get(reverse("patients:detail", args=[patient.pk]), {"tab": "documents"})
    assert b"Referral" in page.content
    assert reverse("documents:file", args=[doc.pk]).encode() in page.content


def test_lab_attachment_uses_secure_checks(admin_client, org, branch, admin_user, patient):
    from apps.lab.models import LabAttachment, LabOrder, LabOrderItem, LabTest, LabTestCategory

    cat = LabTestCategory.objects.create(organization=org, name="Chem")
    test = LabTest.objects.create(organization=org, code="X", name="X", category=cat)
    order = LabOrder.objects.create(
        organization=org, branch=branch, number="LAB-9", patient=patient, created_by=admin_user
    )
    item = LabOrderItem.objects.create(order=order, test=test, test_name="X")
    admin_client.post(
        reverse("lab:attachment-upload", args=[item.pk]), {"file": pdf("scan.pdf", b"MZ fake")}
    )
    assert not LabAttachment.objects.exists()
    admin_client.post(reverse("lab:attachment-upload", args=[item.pk]), {"file": pdf("scan.pdf")})
    assert LabAttachment.objects.count() == 1
