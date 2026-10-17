from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.ipd.tests.helpers import make_request
from apps.notifications import channels, services, sweeps
from apps.notifications.models import Channel, Event, MessageDelivery, MessageTemplate, Notification


def _appointment(org, branch, patient, doctor, user, *, start=None, day=None, number="APT-1"):
    start = start or time(10)
    return Appointment.objects.create(
        organization=org,
        branch=branch,
        number=number,
        patient=patient,
        doctor=doctor,
        department=doctor.department,
        date=day or timezone.localdate(),
        start_time=start,
        end_time=time(min(start.hour + 1, 23)),
        fee=Decimal("1500"),
        created_by=user,
    )


def test_role_and_permission_targeting_with_dedupe(org, branch, branch2, make_user):
    n1 = make_user("nurse", branch=branch)
    n2 = make_user("nurse", branch=branch2)
    pharm = make_user("pharmacy")
    assert services.notify(org, Event.GENERAL, "Ward meeting", branch=branch, roles=["nurse"]) == 1
    assert Notification.objects.get().recipient == n1  # the other branch's nurse is not included
    created = services.notify(
        org,
        Event.LOW_STOCK,
        "Low",
        branch=branch,
        permission="pharmacy.inventory.manage",
        dedupe_key="low-1",
    )
    again = services.notify(
        org,
        Event.LOW_STOCK,
        "Low",
        branch=branch,
        permission="pharmacy.inventory.manage",
        dedupe_key="low-1",
    )
    assert created >= 1 and again == 0
    assert Notification.objects.filter(recipient=pharm, event=Event.LOW_STOCK).count() == 1
    assert not Notification.objects.filter(recipient=n2).exists()


def test_center_mark_read_and_mark_all(client, make_user, org, branch):
    user = make_user("reception")
    other = make_user("reception")
    for i in range(3):
        services.notify(org, Event.GENERAL, f"Note {i}", users=[user], url="/patients/")
    services.notify(org, Event.GENERAL, "Not yours", users=[other])
    client.force_login(user)
    page = client.get(reverse("notifications:center"))
    assert (
        page.status_code == 200 and b"Note 2" in page.content and b"Not yours" not in page.content
    )
    assert page.context["notif_unread"] == 3
    first = Notification.objects.filter(recipient=user).first()
    r = client.post(reverse("notifications:open", args=[first.pk]))
    assert r["Location"] == "/patients/" and Notification.objects.get(pk=first.pk).is_read
    theirs = Notification.objects.get(recipient=other)
    assert client.post(reverse("notifications:read", args=[theirs.pk])).status_code == 404
    client.post(reverse("notifications:read-all"))
    assert services.unread_count(user, org) == 0 and services.unread_count(other, org) == 1


def test_appointment_cancelled_notifies_doctor(
    django_capture_on_commit_callbacks, org, branch, patient, doctor, admin_user
):
    from apps.appointments import services as appt_services

    appt = _appointment(
        org, branch, patient, doctor, admin_user, day=timezone.localdate() + timedelta(days=1)
    )
    with django_capture_on_commit_callbacks(execute=True):
        appt_services.cancel(appt, user=admin_user, reason="Patient travelling")
    note = Notification.objects.get(event=Event.APPOINTMENT_CANCELLED)
    assert note.recipient == doctor.user and patient.full_name not in note.title


def test_lab_release_and_critical_result_notify_doctor(
    django_capture_on_commit_callbacks, org, branch, patient, doctor, admin_user
):
    from apps.lab.models import LabOrder, LabOrderItem, LabResult, LabTest, LabTestCategory

    cat = LabTestCategory.objects.create(organization=org, name="Chem")
    test = LabTest.objects.create(organization=org, code="K", name="Potassium", category=cat)
    order = LabOrder.objects.create(
        organization=org,
        branch=branch,
        number="LAB-1",
        patient=patient,
        doctor=doctor,
        created_by=admin_user,
    )
    item = LabOrderItem.objects.create(order=order, test=test, test_name="Potassium")
    with django_capture_on_commit_callbacks(execute=True):
        result = LabResult.objects.create(item=item, flag="critical_high", entered_by=admin_user)
    assert not Notification.objects.exists()  # unverified: no alert yet
    with django_capture_on_commit_callbacks(execute=True):
        result.verified_at, result.verified_by = timezone.now(), admin_user
        result.save()
        order.status, order.released_at = LabOrder.Status.RELEASED, timezone.now()
        order.save()
    events = set(Notification.objects.filter(recipient=doctor.user).values_list("event", "level"))
    assert (Event.LAB_CRITICAL, "critical") in events and (Event.LAB_READY, "info") in events


