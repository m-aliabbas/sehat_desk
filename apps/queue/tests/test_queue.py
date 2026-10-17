from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.audit.models import AuditLog
from apps.queue import services
from apps.queue.models import Counter, QueueToken

Status = QueueToken.Status


@pytest.fixture
def desk_request(rf, admin_user, org, branch):
    """A request-like object for calling services directly."""
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = admin_user, org, branch
    req.sd_perms = effective_permissions(admin_user, branch)
    return req


@pytest.fixture
def make_patient(org, branch, admin_user):
    from apps.patients.services import register_patient

    def _make(name):
        return register_patient(
            organization=org, branch=branch, user=admin_user, data={"full_name": name}
        )

    return _make


def _today_appt(org, branch, patient, doctor, user, start=time(13, 30)):
    return Appointment.objects.create(
        organization=org, branch=branch, number=f"A-{Appointment.objects.count()}", patient=patient,
        doctor=doctor, department=doctor.department, date=timezone.localdate(), start_time=start,
        end_time=start, fee=Decimal("0"), created_by=user,
    )  # fmt: skip


# --- Numbering and priority --------------------------------------------------------------------


def test_tokens_number_per_department_per_day(desk_request, doctor, department, make_patient, org):
    from apps.masterdata.models import Department

    department.token_prefix = "A"
    department.save()
    cardio = Department.objects.create(
        organization=org, name="Cardiology", code="CARD", token_prefix="B"
    )
    t1 = services.issue_token(
        request=desk_request, patient=make_patient("P1"), department=department, doctor=doctor
    )
    t2 = services.issue_token(
        request=desk_request, patient=make_patient("P2"), department=department
    )
    t3 = services.issue_token(request=desk_request, patient=make_patient("P3"), department=cardio)
    assert [t1.label, t2.label, t3.label] == ["A-001", "A-002", "B-001"]


def test_department_prefix_defaults_to_code_letter(desk_request, department, patient):
    assert (
        services.issue_token(request=desk_request, patient=patient, department=department).label
        == "G-001"
    )


def test_priority_order(desk_request, doctor, department, make_patient):
    normal = services.issue_token(
        request=desk_request, patient=make_patient("N"), department=department, doctor=doctor
    )
    urgent = services.issue_token(
        request=desk_request,
        patient=make_patient("U"),
        department=department,
        doctor=doctor,
        priority="urgent",
    )
    emergency = services.issue_token(
        request=desk_request,
        patient=make_patient("E"),
        department=department,
        doctor=doctor,
        priority="emergency",
    )
    order = list(services.waiting(services.queue_for(desk_request.branch, doctor=doctor)))
    assert order == [emergency, urgent, normal]


def test_special_priority_needs_permission(desk_request, department, patient):
    desk_request.sd_perms = desk_request.sd_perms - {"priority.special"}
    with pytest.raises(services.QueueError):
        services.issue_token(
            request=desk_request, patient=patient, department=department, priority="special"
        )


def test_patient_cannot_hold_two_open_tokens_in_same_clinic(desk_request, department, patient):
    services.issue_token(request=desk_request, patient=patient, department=department)
    with pytest.raises(services.QueueError, match="already holds"):
        services.issue_token(request=desk_request, patient=patient, department=department)


# --- Lifecycle ---------------------------------------------------------------------------------


def test_full_lifecycle_moves_appointment(
    desk_request, org, branch, doctor, department, patient, admin_user
):
    appt = _today_appt(org, branch, patient, doctor, admin_user)
    token = services.issue_token(
        request=desk_request,
        patient=patient,
        department=department,
        doctor=doctor,
        appointment=appt,
    )
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.IN_QUEUE and appt.checked_in_at
    token = services.call_next(request=desk_request, doctor=doctor)
    assert token.status == Status.CALLED and token.call_count == 1
    token = services.recall(token, request=desk_request)
    assert token.call_count == 2
    services.start(token)
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.WITH_DOCTOR
    services.complete(token)
    appt.refresh_from_db()
    assert appt.status == Appointment.Status.COMPLETED and token.completed_at


