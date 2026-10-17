from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.audit.models import AuditLog
from apps.encounters import services
from apps.encounters.models import Encounter, EncounterVersion
from apps.queue import services as queue_services
from apps.queue.models import QueueToken

Status = Encounter.Status


def _req(rf, user, org, branch):
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    return req


@pytest.fixture
def doc_request(rf, doctor, org, branch):
    return _req(rf, doctor.user, org, branch)


@pytest.fixture
def desk_request(rf, admin_user, org, branch):
    return _req(rf, admin_user, org, branch)


@pytest.fixture
def called_token(desk_request, doctor, department, patient):
    token = queue_services.issue_token(
        request=desk_request, patient=patient, department=department, doctor=doctor
    )
    return queue_services.call(token, request=desk_request, doctor=doctor)


def _appt(org, branch, patient, doctor, user, status=Appointment.Status.CHECKED_IN):
    return Appointment.objects.create(
        organization=org, branch=branch, number=f"A-{Appointment.objects.count() + 1}",
        patient=patient, doctor=doctor, department=doctor.department,
        date=timezone.localdate(), start_time=time(9), end_time=time(9, 30), fee=Decimal("0"),
        created_by=user, status=status, reason="Fever for 3 days",
    )  # fmt: skip


NOTE = {"chief_complaint": "Fever", "provisional_diagnosis": "Viral fever"}


# --- Start -------------------------------------------------------------------------------------


def test_start_from_token_moves_queue_and_numbers_by_year(doc_request, called_token, patient):
    enc, created = services.start(request=doc_request, patient=patient, token=called_token)
    called_token.refresh_from_db()
    assert created and enc.status == Status.DRAFT
    assert enc.number == f"VIS-{timezone.localdate().year}-00001"
    assert called_token.status == QueueToken.Status.IN_CONSULTATION
    again, created = services.start(request=doc_request, patient=patient, token=called_token)
    assert again.pk == enc.pk and not created


def test_start_needs_a_called_token(doc_request, desk_request, doctor, department, patient):
    token = queue_services.issue_token(
        request=desk_request, patient=patient, department=department, doctor=doctor
    )
    with pytest.raises(services.EncounterError, match="Call"):
        services.start(request=doc_request, patient=patient, token=token)


def test_only_doctors_start(desk_request, patient):
    with pytest.raises(services.EncounterError, match="Only a doctor"):
        services.start(request=desk_request, patient=patient)


def test_start_from_appointment_and_patient_continues_draft(
    doc_request, org, branch, patient, doctor, admin_user
):
    appt = _appt(org, branch, patient, doctor, admin_user, Appointment.Status.SCHEDULED)
    enc, _ = services.start(request=doc_request, patient=patient, appointment=appt)
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.WITH_DOCTOR and enc.visit_reason == "Fever for 3 days"
    direct, created = services.start(request=doc_request, patient=patient)
    assert direct.pk == enc.pk and not created  # one open draft per patient per doctor


# --- Save, complete, reopen --------------------------------------------------------------------


def test_save_note_and_revision_conflict(doc_request, patient):
    enc, _ = services.start(request=doc_request, patient=patient)
    enc = services.save_note(enc, request=doc_request, data=NOTE, revision=0)
    assert enc.revision == 1 and enc.chief_complaint == "Fever"
    with pytest.raises(services.ConflictError):
        services.save_note(enc, request=doc_request, data={"chief_complaint": "x"}, revision=0)


def test_other_doctor_cannot_edit(doc_request, rf, org, branch, department, make_user, patient):
    from apps.masterdata.models import DoctorProfile

    other = make_user("doctor")
    DoctorProfile.objects.create(organization=org, user=other, department=department)
    enc, _ = services.start(request=doc_request, patient=patient)
    with pytest.raises(services.EncounterError, match="treating doctor"):
        services.save_note(enc, request=_req(rf, other, org, branch), data=NOTE)


def test_complete_requires_minimum_note(doc_request, patient):
    enc, _ = services.start(request=doc_request, patient=patient)
    with pytest.raises(services.EncounterError, match="chief complaint"):
        services.complete(enc, request=doc_request)
    services.save_note(enc, request=doc_request, data={"chief_complaint": "Fever"})
    with pytest.raises(services.EncounterError, match="diagnosis"):
        services.complete(enc, request=doc_request)
    services.save_note(
        enc,
        request=doc_request,
        data={**NOTE, "referral_to": "Cardiology", "follow_up_date": None},
    )
    with pytest.raises(services.EncounterError, match="referral"):
        services.complete(enc, request=doc_request)


def test_complete_signs_version_and_closes_token(doc_request, called_token, patient):
    enc, _ = services.start(request=doc_request, patient=patient, token=called_token)
    services.save_note(enc, request=doc_request, data=NOTE)
    v = services.complete(enc, request=doc_request)
    enc.refresh_from_db()
    called_token.refresh_from_db()
    assert (v.version, v.content["chief_complaint"], v.signed_by) == (1, "Fever", doc_request.user)
    assert enc.status == Status.COMPLETED and enc.completed_at
    assert called_token.status == QueueToken.Status.COMPLETED
    with pytest.raises(services.EncounterError, match="completed"):
        services.save_note(enc, request=doc_request, data={"chief_complaint": "Changed"})


def test_appointment_completed_with_encounter(
    doc_request, org, branch, patient, doctor, admin_user
):
    appt = _appt(org, branch, patient, doctor, admin_user)
    enc, _ = services.start(request=doc_request, patient=patient, appointment=appt)
    services.save_note(enc, request=doc_request, data=NOTE)
    services.complete(enc, request=doc_request)
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.COMPLETED


