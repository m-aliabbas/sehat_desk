"""All menu page: short admin sidebar, screens grouped by category, procedures and lab setup."""

import re

import pytest
from django.urls import reverse

from apps.masterdata.models import Service

SIDEBAR = ["Overview", "Inventory", "Reports", "Finance & P&L", "Reception", "Procedures", "Lab"]


def _sidebar_labels(html):
    aside = html.split('aria-label="Main navigation"')[1].split("</aside>")[0]
    return re.findall(
        r'<span class="sidebar-text flex-1 truncate[^"]*">([^<]+)</span>',
        aside.replace("&amp;", "&"),
    )


@pytest.mark.parametrize("role", ["super_admin", "hospital_admin"])
def test_admin_sidebar_is_short_with_settings_last(client, make_user, role):
    client.force_login(make_user(role))
    html = client.get(reverse("core:dashboard")).content.decode()
    labels = _sidebar_labels(html)
    # The till shows for whoever may sell; hospital admins get that right per hospital.
    assert ("Pharmacy" in labels) == (role == "super_admin")
    assert [x for x in labels if x != "Pharmacy"] == [*SIDEBAR, "All menu", "Settings"]


def test_other_roles_keep_their_menu(client, make_user):
    client.force_login(make_user("reception"))
    labels = _sidebar_labels(
        client.get(reverse("core:dashboard") + "?view=overview").content.decode()
    )
    assert "Reception & tokens" in labels
    assert "Appointments" not in labels  # booked from the reception desk
    assert "Settings" not in labels


def test_every_tile_opens_for_super_admin(admin_client):
    html = admin_client.get(reverse("core:menu")).content.decode()
    content = html.split("</aside>")[1]
    urls = set(re.findall(r'<a href="(/[^"]+)" class="flex items-center gap-3', content))
    assert len(urls) > 20
    for url in urls:
        assert admin_client.get(url.replace("&amp;", "&"), follow=True).status_code == 200, url


def test_hospital_admin_sees_categories_and_locked_terminals(client, make_user):
    client.force_login(make_user("hospital_admin"))
    html = client.get(reverse("core:menu")).content.decode()
    for title in ("HR &amp; accounts", "Inventory", "Terminals", "Procedures", "Lab tests"):
        assert title in html
    assert "Not in your role" in html
    assert f'href="{reverse("encounters:workspace")}"' not in html
    assert reverse("lab:catalog") in html


def test_settings_stays_its_own_section(admin_client):
    response = admin_client.get(reverse("organizations:settings"))
    assert response.url == reverse("organizations:hospital")
    html = admin_client.get(response.url).content.decode()
    assert 'class="tab"' in html
    assert "Save modules" not in html


def test_turned_off_module_leaves_the_page(admin_client):
    admin_client.post(
        reverse("organizations:modules"),
        {"feature_lab": "on", "feature_pharmacy": "on", "feature_nursing": "on"},
    )
    html = admin_client.get(reverse("core:menu")).content.decode()
    assert reverse("ipd:setup") not in html
    assert reverse("nursing:workspace") not in html


def test_all_menu_refused_without_admin_rights(client, make_user):
    client.force_login(make_user("doctor"))
    assert client.get(reverse("core:menu")).status_code == 403


def test_new_procedure_form_starts_as_procedure(admin_client, org):
    url = reverse("masterdata:service-create")
    html = admin_client.get(url, {"kind": "procedure"}).content.decode()
    assert re.search(r'<input[^>]*name="is_procedure"[^>]*checked', html)
    html = admin_client.get(url).content.decode()
    assert not re.search(r'<input[^>]*name="is_procedure"[^>]*checked', html)
    assert not Service.objects.filter(organization=org, is_procedure=True).exists()


def test_catalogue_only_admin_gets_no_patient_lab_data(client, make_user, patient, branch):
    from apps.lab.models import LabOrder

    admin = make_user("hospital_admin")
    client.force_login(admin)
    response = client.get(reverse("lab:worklist"))
    assert response.status_code == 302
    assert response.url == reverse("lab:catalog")
    assert client.get(reverse("lab:catalog")).status_code == 200
    order = LabOrder.objects.create(
        organization=patient.organization,
        branch=branch,
        patient=patient,
        number="LAB-T1",
        created_by=admin,
    )
    assert client.get(reverse("lab:order-detail", args=[order.pk])).status_code == 403


def test_existing_hospital_admin_role_gets_sidebar_access(org):
    from importlib import import_module

    from django.apps import apps

    from apps.accounts.models import Role

    role = Role.objects.get(organization=org, system_key="hospital_admin")
    role.permissions.remove(*role.permissions.filter(code="lab.catalog.manage"))
    module = import_module("apps.accounts.migrations.0002_hospital_admin_sidebar_access")
    module.grant(apps, None)
    assert role.permissions.filter(code="lab.catalog.manage").exists()
