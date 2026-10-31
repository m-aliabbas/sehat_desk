"""The doctor's home: their queue, one Next button, and only their own patients."""

import pytest
from django.urls import reverse

from apps.appointments.models import Appointment
from apps.prescriptions import services as rx_services
from apps.queue import services
from apps.queue.models import QueueToken

pytestmark = pytest.mark.django_db


@pytest.fixture
def doctor_user(doctor):
    return doctor.user


@pytest.fixture
def doc_client(client, doctor_user):
    client.force_login(doctor_user)
    return client


def _token(make_request, admin_user, patient, doctor):
    return services.issue_token(
        request=make_request(admin_user), patient=patient, doctor=doctor,
        department=doctor.department,
    )  # fmt: skip


def test_home_sends_a_doctor_to_their_queue(doc_client):
    response = doc_client.get(reverse("core:dashboard"))
    assert response.status_code == 302 and response.url == reverse("queue:doctor")


def test_queue_shows_counters_and_next_button(
    doc_client, make_request, admin_user, patient, doctor
):
    token = _token(make_request, admin_user, patient, doctor)
    services.call(token, request=make_request(doctor.user), doctor=doctor)
    html = doc_client.get(reverse("queue:doctor")).content.decode()
    assert "Checked today" in html and "Checked · Next patient" in html


def test_next_signs_an_open_consultation_and_closes_the_token(
    doc_client, make_request, admin_user, patient, doctor
):
    from apps.encounters import services as encounters

    token = _token(make_request, admin_user, patient, doctor)
    request = make_request(doctor.user)
    services.call(token, request=request, doctor=doctor)
    encounter, _ = encounters.start(request=request, patient=patient, token=token)
    rx_item = rx_services.add_item(encounter, request=request, data={"medicine_name": "Panadol"})
    response = doc_client.post(reverse("queue:complete-next", args=[token.pk]))
    assert response.status_code == 302
    encounter.refresh_from_db()
    assert encounter.status == "completed"
    assert QueueToken.objects.get(pk=token.pk).status == QueueToken.Status.COMPLETED
    rx_item.prescription.refresh_from_db()
    assert rx_item.prescription.sent_to_pharmacy_at is not None


def test_doctor_cannot_open_another_doctors_patient(doc_client, make_request, admin_user, patient):
    assert doc_client.get(reverse("patients:detail", args=[patient.pk])).status_code == 404


def test_doctor_sees_own_patient_after_a_token(
    doc_client, make_request, admin_user, patient, doctor
):
    _token(make_request, admin_user, patient, doctor)
    assert doc_client.get(reverse("patients:detail", args=[patient.pk])).status_code == 200
    assert Appointment.objects.count() == 0
