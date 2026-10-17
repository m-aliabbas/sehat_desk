import io

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.audit.models import AuditLog
from apps.core.scoping import scoped
from apps.organizations.models import Branch


def _org_post(org, **overrides):
    data = {
        "name": org.name,
        "timezone": "Asia/Karachi",
        "currency": "PKR",
        "language": "en",
        "patient_id_prefix": "sd",
        "receipt_prefix": "INV",
    }
    data.update(overrides)
    return data


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "teal").save(buf, "PNG")
    return SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")


def test_update_hospital_settings_is_audited(admin_client, org):
    response = admin_client.post("/settings/hospital/", _org_post(org, name="Sehat Hospital"))
    assert response.status_code == 302
    org.refresh_from_db()
    assert org.name == "Sehat Hospital"
    assert org.patient_id_prefix == "SD"
    assert AuditLog.objects.get(action="organization.update").changes["name"] == [
        "Sehat Medical Centre",
        "Sehat Hospital",
    ]


def test_logo_upload_and_svg_rejected(admin_client, org, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    admin_client.post("/settings/hospital/", {**_org_post(org), "logo": _png()})
    org.refresh_from_db()
    assert org.logo.name.startswith("organization-logos/")
    svg = SimpleUploadedFile("x.svg", b"<svg onload=alert(1)></svg>", content_type="image/svg+xml")
    response = admin_client.post("/settings/hospital/", {**_org_post(org), "logo": svg})
    assert response.status_code == 200  # rejected with a form error


def test_media_requires_login(client, admin_user):
    assert client.get("/media/organization-logos/logo.png").status_code == 302


def test_create_branch_and_unique_code(admin_client, org):
    admin_client.post("/settings/branches/new/", {"name": "City branch", "code": "city"})
    assert Branch.objects.get(organization=org, code="CITY")
    response = admin_client.post("/settings/branches/new/", {"name": "Another", "code": "CITY"})
    assert response.status_code == 200
    assert b"already uses this code" in response.content


def test_only_one_main_branch(admin_client, org, branch):
    admin_client.post(
        "/settings/branches/new/", {"name": "New main", "code": "NEW", "is_main": "on"}
    )
    assert list(
        Branch.objects.filter(organization=org, is_main=True).values_list("code", flat=True)
    ) == ["NEW"]


def test_main_branch_cannot_be_deactivated(admin_client, branch):
    admin_client.post(f"/settings/branches/{branch.pk}/status/")
    branch.refresh_from_db()
    assert branch.is_active


def test_deactivated_branch_removes_access(client, make_user, branch, branch2):
    user = make_user("reception", branch=branch2)
    client.force_login(user)
    assert client.get("/").status_code == 200
    branch2.is_active = False
    branch2.save()
    assert client.get("/").url == "/workspace/no-access/"


def test_timezone_follows_organization(admin_client):
    from django.utils import timezone

    response = admin_client.get("/")
    assert response.status_code == 200
    assert timezone.get_current_timezone_name() in ("Asia/Karachi", "UTC")


def test_scoped_helper_filters_by_org_and_branch(rf, org, branch, branch2, admin_user):
    request = rf.get("/")
    request.organization = org
    request.branch = branch
    # Branch has no `branch` field, so only the organization filter applies.
    assert set(scoped(Branch, request)) == {branch, branch2}
    request.organization = None
    assert not scoped(Branch, request).exists()
