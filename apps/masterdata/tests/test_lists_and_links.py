"""Standard lists from defaults.json, type-to-add dropdowns, and doctor/staff links."""

from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import resolve
from PIL import Image

from apps.accounts.models import Role, RoleAssignment
from apps.accounts.services import create_organization_with_admin
from apps.billing.models import PaymentMethod
from apps.billing.services import discount_reasons
from apps.finance.models import ExpenseCategory
from apps.lab.models import LabTestCategory
from apps.masterdata import defaults
from apps.masterdata.forms import DoctorProfileForm
from apps.masterdata.models import Department, ServiceCategory, Specialty
from apps.organizations.models import PrintLayout, preferences_for
from apps.pharmacy.models import ProductCategory

pytestmark = pytest.mark.django_db


def _png():
    buffer = BytesIO()
    Image.new("RGB", (4, 1), "white").save(buffer, "PNG")
    return buffer.getvalue()


PNG = _png()


@pytest.fixture(autouse=True)
def _uploads_in_temp(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def test_new_hospital_starts_with_every_list():
    org, _, _ = create_organization_with_admin(
        org_name="New Clinic",
        branch_name="Main",
        branch_code="MAIN",
        city="",
        admin_data={"username": "first", "password": "Long-pass-2026!", "first_name": "A"},
    )
    assert Department.objects.filter(organization=org).count() == len(defaults.rows("departments"))
    for model in (Specialty, ServiceCategory, LabTestCategory, ProductCategory, ExpenseCategory):
        assert model.objects.filter(organization=org).exists(), model
    assert PaymentMethod.objects.filter(organization=org, code="cash").exists()
    assert preferences_for(org).lists_version == defaults._data()["version"]


def test_loading_again_adds_nothing_and_keeps_changes(org):
    Department.objects.create(organization=org, name="Medicine dept", code="GM", is_active=False)
    Department.objects.create(organization=org, name="cardiology", code="HEART")
    defaults.load_defaults(org)
    count = Department.objects.filter(organization=org).count()
    defaults.load_defaults(org)
    assert Department.objects.filter(organization=org).count() == count
    assert not Department.objects.get(organization=org, code="GM").is_active
    assert not Department.objects.filter(organization=org, name="Cardiology").exists()


def test_migrate_gives_each_version_once(org):
    defaults.load_for_all()
    Specialty.objects.filter(organization=org, name="Physician").delete()
    org._sd_preferences = None
    defaults.load_for_all()  # same version: a removed entry stays removed
    assert not Specialty.objects.filter(organization=org, name="Physician").exists()


def test_standard_suggestions_are_offered(org, admin_client):
    assert "Staff" in discount_reasons(org)
    page = admin_client.get("/staff/new/").content.decode()
    assert '<option value="Receptionist">' in page


def test_quick_add_saves_and_reuses(org, admin_client):
    r = admin_client.post("/settings/quick-add/department/", {"name": " Blood   Bank "})
    assert r.status_code == 200
    dept = Department.objects.get(pk=r.json()["id"])
    assert (dept.name, dept.code, dept.organization) == ("Blood Bank", "BLOODB", org)
    again = admin_client.post("/settings/quick-add/department/", {"name": "blood bank"})
    assert again.json()["id"] == dept.pk


def test_quick_add_brings_back_a_switched_off_entry(org, admin_client):
    dept = Department.objects.create(organization=org, name="Old", code="OLD", is_active=False)
    r = admin_client.post("/settings/quick-add/department/", {"name": "old"})
    assert r.json()["id"] == dept.pk
    dept.refresh_from_db()
    assert dept.is_active


def test_quick_add_gives_each_department_its_own_code(org, admin_client):
    Department.objects.create(organization=org, name="Other", code="DENTAL")
    r = admin_client.post("/settings/quick-add/department/", {"name": "Dental"})
    assert Department.objects.get(pk=r.json()["id"]).code == "DENTAL2"


def test_quick_add_refuses_bad_input_and_wrong_permission(org, admin_client, client, make_user):
    assert admin_client.post("/settings/quick-add/department/", {"name": "x"}).status_code == 400
    assert admin_client.post("/settings/quick-add/nope/", {"name": "Ward"}).status_code == 403
    role = Role.objects.create(organization=org, name="Lab catalogue")
    role.permissions.add(*_perm("lab.catalog.manage"))
    user = make_user(username="labby")
    RoleAssignment.objects.create(user=user, role=role, branch=None)
    client.force_login(user)
    assert client.post("/settings/quick-add/lab-category/", {"name": "Genetics"}).status_code == 200
    assert client.post("/settings/quick-add/department/", {"name": "Genetics"}).status_code == 403
    assert not Department.objects.filter(organization=org, name="Genetics").exists()


def _perm(code):
    from apps.accounts.models import Permission

    return Permission.objects.filter(code=code)


def test_form_dropdowns_are_marked_and_page_lists_allowed_kinds(org, admin_client):
    form = DoctorProfileForm(organization=org)
    assert form.fields["department"].widget.attrs["data-create-kind"] == "department"
    assert form.fields["specialty"].widget.attrs["data-create-kind"] == "specialty"
    page = admin_client.get("/settings/doctors/new/").content.decode()
    assert "data-quick-add=" in page and "specialty" in page


def _rx_layout(org, **fields):
    return PrintLayout.objects.create(organization=org, document="prescription", **fields)


def test_header_image_replaces_clinic_text_but_not_the_doctor(org, admin_client):
    _rx_layout(org, header_image=SimpleUploadedFile("head.png", PNG, "image/png"))
    html = admin_client.get("/settings/print-layouts/preview/?doc=prescription").content.decode()
    assert "rx-image" in html and "letterheads/head" in html
    assert "Dr. Sample Name" in html and "MBBS, FCPS" in html
    assert f'<p class="font-semibold">{org.name}</p>' not in html
    assert "@page { margin: 0; }" in html


def test_clinic_text_can_stay_with_the_image(org, admin_client):
    _rx_layout(
        org,
        header_image=SimpleUploadedFile("head.png", PNG, "image/png"),
        image_keep_clinic_text=True,
    )
    html = admin_client.get("/settings/print-layouts/preview/?doc=prescription").content.decode()
    assert f'<p class="font-semibold">{org.name}</p>' in html


def test_no_image_leaves_the_prescription_as_it_was(org, admin_client):
    html = admin_client.get("/settings/print-layouts/preview/?doc=prescription").content.decode()
    assert f'<p class="font-semibold">{org.name}</p>' in html
    assert "@page { margin: 0; }" not in html
    assert PrintLayout(organization=org, document="prescription").rx_body_height_mm == 178


def test_images_upload_for_prescription_only(org, admin_client):
    data = {
        "margin_mm": 10, "top_space_mm": 0, "bottom_space_mm": 0, "text_size": 100,
        "header_align": "left", "logo_mm": 14, "doctor_side": "left", "show_header": "on",
    }  # fmt: skip
    r = admin_client.post(
        "/settings/print-layouts/?doc=prescription",
        {**data, "footer_image": SimpleUploadedFile("foot.png", PNG, "image/png")},
    )
    assert r.status_code == 302
    saved = PrintLayout.objects.get(organization=org, document="prescription").footer_image
    assert resolve(saved.url).kwargs["path"].startswith("organization-logos/")  # served by Django
    page = admin_client.get("/settings/print-layouts/?doc=invoice").content.decode()
    assert "Letterhead image" not in page


def test_doctor_form_edits_the_staff_name(admin_client, doctor):
    r = admin_client.post(
        f"/settings/doctors/{doctor.pk}/edit/",
        {
            "department": doctor.department_id, "first_name": "Sana", "last_name": "Malik",
            "phone": "0300", "consultation_fee": 1, "follow_up_fee": 1, "follow_up_days": 7,
            "slot_minutes": 15,
        },
    )  # fmt: skip
    assert r.status_code == 302
    doctor.user.refresh_from_db()
    assert doctor.user.get_full_name() == "Sana Malik"
    assert doctor.user.phone == "0300"


def test_staff_and_doctor_pages_link_to_each_other(admin_client, doctor):
    staff_page = admin_client.get(f"/staff/{doctor.user_id}/").content.decode()
    assert f"/settings/doctors/{doctor.pk}/edit/" in staff_page
    doctor_page = admin_client.get(f"/settings/doctors/{doctor.pk}/").content.decode()
    assert f"/staff/{doctor.user_id}/" in doctor_page


def test_add_doctor_opens_with_the_person_chosen(org, admin_client, make_user):
    person = make_user(username="newdoc")
    page = admin_client.get(f"/settings/doctors/new/?user={person.pk}").content.decode()
    assert f'<option value="{person.pk}" selected>' in page


def test_new_staff_with_doctor_role_goes_on_to_the_doctor_form(org, admin_client):
    role = Role.objects.get(organization=org, system_key="doctor")
    r = admin_client.post(
        "/staff/new/",
        {
            "first_name": "Nida", "username": "nida", "role": role.pk,
            "password1": "Long-pass-2026!", "password2": "Long-pass-2026!",
        },
    )  # fmt: skip
    assert r.status_code == 302 and r.url.startswith("/settings/doctors/new/?user=")


def test_desktop_start_loads_a_new_list_version_with_no_migration_to_run(org):
    from launcher.server import migrate_safely

    migrate_safely()
    assert Department.objects.filter(organization=org, code="GM").exists()
