from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.queue import services as queue_services
from apps.vitals import services
from apps.vitals.models import VitalSign, compute_bmi


def _req(rf, user, org, branch):
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    return req


@pytest.fixture
def nurse(make_user):
    return make_user("nurse", username="nurse1")


@pytest.fixture
def nurse_request(rf, nurse, org, branch):
    return _req(rf, nurse, org, branch)


def test_bmi_calculation():
    assert compute_bmi(Decimal("70"), Decimal("175")) == Decimal("22.9")
    assert compute_bmi(None, Decimal("175")) is None


def test_record_computes_bmi_and_flags(nurse_request, patient):
    v = services.record(
        request=nurse_request,
        patient=patient,
        data={
            "temperature_c": Decimal("39.2"),
            "bp_systolic": 185,
            "bp_diastolic": 95,
            "pulse": 88,
            "spo2": 93,
            "weight_kg": Decimal("70"),
            "height_cm": Decimal("175"),
            "blood_glucose": 110,
            "glucose_context": "fasting",
        },
    )
    assert v.bmi == Decimal("22.9")
    flags = services.assess(v)
    assert flags["temperature_c"] == "high"
    assert flags["bp_systolic"] == "critical" and flags["bp"] == "critical"
    assert flags["pulse"] == "normal"
    assert flags["spo2"] == "low"
    assert flags["blood_glucose"] == "high"  # fasting 110 is above 99


def test_children_are_not_flagged_on_adult_ranges(nurse_request, patient):
    patient.date_of_birth = date(date.today().year - 4, 1, 1)
    patient.save()
    v = services.record(request=nurse_request, patient=patient, data={"pulse": 120, "spo2": 88})
    flags = services.assess(v)
    assert "pulse" not in flags and flags["spo2"] == "critical"


def test_record_validation(nurse_request, patient):
    with pytest.raises(services.VitalsError, match="at least one"):
        services.record(request=nurse_request, patient=patient, data={"notes": "x"})
    with pytest.raises(services.VitalsError, match="both"):
        services.record(request=nurse_request, patient=patient, data={"bp_systolic": 120})
    with pytest.raises(services.VitalsError, match="higher"):
        services.record(
            request=nurse_request, patient=patient, data={"bp_systolic": 80, "bp_diastolic": 90}
        )


def test_reception_cannot_record(rf, make_user, org, branch, patient):
    req = _req(rf, make_user("reception"), org, branch)
    with pytest.raises(services.VitalsError, match="not allowed"):
        services.record(request=req, patient=patient, data={"pulse": 80})


def test_mark_error_keeps_the_row(nurse_request, patient):
    v = services.record(request=nurse_request, patient=patient, data={"pulse": 300 - 220})
    services.mark_error(v, request=nurse_request, reason="Wrong patient")
    v.refresh_from_db()
    assert v.entered_in_error and v.error_reason == "Wrong patient"
    assert services.latest(patient, patient.organization) is None
    assert VitalSign.objects.count() == 1
    with pytest.raises(services.VitalsError, match="already"):
        services.mark_error(v, request=nurse_request, reason="again")


def test_trends_oldest_first(nurse_request, patient):
    for pulse in (70, 80, 90):
        services.record(request=nurse_request, patient=patient, data={"pulse": pulse})
    trend = {t["field"]: t for t in services.trends(patient, patient.organization)}
    assert [p[1] for p in trend["pulse"]["points"]] == [70, 80, 90]
    assert trend["pulse"]["change"] == 20


def test_token_vitals_link_to_patient(nurse_request, admin_user, rf, org, branch, department,
                                      doctor, patient):  # fmt: skip
    desk = _req(rf, admin_user, org, branch)
    token = queue_services.issue_token(
        request=desk, patient=patient, department=department, doctor=doctor
    )
    v = services.record(request=nurse_request, patient=patient, data={"pulse": 72}, token=token)
    assert v.token == token and v.branch == branch


# --- Views -------------------------------------------------------------------------------------


def test_station_and_record_views(client, nurse, admin_user, rf, org, branch, department, doctor,
                                  patient):  # fmt: skip
    desk = _req(rf, admin_user, org, branch)
    token = queue_services.issue_token(
        request=desk, patient=patient, department=department, doctor=doctor
    )
    client.force_login(nurse)
    page = client.get(reverse("vitals:station"))
    assert page.status_code == 200 and token.label in page.content.decode()
    resp = client.post(
        reverse("vitals:record"),
        {
            "token": token.pk,
            "temperature": "101.3",
            "temperature_unit": "F",
            "bp_systolic": "120",
            "bp_diastolic": "80",
            "glucose_context": "random",
        },
    )
    assert resp.status_code == 302
    v = VitalSign.objects.get()
    assert v.temperature_c == Decimal("38.5") and v.recorded_by == nurse
    assert AuditLog.objects.filter(action="vitals.record").exists()
    tab = client.get(reverse("patients:detail", args=[patient.pk]) + "?tab=vitals")
    assert "38.5 °C" in tab.content.decode()


def test_implausible_temperature_rejected(client, nurse, patient):
    client.force_login(nurse)
    resp = client.post(
        reverse("vitals:record"),
        {"patient": patient.pk, "temperature": "38", "temperature_unit": "F",
         "glucose_context": "random"},
    )  # fmt: skip
    assert resp.status_code == 200 and "not plausible" in resp.content.decode()
    assert not VitalSign.objects.exists()


def test_other_org_patient_is_404(client, nurse, make_user):
    from apps.organizations.models import Organization
    from apps.patients.models import Patient

    other = Organization.objects.create(name="Other")
    from apps.organizations.models import Branch

    ob = Branch.objects.create(organization=other, name="X", code="X", is_main=True)
    p = Patient.objects.create(organization=other, mrn="X-1", full_name="Z", registered_branch=ob)
    client.force_login(nurse)
    assert client.get(reverse("vitals:record") + f"?patient={p.pk}").status_code == 404


def test_cashier_cannot_open_station(client, make_user):
    client.force_login(make_user("cashier"))
    assert client.get(reverse("vitals:station")).status_code == 403
