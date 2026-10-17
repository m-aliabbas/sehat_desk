import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.encounters import services as enc_services
from apps.encounters.models import DiagnosisCode, EncounterDiagnosis
from apps.patients.models import Allergy
from apps.prescriptions import services
from apps.prescriptions.models import (
    FavoritePrescription,
    Medicine,
    Prescription,
    suggest_quantity,
)


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
def encounter(doc_request, patient):
    enc, _ = enc_services.start(request=doc_request, patient=patient)
    return enc


@pytest.fixture
def panadol(org):
    return Medicine.objects.create(
        organization=org, brand_name="Panadol", generic_name="Paracetamol", strength="500 mg"
    )


@pytest.fixture
def amoxil(org):
    return Medicine.objects.create(
        organization=org,
        brand_name="Amoxil",
        generic_name="Amoxicillin",
        strength="500 mg",
        form=Medicine.Form.CAPSULE,
    )


ITEM = {"dose": "1 tablet", "frequency": "TDS", "duration_value": 5, "duration_unit": "days"}


def _sign(enc, req):
    enc_services.save_note(enc, request=req, data={"chief_complaint": "Fever"})
    return enc_services.complete(enc, request=req)


# --- Quantity ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "form,dose,freq,days,unit,expected",
    [
        ("tablet", "1 tablet", "TDS", 5, "days", 15),
        ("tablet", "1/2", "BD", 7, "days", 7),
        ("capsule", "2", "OD", 2, "weeks", 28),
        ("syrup", "5 ml", "TDS", 5, "days", None),
        ("tablet", "1", "SOS", 5, "days", None),
    ],
)
def test_suggest_quantity(form, dose, freq, days, unit, expected):
    assert suggest_quantity(form, dose, freq, days, unit) == expected


# --- Diagnoses ---------------------------------------------------------------------------------


def test_diagnosis_search_add_and_primary(doc_request, encounter, org):
    results = list(enc_services.search_codes(org, "upper resp"))
    assert results and results[0].code == "J06.9"
    d1 = enc_services.add_diagnosis(encounter, request=doc_request, code=results[0])
    d2 = enc_services.add_diagnosis(
        encounter, request=doc_request, text="Viral fever", kind="final"
    )
    assert d1.is_primary and not d2.is_primary and d2.code is None
    with pytest.raises(enc_services.EncounterError, match="already"):
        enc_services.add_diagnosis(encounter, request=doc_request, text="viral fever")
    enc_services.update_diagnosis(d2, request=doc_request, primary=True)
    d1.refresh_from_db()
    assert not d1.is_primary
    enc_services.remove_diagnosis(d2, request=doc_request)
    d1.refresh_from_db()
    assert d1.is_primary


def test_coded_diagnosis_is_enough_to_complete_and_is_versioned(doc_request, encounter, org):
    enc_services.ensure_diagnosis_codes(org)
    code = DiagnosisCode.objects.get(organization=org, code="I10")
    enc_services.add_diagnosis(encounter, request=doc_request, code=code, kind="final")
    version = _sign(encounter, doc_request)
    assert version.content["diagnoses"][0]["code"] == "I10"
    with pytest.raises(enc_services.EncounterError):
        enc_services.add_diagnosis(encounter, request=doc_request, text="x")


# --- Prescription ------------------------------------------------------------------------------


def test_add_item_copies_master_and_suggests_quantity(doc_request, encounter, panadol):
    item = services.add_item(encounter, request=doc_request, medicine=panadol, data=dict(ITEM))
    rx = item.prescription
    assert rx.number.startswith("RX-") and rx.status == Prescription.Status.DRAFT
    assert (item.medicine_name, item.generic_name, item.quantity) == (
        "Panadol 500 mg",
        "Paracetamol",
        15,
    )
    assert item.directions == "1 tablet · three times daily · for 5 days"


def test_free_text_item_and_validation(doc_request, encounter):
    item = services.add_item(
        encounter, request=doc_request, data={**ITEM, "medicine_name": "ORS sachet"}
    )
    assert item.medicine is None and item.quantity is None
    with pytest.raises(services.PrescriptionError, match="dose"):
        services.add_item(encounter, request=doc_request, data={"medicine_name": "X", "dose": ""})


def test_allergy_conflict_needs_reason(doc_request, encounter, amoxil, patient, admin_user):
    Allergy.objects.create(patient=patient, substance="Amoxicillin", recorded_by=admin_user)
    with pytest.raises(services.AllergyConflict) as err:
        services.add_item(encounter, request=doc_request, medicine=amoxil, data=dict(ITEM))
    assert err.value.substances == ["Amoxicillin"]
    item = services.add_item(
        encounter, request=doc_request, medicine=amoxil, data=dict(ITEM),
        override_reason="Tolerated before, monitored",
    )  # fmt: skip
    assert item.allergy_override_reason


def test_other_doctor_and_non_prescriber_blocked(rf, org, branch, department, make_user,
                                                 encounter, panadol):  # fmt: skip
    from apps.masterdata.models import DoctorProfile

    other = make_user("doctor")
    DoctorProfile.objects.create(organization=org, user=other, department=department)
    with pytest.raises(services.PrescriptionError, match="treating doctor"):
        services.add_item(
            encounter, request=_req(rf, other, org, branch), medicine=panadol, data=dict(ITEM)
        )


