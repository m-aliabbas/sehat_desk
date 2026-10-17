import io
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.audit.models import AuditLog
from apps.organizations.models import Organization
from apps.patients.identity import format_cnic, normalize_cnic, normalize_phone
from apps.patients.models import Allergy, Patient
from apps.patients.services import find_duplicates, register_patient


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0300-1234567", "03001234567"),
        ("+92 300 1234567", "03001234567"),
        ("923001234567", "03001234567"),
        ("3001234567", "03001234567"),
        ("051 1234567", "0511234567"),
        ("", ""),
    ],
)
def test_phone_normalization(raw, expected):
    assert normalize_phone(raw) == expected


def test_cnic_normalization():
    assert normalize_cnic("35202-1234567-1") == "3520212345671"
    assert format_cnic("3520212345671") == "35202-1234567-1"


def test_mrn_is_sequential_and_uses_prefix(org, branch, admin_user):
    p1 = register_patient(
        organization=org,
        branch=branch,
        user=admin_user,
        data={"full_name": "A", "phone": "03001111111"},
    )
    p2 = register_patient(
        organization=org,
        branch=branch,
        user=admin_user,
        data={"full_name": "B", "phone": "03002222222"},
    )
    assert (p1.mrn, p2.mrn) == ("SD-000001", "SD-000002")


def test_mrn_sequences_are_per_organization(org, branch, admin_user):
    from apps.organizations.models import Branch

    other = Organization.objects.create(name="Other", patient_id_prefix="OT")
    ob = Branch.objects.create(organization=other, name="M", code="M")
    register_patient(organization=org, branch=branch, user=admin_user, data={"full_name": "A"})
    p = register_patient(organization=other, branch=ob, user=admin_user, data={"full_name": "B"})
    assert p.mrn == "OT-000001"


def _reg(**overrides):
    data = {
        "full_name": "Nadia Rauf",
        "phone": "0300 7654321",
        "gender": "female",
        "next": "profile",
    }
    data.update(overrides)
    return data


def test_register_patient_normalizes_and_audits(admin_client, org):
    response = admin_client.post(
        "/patients/register/", _reg(cnic="35202-1234567-1", age_years="30")
    )
    p = Patient.objects.get(organization=org)
    assert response.url == f"/patients/{p.pk}/"
    assert p.phone == "03007654321" and p.cnic == "3520212345671"
    assert p.dob_is_estimated and p.age == date.today().year - (date.today().year - 30)
    assert AuditLog.objects.filter(action="patient.create", record_id=str(p.pk)).exists()


def test_register_validates_cnic_and_phone(admin_client):
    response = admin_client.post("/patients/register/", _reg(cnic="12345", phone="12"))
    form = response.context["form"]
    assert "cnic" in form.errors and "phone" in form.errors


def test_save_and_book_goes_to_booking(admin_client, org):
    response = admin_client.post("/patients/register/", _reg(next="book"))
    p = Patient.objects.get(organization=org)
    assert response.url == f"/appointments/book/?patient={p.pk}"


def test_duplicate_cnic_blocks_until_confirmed(admin_client, patient):
    patient.cnic = "3520212345671"
    patient.save()
    response = admin_client.post("/patients/register/", _reg(cnic="35202-1234567-1"))
    assert response.status_code == 200
    assert b"looks like an existing patient" in response.content
    response = admin_client.post(
        "/patients/register/", _reg(cnic="35202-1234567-1", not_duplicate="on")
    )
    assert response.status_code == 302
    assert AuditLog.objects.filter(
        action="patient.create", summary__contains="duplicate_override"
    ).exists()


def test_duplicate_detection_rules(org, patient):
    # Similar spelling + same phone -> strong.
    m = find_duplicates(org, full_name="Aisha Khan", phone="+92 300 1234567")
    assert m and m[0].patient == patient and m[0].strong
    # Same phone, different name -> weak (family number).
    m = find_duplicates(org, full_name="Bilal Hussain", phone="03001234567")
    assert m and not m[0].strong
    # Nothing in common.
    assert find_duplicates(org, full_name="Zara Malik", phone="03339999999") == []


def test_duplicate_panel_htmx(admin_client, patient):
    response = admin_client.get(
        "/patients/duplicates/", {"phone": "0300-1234567", "full_name": "Ayesha Khan"}
    )
    assert b"Possible duplicate" in response.content