def test_must_finish_current_before_calling_next(desk_request, doctor, department, make_patient):
    for n in ("A", "B"):
        services.issue_token(
            request=desk_request, patient=make_patient(n), department=department, doctor=doctor
        )
    services.call_next(request=desk_request, doctor=doctor)
    with pytest.raises(services.QueueError, match="Finish"):
        services.call_next(request=desk_request, doctor=doctor)


def test_department_queue_token_is_claimed_by_calling_doctor(
    desk_request, doctor, department, patient
):
    token = services.issue_token(request=desk_request, patient=patient, department=department)
    called = services.call_next(request=desk_request, doctor=doctor)
    assert called.pk == token.pk and called.doctor == doctor


def test_skip_and_requeue_goes_to_back_of_line(desk_request, doctor, department, make_patient):
    first = services.issue_token(
        request=desk_request, patient=make_patient("A"), department=department, doctor=doctor
    )
    second = services.issue_token(
        request=desk_request, patient=make_patient("B"), department=department, doctor=doctor
    )
    services.call_next(request=desk_request, doctor=doctor)
    services.skip(QueueToken.objects.get(pk=first.pk))
    services.requeue(QueueToken.objects.get(pk=first.pk))
    order = list(services.waiting(services.queue_for(desk_request.branch, doctor=doctor)))
    assert order == [second, QueueToken.objects.get(pk=first.pk)]


def test_cancel_requires_reason_and_cancels_appointment(
    desk_request, org, branch, doctor, department, patient, admin_user
):
    appt = _today_appt(org, branch, patient, doctor, admin_user)
    token = services.issue_token(
        request=desk_request,
        patient=patient,
        department=department,
        doctor=doctor,
        appointment=appt,
    )
    with pytest.raises(services.QueueError):
        services.cancel(token, reason=" ")
    services.cancel(token, reason="Patient left")
    appt.refresh_from_db()
    assert (
        appt.status == Appointment.Status.CANCELLED and "Patient left" in appt.cancellation_reason
    )


def test_invalid_transitions_rejected(desk_request, department, patient):
    token = services.issue_token(request=desk_request, patient=patient, department=department)
    with pytest.raises(services.QueueError):
        services.start(token)
    with pytest.raises(services.QueueError):
        services.requeue(token)


def test_stats_average_wait(desk_request, doctor, department, patient):
    token = services.issue_token(
        request=desk_request, patient=patient, department=department, doctor=doctor
    )
    QueueToken.objects.filter(pk=token.pk).update(created_at=timezone.now() - timedelta(minutes=12))
    services.call_next(request=desk_request, doctor=doctor)
    s = services.stats(desk_request.branch)
    assert s["avg_wait_minutes"] == 12 and s["issued"] == 1 and s["called"] == 1


# --- Views and permissions ---------------------------------------------------------------------


def test_appointment_check_in_issues_token(admin_client, org, branch, doctor, patient, admin_user):
    appt = _today_appt(org, branch, patient, doctor, admin_user)
    admin_client.post(f"/appointments/{appt.pk}/check-in/")
    assert QueueToken.objects.get(appointment=appt).status == Status.WAITING
    assert AuditLog.objects.filter(action="queue.issue").exists()


def test_walk_in_with_doctor_records_visit(admin_client, doctor, department, patient):
    response = admin_client.post(
        "/reception/walk-in/",
        {
            "patient": patient.pk,
            "department": department.pk,
            "doctor": doctor.pk,
            "priority": "normal",
        },
    )
    token = QueueToken.objects.get()
    assert response.url == f"/reception/?issued={token.pk}"
    assert token.appointment.visit_type == Appointment.VisitType.WALK_IN
    assert token.appointment.status == Appointment.Status.IN_QUEUE


def test_walk_in_department_only(admin_client, department, patient):
    admin_client.post(
        "/reception/walk-in/",
        {"patient": patient.pk, "department": department.pk, "priority": "urgent"},
    )
    token = QueueToken.objects.get()
    assert token.doctor is None and token.appointment is None and token.priority == "urgent"


def test_walk_in_blocked_when_doctor_on_leave(admin_client, doctor, department, patient):
    from apps.masterdata.models import DoctorLeave

    today = timezone.localdate()
    DoctorLeave.objects.create(doctor=doctor, start_date=today, end_date=today)
    response = admin_client.post(
        "/reception/walk-in/",
        {
            "patient": patient.pk,
            "department": department.pk,
            "doctor": doctor.pk,
            "priority": "normal",
        },
    )
    assert b"on leave" in response.content and not QueueToken.objects.exists()