def test_sign_send_and_reopen(doc_request, encounter, panadol):
    services.add_item(encounter, request=doc_request, medicine=panadol, data=dict(ITEM))
    rx = encounter.prescription
    with pytest.raises(services.PrescriptionError, match="sign"):
        services.send_to_pharmacy(rx, request=doc_request)
    enc_services.save_note(encounter, request=doc_request, data={"provisional_diagnosis": "Flu"})
    version = _sign(encounter, doc_request)
    rx.refresh_from_db()
    assert rx.status == Prescription.Status.SIGNED and rx.signed_at
    assert version.content["prescription"][0]["medicine"] == "Panadol 500 mg"
    services.send_to_pharmacy(rx, request=doc_request)
    with pytest.raises(services.PrescriptionError, match="already"):
        services.send_to_pharmacy(rx, request=doc_request)
    enc_services.reopen(encounter, request=doc_request, reason="Change dose")
    rx.refresh_from_db()
    assert rx.is_draft and rx.sent_to_pharmacy_at is None


def test_favorites_and_copy_previous_skip_allergies(doc_request, rf, encounter, panadol, amoxil,
                                                    patient, admin_user, org, branch):  # fmt: skip
    services.add_item(encounter, request=doc_request, medicine=panadol, data=dict(ITEM))
    services.add_item(encounter, request=doc_request, medicine=amoxil, data=dict(ITEM))
    fav = services.save_favorite(encounter, request=doc_request, name="URTI")
    assert len(fav.items) == 2
    enc_services.save_note(encounter, request=doc_request, data={"provisional_diagnosis": "Flu"})
    _sign(encounter, doc_request)

    Allergy.objects.create(patient=patient, substance="Amoxicillin", recorded_by=admin_user)
    second, _ = enc_services.start(request=doc_request, patient=patient)
    assert second.pk != encounter.pk
    assert services.copy_items(second, request=doc_request, items=fav.items) == 1
    assert [i.medicine_name for i in second.prescription.items.all()] == ["Panadol 500 mg"]


# --- Views -------------------------------------------------------------------------------------


def test_panel_endpoints_over_htmx(client, doctor, encounter, panadol):
    client.force_login(doctor.user)
    hx = {"HTTP_HX_REQUEST": "true"}
    search = client.get(reverse("prescriptions:medicine-search"), {"medicine_name": "parac"}, **hx)
    assert "Panadol 500 mg" in search.content.decode()
    resp = client.post(
        reverse("prescriptions:item-add", args=[encounter.pk]),
        {"medicine": panadol.pk, "medicine_name": "Panadol 500 mg", "dose": "1 tablet",
         "route": "oral", "frequency": "BD", "duration_value": "3", "duration_unit": "days"},
        **hx,
    )  # fmt: skip
    body = resp.content.decode()
    assert resp.status_code == 200 and 'id="rx-panel"' in body and "Panadol 500 mg" in body
    # the search must fill its results box, not replace it (htmx inherits the form's swap)
    assert 'hx-target="#rx-med-results" hx-swap="innerHTML"' in body
    assert encounter.prescription.items.get().quantity == 6
    dx = client.post(
        reverse("encounters:diagnosis-add", args=[encounter.pk]),
        {"text": "Viral fever", "kind": "provisional"},
        **hx,
    )
    assert "Viral fever" in dx.content.decode()
    assert EncounterDiagnosis.objects.filter(encounter=encounter).count() == 1
    page = client.get(reverse("encounters:detail", args=[encounter.pk]))
    assert (
        'id="rx-panel"' in page.content.decode() and 'id="diagnoses-panel"' in page.content.decode()
    )


def test_print_and_inbox_permissions(client, doctor, make_user, doc_request, encounter, panadol):
    services.add_item(encounter, request=doc_request, medicine=panadol, data=dict(ITEM))
    enc_services.save_note(encounter, request=doc_request, data={"provisional_diagnosis": "Flu"})
    _sign(encounter, doc_request)
    rx = encounter.prescription
    services.send_to_pharmacy(rx, request=doc_request)

    pharmacist = make_user("pharmacy")
    client.force_login(pharmacist)
    inbox = client.get(reverse("prescriptions:pharmacy-inbox"), follow=True)
    assert rx.number in inbox.content.decode()
    printed = client.get(reverse("prescriptions:print", args=[rx.pk]))
    assert printed.status_code == 200 and "Panadol 500 mg" in printed.content.decode()
    assert AuditLog.objects.filter(action="prescription.print").exists()

    client.force_login(make_user("cashier"))
    assert client.get(reverse("prescriptions:print", args=[rx.pk])).status_code == 403
    assert client.get(reverse("prescriptions:pharmacy-inbox")).status_code == 403


def test_medicine_master_crud(admin_client, org):
    resp = admin_client.post(
        reverse("prescriptions:medicine-create"),
        {"brand_name": "Brufen", "generic_name": "Ibuprofen", "strength": "400 mg",
         "form": "tablet", "route": "oral"},
    )  # fmt: skip
    assert resp.status_code == 302
    med = Medicine.objects.get(brand_name="Brufen")
    dup = admin_client.post(
        reverse("prescriptions:medicine-create"),
        {"brand_name": "brufen", "generic_name": "Ibuprofen", "strength": "400 mg",
         "form": "tablet", "route": "oral"},
    )  # fmt: skip
    assert "already in the list" in dup.content.decode()
    admin_client.post(reverse("prescriptions:medicine-toggle", args=[med.pk]))
    med.refresh_from_db()
    assert not med.is_active
    assert FavoritePrescription.objects.count() == 0


def test_doctor_cannot_manage_medicine_master(client, doctor):
    client.force_login(doctor.user)
    assert client.get(reverse("prescriptions:medicine-list")).status_code == 403
