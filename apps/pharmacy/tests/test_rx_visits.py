"""Reception visits at the pharmacy: a paper prescription can be tagged to the sale."""

import pytest
from django.urls import reverse

from apps.pharmacy import pos
from apps.pharmacy.tests.test_pos import paracetamol, sent_rx  # noqa: F401
from apps.prescriptions.models import Prescription
from apps.queue import services as queue
from apps.queue.models import QueueToken

pytestmark = pytest.mark.django_db


@pytest.fixture
def req(make_request, admin_user):
    return make_request(admin_user)


def _token(req, patient, doctor, status=QueueToken.Status.COMPLETED):
    token = queue.issue_token(request=req, patient=patient, doctor=doctor,
                              department=doctor.department)  # fmt: skip
    QueueToken.objects.filter(pk=token.pk).update(status=status)
    return token


@pytest.fixture
def visit(req, patient, doctor):
    """The doctor has seen the patient (and may have written on paper)."""
    return _token(req, patient, doctor)


def test_a_cancelled_token_is_not_offered(req, patient, doctor):
    """A patient still waiting is offered now (the doctor may write on paper and never close
    the token; apps/pharmacy/tests/test_token_tagging.py). A cancelled visit is not."""
    _token(req, patient, doctor, status=QueueToken.Status.CANCELLED)
    assert pos.visits_without_rx(req) == []


def test_tagging_a_visit_attaches_the_doctors_sent_prescription(req, patient, doctor, sent_rx):
    """Open at till / Tag at till with a signed prescription waiting: that one, not a paper Rx."""
    cart = pos.tag_visit(pos.open_cart(req), request=req, patient=patient, doctor=doctor)
    assert cart.prescription == sent_rx
    assert not Prescription.objects.filter(patient=patient, status="paper").exists()


def test_visit_shows_until_tagged_then_goes(req, visit, patient, doctor):
    assert [v["patient"] for v in pos.visits_without_rx(req)] == [patient]
    cart = pos.open_cart(req)
    cart = pos.tag_visit(cart, request=req, patient=patient, doctor=doctor)
    assert cart.patient == patient
    assert cart.prescription.status == Prescription.Status.PAPER
    assert pos.visits_without_rx(req) == []


def test_tag_refuses_another_patients_sale(req, visit, patient, doctor, org, branch):
    from apps.patients.models import Patient

    other = Patient.objects.create(
        organization=org, registered_branch=branch, mrn="X1", full_name="Other", gender="male"
    )
    cart = pos.open_cart(req)
    pos.attach_patient(cart, request=req, patient=other)
    with pytest.raises(pos.PosError, match="Other"):
        pos.tag_visit(cart, request=req, patient=patient, doctor=doctor)


def test_rx_page_lists_and_tags_the_visit(admin_client, visit, patient, doctor):
    page = admin_client.get(reverse("pharmacy:rx-queue")).content.decode()
    assert "Tag at till" in page and patient.full_name in page
    response = admin_client.post(
        reverse("pharmacy:pos-prescription"),
        {"action": "visit", "patient": patient.pk, "doctor": doctor.pk},
    )
    assert response.status_code == 302 and response.url == reverse("pharmacy:pos")
    assert Prescription.objects.filter(patient=patient, status="paper").exists()