def test_sweeps_raise_upcoming_and_stock_alerts(
    org, branch, patient, doctor, admin_user, make_user
):
    from apps.pharmacy import stock

    soon = (timezone.localtime() + timedelta(minutes=30)).time().replace(second=0, microsecond=0)
    if soon < timezone.localtime().time():
        pytest.skip("too close to midnight")
    _appointment(org, branch, patient, doctor, admin_user, start=soon)
    pharm = make_user("pharmacy")
    original = stock.low_stock
    stock.low_stock = lambda b, o: type("Q", (), {"count": lambda self: 4})()
    try:
        sweeps.run(branch)
        sweeps.run(branch)  # repeats are deduplicated
    finally:
        stock.low_stock = original
    assert (
        Notification.objects.filter(recipient=doctor.user, event=Event.APPOINTMENT_UPCOMING).count()
        == 1
    )
    assert Notification.objects.filter(recipient=pharm, event=Event.LOW_STOCK).count() == 1


def test_overdue_ward_tasks_remind_nurses(rf, org, branch, patient, doctor, admin_user, make_user):
    from apps.ipd import services as ipd
    from apps.ipd.tests.helpers import admit, make_ward

    nurse = make_user("nurse")
    _, _, beds = make_ward(org, branch)
    req = make_request(rf, admin_user, org, branch)
    adm = admit(req, patient, doctor, beds[0], admitted_at=timezone.now() - timedelta(days=1))
    order = ipd.order_medication(
        adm, request=req, data={"medicine_name": "X", "dose": "1", "frequency": "Q6H"}
    )
    order.starts_at = timezone.now() - timedelta(days=1)
    order.save(update_fields=["starts_at"])
    sweeps.workflow_reminders(branch, timezone.now())
    assert Notification.objects.filter(recipient=nurse, event=Event.REMINDER).exists()


def test_external_channels_and_templates(org, settings):
    delivery = channels.send_external(
        org,
        Event.LAB_READY,
        Channel.SMS,
        to="03001234567",
        context={"patient_name": "Ayesha", "order_number": "LAB-1"},
    )
    assert delivery.status == MessageDelivery.Status.SKIPPED
    assert "Ayesha" in delivery.body and org.name in delivery.body

    MessageTemplate.objects.create(
        organization=org,
        event=Event.LAB_READY,
        channel=Channel.EMAIL,
        subject="Report {order_number}",
        body="Hi {patient_name} {secret} {order_number}",
    )
    settings.EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    sent = channels.send_external(
        org,
        Event.LAB_READY,
        Channel.EMAIL,
        to="a@example.com",
        context={"patient_name": "Ayesha", "order_number": "LAB-1", "secret": "x"},
    )
    assert sent.status == MessageDelivery.Status.SENT and mail.outbox[0].subject == "Report LAB-1"
    assert "{secret}" in sent.body  # placeholders outside the allowed list are not filled


def test_message_settings_page(admin_client):
    assert admin_client.get(reverse("notifications:messages")).status_code == 200
    r = admin_client.post(
        reverse("notifications:messages"),
        {"event": "lab_ready", "channel": "sms", "body": "Ready {order_number}", "is_active": "on"},
    )
    assert r.status_code == 302 and MessageTemplate.objects.count() == 1


def test_sweep_middleware_runs_once_per_interval(admin_client, settings, monkeypatch):
    from django.core.cache import cache

    calls = []
    monkeypatch.setattr(sweeps, "run", lambda branch, now=None: calls.append(branch.pk))
    settings.SEHATDESK_NOTIFICATION_SWEEP_SECONDS = 300
    cache.clear()
    admin_client.get("/")
    admin_client.get("/")
    assert len(calls) == 1