def test_reception_desk_and_live_refresh(admin_client, doctor, department, patient, desk_request):
    services.issue_token(
        request=desk_request, patient=patient, department=department, doctor=doctor
    )
    page = admin_client.get("/reception/", {"clinic": f"doc-{doctor.pk}"})
    assert page.status_code == 200 and b"G-001" in page.content
    live = admin_client.get(
        "/reception/",
        {"clinic": f"doc-{doctor.pk}"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="desk-live",
    )
    assert b"<html" not in live.content and b"G-001" in live.content


def test_doctor_can_operate_only_own_queue(
    client, make_user, org, branch, doctor, department, desk_request, make_patient
):
    from apps.masterdata.models import DoctorProfile

    token = services.issue_token(
        request=desk_request, patient=make_patient("A"), department=department, doctor=doctor
    )
    other_user = make_user("doctor", username="drother")
    other = DoctorProfile.objects.create(organization=org, user=other_user, department=department)
    client.force_login(other_user)
    assert client.post(f"/queue/tokens/{token.pk}/call/", {"doctor": doctor.pk}).status_code == 403
    assert client.post("/queue/call-next/", {"doctor": doctor.pk}).status_code == 403
    client.force_login(doctor.user)
    client.post("/queue/call-next/")
    token.refresh_from_db()
    assert token.status == Status.CALLED and token.called_by == doctor.user
    assert other  # profile exists


def test_doctor_queue_page(client, doctor, desk_request, department, patient):
    services.issue_token(
        request=desk_request, patient=patient, department=department, doctor=doctor
    )
    client.force_login(doctor.user)
    response = client.get("/queue/doctor/")
    assert response.status_code == 200 and len(response.context["waiting"]) == 1


def test_queue_screens_need_permission(client, make_user):
    client.force_login(make_user("lab"))
    assert client.get("/reception/").status_code == 403
    assert client.get("/queue/doctor/").status_code == 403


def test_token_slip(admin_client, desk_request, department, patient):
    token = services.issue_token(request=desk_request, patient=patient, department=department)
    response = admin_client.get(f"/queue/tokens/{token.pk}/slip/")
    assert b"G-001" in response.content and patient.mrn.encode() in response.content


def test_counters(admin_client, branch):
    admin_client.post("/reception/counters/", {"code": "01", "name": "Main reception"})
    counter = Counter.objects.get(branch=branch)
    admin_client.post(f"/reception/counters/{counter.pk}/toggle/")
    counter.refresh_from_db()
    assert counter.is_open and counter.assigned_staff is not None


# --- Public display ----------------------------------------------------------------------------


def test_public_display_needs_valid_key_and_hides_names(
    client, admin_client, branch, desk_request, doctor, department, patient
):
    assert client.get("/display/not-a-real-key-at-all-xx/").status_code == 404
    admin_client.post("/reception/display-key/")
    branch.refresh_from_db()
    services.issue_token(
        request=desk_request, patient=patient, department=department, doctor=doctor
    )
    services.call_next(request=desk_request, doctor=doctor)
    response = client.get(f"/display/{branch.display_key}/")  # anonymous
    assert response.status_code == 200
    assert b"G-001" in response.content
    assert (
        patient.full_name.encode() not in response.content
        and patient.mrn.encode() not in response.content
    )
    assert response["Cache-Control"] == "no-store"


def test_display_key_regeneration_revokes_old_link(client, admin_client, branch):
    admin_client.post("/reception/display-key/")
    branch.refresh_from_db()
    old = branch.display_key
    admin_client.post("/reception/display-key/")
    assert client.get(f"/display/{old}/").status_code == 404


def test_tokens_from_other_branch_are_404(
    admin_client, org, branch2, department, patient, admin_user
):
    token = QueueToken.objects.create(
        organization=org, branch=branch2, date=timezone.localdate(), department=department,
        patient=patient, number=1, label="G-001", issued_by=admin_user,
    )  # fmt: skip
    assert admin_client.post(f"/queue/tokens/{token.pk}/skip/").status_code == 404
    assert admin_client.get(f"/queue/tokens/{token.pk}/slip/").status_code == 404
