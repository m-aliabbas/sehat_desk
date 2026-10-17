from datetime import datetime, time, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.ipd import services as ipd
from apps.ipd.tests.helpers import admit, make_request, make_ward
from apps.nursing import services
from apps.nursing.models import HandoverNote, MedicationAdministration, current_shift


@pytest.fixture
def nurse(make_user):
    return make_user("nurse", username="nurse1", first_name="Nida")


@pytest.fixture
def nreq(rf, nurse, org, branch):
    return make_request(rf, nurse, org, branch)


@pytest.fixture
def adm(rf, admin_user, org, branch, patient, doctor):
    _, _, beds = make_ward(org, branch)
    req = make_request(rf, admin_user, org, branch)
    admission = admit(req, patient, doctor, beds[0], admitted_at=timezone.now() - timedelta(days=2))
    return admission


@pytest.fixture
def order(rf, admin_user, org, branch, adm):
    req = make_request(rf, admin_user, org, branch)
    o = ipd.order_medication(
        adm, request=req, data={"medicine_name": "Ceftriaxone", "dose": "1 g", "frequency": "BD"}
    )
    o.starts_at = timezone.now() - timedelta(days=2)
    o.save(update_fields=["starts_at"])
    return o


def _at(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def test_current_shift():
    tz = timezone.get_current_timezone()
    assert current_shift(datetime(2026, 1, 2, 8, tzinfo=tz))[0] == "morning"
    assert current_shift(datetime(2026, 1, 2, 15, tzinfo=tz))[0] == "evening"
    night = current_shift(datetime(2026, 1, 2, 3, tzinfo=tz))
    assert night[0] == "night" and night[1].day == 1


def test_scheduled_times_follow_frequency(order):
    today = timezone.localdate()
    assert services.scheduled_times(order, today) == [_at(today, 9), _at(today, 21)]


def test_chart_dose_rules(nreq, order):
    yesterday = timezone.localdate() - timedelta(days=1)
    nine = _at(yesterday, 21)
    with pytest.raises(services.NursingError, match="reason"):
        services.chart_dose(order, request=nreq, status="refused", scheduled_for=nine)
    dose = services.chart_dose(order, request=nreq, status="given", scheduled_for=nine)
    assert dose.dose_given == "1 g"
    with pytest.raises(services.NursingError, match="already charted"):
        services.chart_dose(order, request=nreq, status="given", scheduled_for=nine)
    with pytest.raises(services.NursingError, match="not on this medicine"):
        services.chart_dose(order, request=nreq, status="given", scheduled_for=_at(yesterday, 10))
    with pytest.raises(services.NursingError, match="not due yet"):
        services.chart_dose(
            order,
            request=nreq,
            status="given",
            scheduled_for=_at(timezone.localdate() + timedelta(days=1), 9),
        )
    # A wrong entry is marked in error (kept), then the dose can be charted again.
    services.mark_error(dose, request=nreq, reason="Charted on wrong patient")
    services.chart_dose(
        order, request=nreq, status="held", reason="Nil by mouth", scheduled_for=nine
    )
    assert MedicationAdministration.objects.filter(order=order).count() == 2


def test_mar_marks_overdue_doses(order):
    yesterday = timezone.localdate() - timedelta(days=1)
    rows = services.mar_for_day(order.admission, yesterday, now=_at(timezone.localdate(), 12))
    assert [d["state"] for d in rows[0]["doses"]] == ["overdue", "overdue"]
    tasks = services.due_tasks([order.admission], now=_at(yesterday, 9))
    assert any(t["kind"] == "dose" and t["state"] == "due" for t in tasks)


def test_vitals_schedule_and_fluid_balance(nreq, adm):
    assert services.vitals_due_at(adm) is None
    services.set_vitals_schedule(adm, request=nreq, interval_hours=4)
    adm.refresh_from_db()
    assert services.vitals_due_at(adm) == adm.admitted_at + timedelta(hours=4)
    services.record_intake_output(adm, request=nreq, kind="intake", category="iv", volume_ml=1000)
    out = services.record_intake_output(
        adm, request=nreq, kind="output", category="urine", volume_ml=600
    )
    with pytest.raises(services.NursingError, match="matching"):
        services.record_intake_output(
            adm, request=nreq, kind="intake", category="urine", volume_ml=10
        )
    assert services.fluid_balance(adm)["balance"] == 400
    services.mark_error(out, request=nreq, reason="Duplicate")
    assert services.fluid_balance(adm)["balance"] == 1000


def test_handover_acknowledged_by_another_nurse(rf, make_user, org, branch, nreq, adm):
    ward = adm.current_bed.room.ward
    note = services.give_handover(ward, request=nreq, summary="Bed 1 febrile overnight")
    with pytest.raises(services.NursingError, match="receiving"):
        services.acknowledge_handover(note, request=nreq)
    other = make_request(rf, make_user("nurse"), org, branch)
    services.acknowledge_handover(note, request=other)
    assert HandoverNote.objects.get(pk=note.pk).acknowledged_by == other.user


def test_nursing_views_and_audit_trail(client, nurse, adm, order):
    client.force_login(nurse)
    assert client.get(reverse("nursing:workspace")).status_code == 200
    r = client.post(
        reverse("nursing:note-add", args=[adm.pk]), {"kind": "general", "note": "Comfortable"}
    )
    assert r.status_code == 302
    when = _at(timezone.localdate() - timedelta(days=1), 9)
    client.post(
        reverse("nursing:dose", args=[order.pk]),
        {"status": "given", "scheduled_for": when.isoformat()},
    )
    assert MedicationAdministration.objects.filter(order=order, status="given").exists()
    actions = set(
        AuditLog.objects.filter(entity="Admission", record_id=str(adm.pk)).values_list(
            "action", flat=True
        )
    )
    assert {"nursing.note.add", "nursing.dose"} <= actions
    # Notes are never stored in the audit log.
    assert not AuditLog.objects.filter(summary__icontains="Comfortable").exists()
    assert client.get(reverse("nursing:handover")).status_code == 200
    page = client.get(reverse("ipd:admission-detail", args=[adm.pk]), {"tab": "nursing"})
    assert b"nursing.note.add" in page.content


def test_nursing_needs_permission(client, make_user, adm):
    client.force_login(make_user("cashier"))
    assert client.get(reverse("nursing:workspace")).status_code == 403
    assert client.post(reverse("nursing:note-add", args=[adm.pk]), {"note": "x"}).status_code == 403