@pytest.mark.parametrize("q", ["SD-000001", "000001", "0300 1234567", "ayesha", "khan ayesha"])
def test_search_finds_patient(admin_client, patient, q):
    response = admin_client.get("/patients/", {"q": q})
    assert patient in response.context["page_obj"].object_list


def test_search_by_cnic(admin_client, patient):
    patient.cnic = "3520212345671"
    patient.save()
    response = admin_client.get("/patients/", {"q": "35202-1234567-1"})
    assert patient in response.context["page_obj"].object_list


def test_fuzzy_suggestion_when_no_exact_match(admin_client, patient):
    response = admin_client.get("/patients/", {"q": "Aysha"})
    assert not response.context["page_obj"].object_list
    assert patient in response.context["suggestions"]


def test_search_never_returns_other_org_patients(admin_client, patient, admin_user):
    from apps.organizations.models import Branch

    other = Organization.objects.create(name="Other")
    ob = Branch.objects.create(organization=other, name="M", code="M")
    stranger = register_patient(
        organization=other, branch=ob, user=admin_user, data={"full_name": "Ayesha Khan"}
    )
    response = admin_client.get("/patients/", {"q": "ayesha"})
    assert stranger not in response.context["page_obj"].object_list
    assert admin_client.get(f"/patients/{stranger.pk}/").status_code == 404
    assert (
        admin_client.get("/patients/lookup/", {"q": "ayesha"}).content.count(b"patient-picked") == 1
    )


@pytest.mark.parametrize(
    "role,url,expected",
    [
        ("lab", "/patients/register/", 403),
        ("pharmacy", "/patients/1/edit/", 403),
        ("hospital_admin", "/patients/", 200),
        ("cashier", "/patients/register/", 403),
    ],
)
def test_patient_permissions(client, make_user, patient, role, url, expected):
    client.force_login(make_user(role))
    assert client.get(url.replace("/1/", f"/{patient.pk}/")).status_code == expected


def test_profile_view_is_audited_and_tabs_render(admin_client, patient):
    for tab in ["overview", "visits", "prescriptions", "lab", "billing", "documents", "timeline"]:
        assert admin_client.get(f"/patients/{patient.pk}/", {"tab": tab}).status_code == 200
    assert AuditLog.objects.filter(action="patient.view", record_id=str(patient.pk)).count() == 7


def test_edit_demographics_records_diff(admin_client, patient):
    admin_client.post(
        f"/patients/{patient.pk}/edit/",
        {
            "full_name": "Ayesha Khan",
            "phone": "03001234567",
            "gender": "female",
            "blood_group": "B+",
            "allergy_status": "none",
        },
    )
    patient.refresh_from_db()
    assert patient.blood_group == "B+"
    log = AuditLog.objects.get(action="patient.update")
    assert log.changes["blood_group"] == ["", "B+"]


def test_allergy_is_never_deleted_only_marked_error(admin_client, patient):
    admin_client.post(
        f"/patients/{patient.pk}/allergies/",
        {"substance": "Penicillin", "severity": "severe", "source": "reported"},
    )
    allergy = Allergy.objects.get(patient=patient)
    patient.refresh_from_db()
    assert patient.allergy_status == "present"
    admin_client.post(
        f"/patients/{patient.pk}/allergies/{allergy.pk}/error/", {"reason": "Wrong patient"}
    )
    allergy.refresh_from_db()
    assert allergy.entered_in_error and allergy.error_reason == "Wrong patient"
    assert Allergy.objects.filter(patient=patient).count() == 1


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, "PNG")
    return SimpleUploadedFile("p.png", buf.getvalue(), content_type="image/png")


def test_photo_served_only_through_checked_view(
    admin_client, client, make_user, patient, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    patient.photo = _png()
    patient.save()
    assert admin_client.get(f"/patients/{patient.pk}/photo/").status_code == 200
    # The raw media path is not routed at all.
    assert admin_client.get(f"/media/{patient.photo.name}").status_code == 404
    # A user without patient.view in this organization cannot fetch it.
    client.force_login(make_user("cashier", username="cash"))
    assert (
        client.get(f"/patients/{patient.pk}/photo/").status_code == 200
    )  # cashier has patient.view
    client.force_login(make_user("lab", username="labby"))
    other_org = Organization.objects.create(name="Elsewhere")
    patient.organization = other_org
    patient.save()
    assert client.get(f"/patients/{patient.pk}/photo/").status_code == 404


def test_registration_slip(admin_client, patient):
    response = admin_client.get(f"/patients/{patient.pk}/slip/")
    assert patient.mrn.encode() in response.content
