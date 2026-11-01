"""A category typed once (city, relation...) is offered next time, within the same hospital only."""

import pytest
from django.template import Context, Template

from apps.organizations.models import Organization
from apps.patients.models import Patient

pytestmark = pytest.mark.django_db


def _list(org):
    class Request:
        organization = org

    return Template('{% load ui %}{% past_values "patients.Patient.city" %}').render(
        Context({"request": Request()})
    )


def test_city_is_offered_once_and_only_to_its_hospital(org, patient):
    Patient.objects.filter(pk=patient.pk).update(city="Chakwal")
    html = _list(org)
    assert 'id="past-patients-patient-city"' in html and html.count('value="Chakwal"') == 1
    other = Organization.objects.create(name="Other")
    assert "Chakwal" not in _list(other)
    assert html.count('value="Lahore"') == 1  # from the standard list


def test_a_typed_city_matching_the_standard_list_shows_once(org, patient):
    Patient.objects.filter(pk=patient.pk).update(city="lahore")
    assert _list(org).lower().count('value="lahore"') == 1


def test_new_patient_page_carries_the_list(admin_client, patient):
    Patient.objects.filter(pk=patient.pk).update(city="Lahore")
    html = admin_client.get("/patients/register/").content.decode()
    assert 'data-list="past-patients-patient-city"' in html and 'value="Lahore"' in html