def test_reopen_keeps_signed_versions(doc_request, patient):
    enc, _ = services.start(request=doc_request, patient=patient)
    services.save_note(enc, request=doc_request, data=NOTE)
    services.complete(enc, request=doc_request)
    with pytest.raises(services.EncounterError, match="reason"):
        services.reopen(enc, request=doc_request, reason=" ")
    enc = services.reopen(enc, request=doc_request, reason="Wrong diagnosis typed")
    assert enc.is_amendment
    services.save_note(enc, request=doc_request, data={"final_diagnosis": "Dengue fever"})
    v2 = services.complete(enc, request=doc_request)
    v1 = EncounterVersion.objects.get(encounter=enc, version=1)
    assert v1.content["final_diagnosis"] == ""  # the original is untouched
    assert v2.content["final_diagnosis"] == "Dengue fever"
    assert v2.amendment_reason == "Wrong diagnosis typed"
    assert v2.changed_from(v1) == ["Final diagnosis"]


def test_reopen_needs_permission(rf, doc_request, patient, org, branch, doctor):
    from apps.accounts.models import Permission

    enc, _ = services.start(request=doc_request, patient=patient)
    services.save_note(enc, request=doc_request, data=NOTE)
    services.complete(enc, request=doc_request)
    role = org.roles.get(system_key="doctor")
    role.permissions.remove(Permission.objects.get(code="consultation.reopen"))
    with pytest.raises(services.EncounterError, match="not allowed"):
        services.reopen(enc, request=_req(rf, doctor.user, org, branch), reason="x")


def test_follow_ups_due(doc_request, patient, doctor):
    enc, _ = services.start(request=doc_request, patient=patient)
    services.save_note(
        enc,
        request=doc_request,
        data={**NOTE, "follow_up_date": timezone.localdate() + timedelta(days=3)},
    )
    services.complete(enc, request=doc_request)
    assert services.follow_ups(doctor) == [enc]


# --- Views -------------------------------------------------------------------------------------


def test_doctor_flow_through_views(client, doctor, called_token, patient):
    client.force_login(doctor.user)
    assert client.get("/opd/").status_code == 200
    r = client.post("/encounters/start/", {"token": called_token.pk})
    enc = Encounter.objects.get()
    assert r["Location"] == f"/encounters/{enc.pk}/"
    assert client.get(f"/encounters/{enc.pk}/").status_code == 200

    r = client.post(
        f"/encounters/{enc.pk}/autosave/",
        {"chief_complaint": "Cough", "revision": "0"},
        HTTP_HX_REQUEST="true",
    )
    enc.refresh_from_db()
    assert b"Draft saved" in r.content and enc.chief_complaint == "Cough" and enc.revision == 1
    r = client.post(
        f"/encounters/{enc.pk}/autosave/",
        {"chief_complaint": "Stale tab", "revision": "0"},
        HTTP_HX_REQUEST="true",
    )
    assert b"another window" in r.content

    client.post(
        f"/encounters/{enc.pk}/complete/",
        {"chief_complaint": "Cough", "provisional_diagnosis": "URTI", "revision": "1"},
    )
    enc.refresh_from_db()
    assert enc.status == Status.COMPLETED
    page = client.get(f"/encounters/{enc.pk}/").content.decode()
    assert "URTI" in page and "<textarea" not in page  # read-only after signing

    client.post(f"/encounters/{enc.pk}/reopen/", {"reason": "Add exam findings"})
    enc.refresh_from_db()
    assert enc.is_amendment
    assert client.get(f"/encounters/{enc.pk}/versions/").status_code == 200
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {
        "encounter.start",
        "encounter.view",
        "encounter.complete",
        "encounter.reopen",
    } <= actions
    # Clinical text never goes into the audit log.
    assert not AuditLog.objects.filter(summary__icontains="Cough").exists()
    assert not AuditLog.objects.filter(summary__icontains="URTI").exists()


def test_reception_cannot_open_consultations(client, make_user, doc_request, patient):
    enc, _ = services.start(request=doc_request, patient=patient)
    client.force_login(make_user("reception"))
    assert client.get(f"/encounters/{enc.pk}/").status_code == 403
    assert client.get("/opd/").status_code == 403
    assert client.post("/encounters/start/", {"patient": patient.pk}).status_code == 403


def test_nurse_reads_but_cannot_write(client, make_user, doc_request, patient):
    enc, _ = services.start(request=doc_request, patient=patient)
    client.force_login(make_user("nurse"))
    page = client.get(f"/encounters/{enc.pk}/").content.decode()
    assert "Read-only" in page and "<textarea" not in page
    assert client.post(f"/encounters/{enc.pk}/autosave/", {}).status_code == 403


def test_encounter_in_other_branch_is_404(admin_client, org, branch2, patient, doctor, admin_user):
    enc = Encounter.objects.create(
        organization=org, branch=branch2, number="VIS-X", patient=patient, doctor=doctor,
        department=doctor.department, created_by=admin_user,
    )  # fmt: skip
    assert admin_client.get(f"/encounters/{enc.pk}/").status_code == 404


def test_patient_profile_start_button(client, doctor, patient):
    client.force_login(doctor.user)
    page = client.get(f"/patients/{patient.pk}/").content.decode()
    assert "Start consultation" in page
    r = client.post("/encounters/start/", {"patient": patient.pk})
    assert r.status_code == 302 and Encounter.objects.filter(patient=patient).exists()
    assert client.get(f"/patients/{patient.pk}/?tab=visits").status_code == 200
    assert client.get(f"/patients/{patient.pk}/?tab=timeline").status_code == 200
