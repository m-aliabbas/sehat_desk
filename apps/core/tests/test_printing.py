"""Shared print engine (Phase 24): branding, paper sizes and the new slips/forms."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.appointments.tests.test_appointments import _appt
from apps.lab import services as lab
from apps.lab.tests.test_lab import _req, catalogue, tech  # noqa: F401 (fixtures)


@pytest.fixture
def branded(org):
    org.address = "12 Jail Road, Lahore"
    org.phone = "042-111-222-333"
    org.registration_number = "PHC-7788"
    org.report_header = "Quality care since 1998"
    org.report_footer = "Open 24/7 for emergencies"
    org.print_terms = "Fees are not refundable after consultation."
    org.save()
    return org


@pytest.fixture
def appt(org, branch, patient, doctor, admin_user):
    return _appt(org, branch, patient, doctor, admin_user, timezone.localdate() + timedelta(days=2))


def test_appointment_slip_is_branded_a5(admin_client, branded, appt):
    response = admin_client.get(reverse("appointments:slip", args=[appt.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert "size: A5" in html
    for text in (branded.name, "12 Jail Road", "042-111-222-333", "PHC-7788",
                 "Quality care since 1998", "Open 24/7", "Appointment slip", appt.number,
                 appt.patient.mrn, "09:00"):  # fmt: skip
        assert text in html, text
    assert "not refundable" not in html  # terms only on money documents


def test_appointment_slip_needs_permission_and_scope(client, make_user, appt, org):
    from apps.organizations.models import Branch, Organization

    client.force_login(make_user("lab"))
    assert client.get(reverse("appointments:slip", args=[appt.pk])).status_code == 403
    from apps.accounts.services import seed_default_roles

    other = Organization.objects.create(name="Elsewhere")
    seed_default_roles(other)
    Branch.objects.create(organization=other, name="Other main", code="OTH", is_main=True)
    stranger = make_user("super_admin", organization=other)
    client.force_login(stranger)
    assert client.get(reverse("appointments:slip", args=[appt.pk])).status_code == 404


def test_lab_order_form(client, rf, tech, patient, catalogue, org, branch, branded):
    order = lab.create_order(
        request=_req(rf, tech, org, branch), patient=patient, tests=[catalogue["hb"]]
    )
    client.force_login(tech)
    response = client.get(reverse("lab:order-print", args=[order.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert order.number in html and "Haemoglobin" in html and "Lab order" in html
    assert order.samples.first().accession in html


def test_lab_order_form_hidden_from_reception(
    client, make_user, rf, tech, patient, catalogue, org, branch
):
    order = lab.create_order(
        request=_req(rf, tech, org, branch), patient=patient, tests=[catalogue["hb"]]
    )
    client.force_login(make_user("reception"))
    assert client.get(reverse("lab:order-print", args=[order.pk])).status_code == 403


def test_invoice_prints_terms_and_letterhead(
    admin_client, branded, rf, tech, patient, catalogue, org, branch
):
    order = lab.create_order(
        request=_req(rf, tech, org, branch), patient=patient, tests=[catalogue["hb"]]
    )
    html = admin_client.get(
        reverse("billing:invoice-print", args=[order.invoice_id])
    ).content.decode()
    assert "size: A4" in html
    assert "not refundable" in html and "PHC-7788" in html and order.invoice.number in html


def test_thermal_layout_uses_roll_paper():
    # The layout itself: thermal sheets keep their padding and use roll paper.
    from django.template.loader import render_to_string

    html = render_to_string("print/thermal.html", {})
    assert "size: 80mm auto" in html


def test_staging_prints_are_marked(admin_client, settings, appt):
    settings.SEHATDESK_ENV = "staging"
    html = admin_client.get(reverse("appointments:slip", args=[appt.pk])).content.decode()
    assert "Staging test print" in html
    settings.SEHATDESK_ENV = "production"
    html = admin_client.get(reverse("appointments:slip", args=[appt.pk])).content.decode()
    assert "Staging test print" not in html


def test_print_terms_editable_in_hospital_settings(admin_client, org):
    response = admin_client.get(reverse("organizations:hospital"))
    assert b'name="print_terms"' in response.content
