"""A doctor sees their own patients and nothing of anyone else's, on every screen."""

import pytest
from django.urls import reverse

from apps.encounters import services as encounter_services
from apps.ipd.models import Admission
from apps.masterdata.models import Service, ServiceCategory
from apps.patients.services import register_patient
from apps.procedures.models import PatientProcedure


@pytest.fixture
def stranger(org, branch, admin_user):
    return register_patient(
        organization=org, branch=branch, user=admin_user, data={"full_name": "Bilal Stranger"}
    )


@pytest.fixture
def mine(org, branch, doctor, patient, make_request):
    encounter, _ = encounter_services.start(request=make_request(doctor.user), patient=patient)
    return encounter


def _procedure(org, branch, patient, user):
    service = Service.objects.create(
        organization=org,
        category=ServiceCategory.objects.create(organization=org, name="Procedures"),
        code="ECG",
        name="ECG",
        price=500,
        is_procedure=True,
    )
    return PatientProcedure.objects.create(
        organization=org,
        branch=branch,
        number="PRC-1",
        patient=patient,
        service=service,
        name="ECG",
        list_price=500,
        unit_price=500,
        created_by=user,
    )


def test_procedures_show_only_own_patients(client, org, branch, doctor, mine, stranger, admin_user):
    other = _procedure(org, branch, stranger, admin_user)
    client.force_login(doctor.user)
    assert list(client.get(reverse("procedures:procedure-list")).context["page_obj"]) == []
    url = reverse("procedures:procedure-detail", args=[other.pk])
    assert client.get(url).status_code == 404
    client.force_login(admin_user)  # hospital-wide roles are unchanged
    assert client.get(url).status_code == 200


def test_consultation_and_vitals_of_others_are_refused(
    client, doctor, mine, stranger, make_user, make_request
):
    from apps.masterdata.models import DoctorProfile

    colleague = DoctorProfile.objects.create(
        organization=doctor.organization,
        user=make_user("doctor", username="drother"),
        department=doctor.department,
        consultation_fee=1000,
        follow_up_fee=500,
        slot_minutes=30,
    )
    theirs, _ = encounter_services.start(request=make_request(colleague.user), patient=stranger)
    client.force_login(doctor.user)
    assert client.get(reverse("encounters:detail", args=[mine.pk])).status_code == 200
    assert client.get(reverse("encounters:detail", args=[theirs.pk])).status_code == 404
    assert client.get(reverse("vitals:record") + f"?patient={stranger.pk}").status_code == 404
    assert client.post(reverse("encounters:start"), {"patient": stranger.pk}).status_code == 404


def test_doctor_gets_no_hospital_overview_or_bed_board(client, doctor):
    client.force_login(doctor.user)
    assert client.get("/?view=overview").status_code == 302
    assert client.get(reverse("ipd:board")).url == reverse("ipd:admission-list")


def test_inpatient_list_and_search_hide_others(client, doctor, mine, stranger):
    client.force_login(doctor.user)
    assert list(client.get(reverse("ipd:admission-list")).context["page_obj"]) == []
    assert Admission.objects.count() == 0
    response = client.get(reverse("search:page"), {"q": "Bilal"})
    assert b"Bilal Stranger" not in response.content
