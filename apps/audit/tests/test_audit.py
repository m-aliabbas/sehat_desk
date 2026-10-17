from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.accounts.services import effective_permissions
from apps.audit import services
from apps.audit.models import AuditLog, AuditLogImmutable
from apps.encounters import services as encounters
from apps.organizations.models import Organization


def _req(rf, user, org, branch):
    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    req.META["REMOTE_ADDR"] = "127.0.0.1"
    return req


# --- Append-only --------------------------------------------------------------------------------


def test_events_cannot_be_changed_or_deleted(rf, admin_user, org, branch):
    entry = services.record(_req(rf, admin_user, org, branch), "staff.update", admin_user)
    entry.summary = "rewritten"
    with pytest.raises(AuditLogImmutable):
        entry.save()
    with pytest.raises(AuditLogImmutable):
        entry.delete()
    with pytest.raises(AuditLogImmutable):
        AuditLog.objects.filter(pk=entry.pk).update(summary="x")
    with pytest.raises(AuditLogImmutable):
        AuditLog.objects.all().delete()


@pytest.mark.parametrize(
    "sql", ["UPDATE audit_auditlog SET summary = 'x'", "DELETE FROM audit_auditlog"]
)
def test_database_refuses_direct_sql(rf, admin_user, org, branch, sql):
    services.record(_req(rf, admin_user, org, branch), "staff.update", admin_user)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql)
    assert AuditLog.objects.filter(action="staff.update").exists()


# --- Events -------------------------------------------------------------------------------------


def test_login_is_audited(client, admin_user):
    client.force_login(admin_user)
    assert AuditLog.objects.filter(action="auth.login", actor=admin_user).exists()


def test_note_edits_are_audited_by_field_without_text(rf, doctor, org, branch, patient):
    req = _req(rf, doctor.user, org, branch)
    enc, _ = encounters.start(request=req, patient=patient)
    enc = encounters.save_note(enc, request=req, data={"chief_complaint": "Severe headache"})
    enc = encounters.save_note(enc, request=req, data={"chief_complaint": "Severe headache x2"})
    events = AuditLog.objects.filter(action="encounter.note_save", record_id=str(enc.pk))
    assert events.count() == 1  # autosave of the same field is coalesced
    assert events.get().changes["fields"] == ["chief_complaint"]
    assert "headache" not in str(events.get().changes) + events.get().summary
    encounters.save_note(enc, request=req, data={"provisional_diagnosis": "Migraine"})
    assert events.count() == 2  # a new field is a new event


def test_prescription_advice_is_audited(client, doctor, patient, org, branch, rf):
    enc, _ = encounters.start(request=_req(rf, doctor.user, org, branch), patient=patient)
    client.force_login(doctor.user)
    client.post(f"/encounters/{enc.pk}/prescription/advice/", {"advice": "Plenty of fluids"})
    log = AuditLog.objects.get(action="prescription.advice")
    assert log.changes == {"fields": ["advice"]} and log.actor == doctor.user


def test_ai_acceptance_helper(rf, doctor, org, branch, patient):
    req = _req(rf, doctor.user, org, branch)
    enc, _ = encounters.start(request=req, patient=patient)
    services.record_ai_acceptance(
        req, enc, feature="note_draft", draft_id=7, fields=["history_of_present_illness"]
    )
    log = AuditLog.objects.get(action="ai.draft_accept")
    assert log.changes["feature"] == "note_draft" and log.entity == "Encounter"


# --- Viewer -------------------------------------------------------------------------------------


def test_viewer_permission(client, make_user, branch):
    client.force_login(make_user("reception"))
    assert client.get("/settings/audit/").status_code == 403
    client.force_login(make_user("hospital_admin"))
    response = client.get("/settings/audit/")
    assert response.status_code == 200
    assert b"Audit trail" in response.content
    # Read-only: the viewer accepts no writes at all.
    assert client.post("/settings/audit/").status_code == 405


def test_viewer_filters_and_org_isolation(rf, admin_client, admin_user, make_user, org, branch):
    nurse = make_user("nurse", first_name="Hina")
    services.record(_req(rf, nurse, org, branch), "patient.update", summary="MRN-1 phone")
    services.record(_req(rf, admin_user, org, branch), "role.update", summary="matrix")
    other = Organization.objects.create(name="Other")
    AuditLog.objects.create(organization=other, action="patient.update", summary="secret")

    def actions(**params):
        response = admin_client.get("/settings/audit/", params)
        return {e.action for e, _, _ in response.context["entries"]}

    everything = admin_client.get("/settings/audit/").context["entries"]
    assert "secret" not in {e.summary for e, _, _ in everything}
    assert actions(actor=nurse.pk) == {"patient.update"}
    assert actions(category="permissions") == {"role.update"}
    assert actions(action="patient") == {"patient.update"}
    assert actions(q="matrix") == {"role.update"}
    tomorrow = timezone.localdate() + timedelta(days=1)
    assert actions(**{"from": tomorrow.isoformat(), "to": tomorrow.isoformat()}) == set()


def test_entry_drawer_and_export(rf, admin_client, admin_user, org, branch):
    e = services.record(
        _req(rf, admin_user, org, branch), "staff.update", admin_user,
        changes={"designation": ["", "Manager"]},
    )  # fmt: skip
    response = admin_client.get(f"/settings/audit/{e.pk}/")
    assert b"Manager" in response.content and b"append-only" in response.content
    other = AuditLog.objects.create(organization=Organization.objects.create(name="O"), action="x")
    assert admin_client.get(f"/settings/audit/{other.pk}/").status_code == 404

    csv = admin_client.get("/settings/audit/?format=csv")
    assert csv["Content-Type"].startswith("text/csv")
    assert "Staff account edited" in csv.content.decode()
    assert AuditLog.objects.filter(action="audit.export").exists()
