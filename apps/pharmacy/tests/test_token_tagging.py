"""Reception tokens the doctor never closed in the system show at the pharmacy for tagging, and
counters find a patient by today's token (A-012, a12, 12)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.flow.models import VisitState
from apps.flow.tests.test_flow import _fresh, _visit, clinic, desk  # noqa: F401
from apps.patients.models import Patient
from apps.patients.services import search_patients
from apps.pharmacy import pos
from apps.prescriptions.models import Prescription
from apps.queue.models import QueueToken
from apps.queue.services import token_q

pytestmark = pytest.mark.django_db


@pytest.fixture
def pharma(make_user, make_request):
    return make_request(make_user("pharmacy", username="pharma"))


def test_waiting_token_is_offered_and_tagging_takes_it_from_the_doctor(
    desk, pharma, clinic, patient, cash
):
    visit, v = _visit(desk, patient, clinic, collect=True, method=cash)
    assert _fresh(visit).state == VisitState.IN_QUEUE
    [offered] = pos.visits_without_rx(pharma)
    assert offered["patient"] == patient and offered["waiting"]
    assert offered["token"] == v.token.label

    cart = pos.tag_visit(
        pos.open_cart(pharma), request=pharma, patient=patient, doctor=clinic,
        token=v.token.label.lower(),
    )  # fmt: skip
    assert cart.prescription.status == Prescription.Status.PAPER
    assert _fresh(visit).state == VisitState.PHARMACY_QUEUE
    assert QueueToken.objects.get(pk=v.token.pk).status == QueueToken.Status.COMPLETED
    assert pos.visits_without_rx(pharma) == []


def test_unpaid_token_is_not_offered(desk, pharma, clinic, patient):
    _visit(desk, patient, clinic)
    assert pos.visits_without_rx(pharma) == []


def test_token_of_another_patient_is_refused(desk, pharma, clinic, patient, cash, org, branch):
    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    other = Patient.objects.create(
        organization=org, registered_branch=branch, mrn="X9", full_name="Other", gender="male"
    )
    with pytest.raises(pos.PosError, match="another patient"):
        pos.tag_visit(pos.open_cart(pharma), request=pharma, patient=other, token=v.token.label)


def test_search_finds_todays_token_however_typed(desk, pharma, clinic, patient, cash):
    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    letter, number = v.token.label.split("-")
    patients = Patient.objects.filter(organization=pharma.organization)
    for typed in (v.token.label, f"{letter}{int(number)}", f"{letter.lower()} {number}",
                  f"{letter}{number}", str(int(number)), f"#{int(number)}"):  # fmt: skip
        found, _ = search_patients(patients, typed, pharma)
        assert patient in found, typed
    found, _ = search_patients(patients, f"{letter}-{int(number) + 1}", pharma)
    assert patient not in found


def test_only_todays_tokens_match(desk, pharma, clinic, patient, cash):
    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    QueueToken.objects.filter(pk=v.token.pk).update(date=timezone.localdate() - timedelta(days=1))
    patients = Patient.objects.filter(pk=patient.pk)
    assert not patients.filter(token_q(pharma, v.token.label, field="")).exists()


def test_counter_screens_search_by_token(admin_client, desk, clinic, patient, cash):
    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    label = v.token.label.replace("-", "").lower()
    page = admin_client.get(reverse("pharmacy:rx-queue"), {"q": label}).content.decode()
    assert patient.full_name in page and "Tag at till" in page
    lookup = admin_client.get(reverse("patients:lookup"), {"q": label}).content.decode()
    assert patient.full_name in lookup
    for name in ("lab:worklist", "procedures:procedure-list"):
        assert admin_client.get(reverse(name), {"q": label}).status_code == 200


def test_till_modal_enter_with_one_match_tags_it(admin_client, desk, clinic, patient, cash):
    _, v = _visit(desk, patient, clinic, collect=True, method=cash)
    modal = admin_client.get(
        reverse("pharmacy:pos-prescription"), {"q": v.token.label, "pick": "1"}
    )
    assert modal.context["auto_pick"] == "visit"
    assert f'"token": "{v.token.label}"' in modal.content.decode()
