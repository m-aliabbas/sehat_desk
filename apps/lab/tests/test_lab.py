from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.billing import services as billing
from apps.billing.models import Invoice, PaymentMethod
from apps.lab import services
from apps.lab.barcode import code39_svg
from apps.lab.models import (
    LabAttachment,
    LabOrder,
    LabOrderItem,
    LabPanel,
    LabReferenceRange,
    LabResult,
    LabSample,
    LabTest,
    LabTestCategory,
)

Status = LabOrder.Status


def _req(rf, user, org, branch):
    from apps.accounts.services import effective_permissions

    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    return req


@pytest.fixture
def tech(make_user):
    return make_user("lab", username="labtech")


@pytest.fixture
def lab_req(rf, tech, org, branch):
    return _req(rf, tech, org, branch)


@pytest.fixture
def catalogue(org):
    haem = LabTestCategory.objects.create(organization=org, name="Haematology")
    chem = LabTestCategory.objects.create(organization=org, name="Chemistry")
    hb = LabTest.objects.create(
        organization=org, code="HB", name="Haemoglobin", category=haem, sample_type="blood",
        price=Decimal("300"), unit="g/dL",
    )  # fmt: skip
    LabReferenceRange.objects.create(test=hb, gender="female", low=12, high=15.5, critical_low=7)
    LabReferenceRange.objects.create(test=hb, gender="male", low=13.5, high=17.5, critical_low=7)
    glu = LabTest.objects.create(
        organization=org, code="FBS", name="Fasting glucose", category=chem, sample_type="serum",
        price=Decimal("200"), unit="mg/dL", decimals=0,
    )  # fmt: skip
    LabReferenceRange.objects.create(test=glu, low=70, high=99, critical_high=400)
    hcv = LabTest.objects.create(
        organization=org, code="HCV", name="Anti-HCV", category=chem, sample_type="serum",
        price=Decimal("500"), result_type="posneg", normal_value="Negative",
    )  # fmt: skip
    return {"hb": hb, "glu": glu, "hcv": hcv}


def _pay(order, req):
    method = PaymentMethod.objects.filter(organization=order.organization, code="cash").first()
    if method is None:
        billing.ensure_payment_methods(order.organization)
        method = PaymentMethod.objects.get(organization=order.organization, code="cash")
    invoice = Invoice.objects.get(pk=order.invoice_id)
    billing.receive_payment(invoice, request=req, method=method, amount=invoice.balance_due)


@pytest.fixture
def cash_req(rf, make_user, org, branch):
    return _req(rf, make_user("cashier"), org, branch)


def test_order_creates_items_samples_and_invoice(lab_req, patient, catalogue, cash_req):
    order = services.create_order(
        request=lab_req, patient=patient, tests=[catalogue["hb"], catalogue["glu"]]
    )
    assert order.number.startswith("LAB-") and order.status == Status.PAYMENT_PENDING
    assert order.samples.count() == 2  # blood + serum
    assert order.invoice.grand_total == Decimal("500.00")
    assert order.invoice.items.filter(kind="lab").count() == 2
    _pay(order, cash_req)
    order.refresh_from_db()
    assert order.status == Status.SAMPLE_PENDING  # payment signal moved it on


def test_panel_dedupes_and_bills_at_panel_price(lab_req, patient, catalogue, org):
    panel = LabPanel.objects.create(organization=org, code="DM", name="Diabetes", price=350)
    panel.tests.set([catalogue["glu"], catalogue["hb"]])
    order = services.create_order(
        request=lab_req, patient=patient, panels=[panel], tests=[catalogue["glu"]]
    )
    assert order.items.count() == 2
    assert order.invoice.grand_total == Decimal("350.00")


def test_routine_collection_waits_for_payment(lab_req, patient, catalogue, cash_req):
    order = services.create_order(request=lab_req, patient=patient, tests=[catalogue["hb"]])
    sample = order.samples.get()
    with pytest.raises(services.LabError, match="not paid"):
        services.collect_sample(sample, request=lab_req)
    services.collect_sample(sample, request=lab_req, unpaid_reason="Panel patient, billed monthly")
    order.refresh_from_db()
    assert order.status == Status.SAMPLE_COLLECTED


def test_stat_orders_collect_before_payment(lab_req, patient, catalogue):
    order = services.create_order(
        request=lab_req, patient=patient, tests=[catalogue["hb"]], priority="stat"
    )
    services.collect_sample(order.samples.get(), request=lab_req)
    assert LabOrderItem.objects.get(order=order).status == "collected"


def _collected(lab_req, patient, tests, cash_req):
    order = services.create_order(request=lab_req, patient=patient, tests=tests)
    _pay(order, cash_req)
    for s in order.samples.all():
        services.collect_sample(s, request=lab_req)
    return LabOrder.objects.get(pk=order.pk)


def test_flags_use_sex_specific_ranges(lab_req, patient, catalogue, cash_req):
    order = _collected(
        lab_req, patient, [catalogue["hb"], catalogue["glu"], catalogue["hcv"]], cash_req
    )
    hb, glu, hcv = order.items.order_by("pk")
    r = services.enter_result(hb, request=lab_req, value="12.8")
    assert r.flag == "normal" and "12 – 15.5" in r.reference_text  # female range
    r = services.enter_result(glu, request=lab_req, value="450")
    assert r.flag == "critical_high"
    r = services.enter_result(hcv, request=lab_req, value="positive")
    assert (r.flag, r.value_text) == ("abnormal", "Positive")
    order = services.refresh_status(order)
    assert order.status == Status.RESULT_ENTERED and order.has_critical
    with pytest.raises(services.LabError, match="choose one"):
        services.enter_result(hcv, request=lab_req, value="maybe")


def test_age_specific_range_wins(org, catalogue, patient):
    hb = catalogue["hb"]
    child = LabReferenceRange.objects.create(test=hb, age_min_years=0, age_max_years=12, low=11)
    patient.date_of_birth = date(date.today().year - 6, 1, 1)
    assert hb.range_for(patient) == child


def test_verify_release_and_lock(lab_req, rf, patient, catalogue, cash_req, make_user, org, branch):
    order = _collected(lab_req, patient, [catalogue["hb"]], cash_req)
    item = order.items.get()
    services.enter_result(item, request=lab_req, value="11.0")
    services.enter_result(item, request=lab_req, value="11.2")  # unverified: corrected in place
    assert item.results.count() == 1
    with pytest.raises(services.LabError, match="Verify every"):
        services.release(order, request=lab_req)
    assert services.verify([item], request=lab_req) == 1
    services.release(order, request=lab_req)
    order.refresh_from_db()
    assert order.status == Status.RELEASED
    with pytest.raises(services.LabError, match="verified"):
        services.enter_result(item, request=lab_req, value="13")


def test_entry_permission_is_not_verification(rf, make_user, org, branch, patient, catalogue,
                                              lab_req, cash_req):  # fmt: skip
    from apps.accounts.models import Role

    order = _collected(lab_req, patient, [catalogue["hb"]], cash_req)
    item = order.items.get()
    services.enter_result(item, request=lab_req, value="12")
    entry_only = Role.objects.create(organization=org, name="Lab assistant")
    from apps.accounts.models import Permission, RoleAssignment

    entry_only.permissions.set(Permission.objects.filter(code="lab.result.enter"))
    user = make_user()
    RoleAssignment.objects.create(user=user, role=entry_only)
    with pytest.raises(services.LabError, match="verify"):
        services.verify([item], request=_req(rf, user, org, branch))


def test_amendment_creates_version_and_reopens(lab_req, patient, catalogue, cash_req):
    order = _collected(lab_req, patient, [catalogue["hb"]], cash_req)
    item = order.items.get()
    services.enter_result(item, request=lab_req, value="12")
    services.verify([item], request=lab_req)
    services.release(order, request=lab_req)
    with pytest.raises(services.LabError, match="reason"):
        services.amend(item, request=lab_req, value="13", reason="")
    new = services.amend(item, request=lab_req, value="13.1", reason="Transcription error")
    old = item.results.get(version=1)
    assert new.version == 2 and new.is_current and not old.is_current and old.verified_at
    order.refresh_from_db()
    assert order.status == Status.RESULT_ENTERED and order.amendment_count == 1
    assert order.released_at is None


def test_reject_sample_opens_recollection(lab_req, patient, catalogue, cash_req):
    order = _collected(lab_req, patient, [catalogue["hb"]], cash_req)
    sample = order.samples.get()
    new = services.reject_sample(sample, request=lab_req, reason="Haemolysed")
    assert new.recollection_of == sample and new.accession != sample.accession
    assert order.items.get().sample == new and order.items.get().status == "pending"
    order.refresh_from_db()
    assert order.status == Status.SAMPLE_PENDING


def test_cancel_voids_unpaid_invoice_but_not_paid(lab_req, patient, catalogue, cash_req):
    order = services.create_order(request=lab_req, patient=patient, tests=[catalogue["hb"]])
    services.cancel_order(order, request=lab_req, reason="Duplicate order")
    order.refresh_from_db()
    assert order.status == Status.CANCELLED and order.invoice.status == "cancelled"
    paid = services.create_order(request=lab_req, patient=patient, tests=[catalogue["hb"]])
    _pay(paid, cash_req)
    with pytest.raises(services.LabError, match="Refund"):
        services.cancel_order(paid, request=lab_req, reason="x")


def test_critical_acknowledgement(lab_req, rf, doctor, org, branch, patient, catalogue, cash_req):
    order = services.create_order(
        request=lab_req, patient=patient, tests=[catalogue["glu"]], doctor=doctor, priority="stat"
    )
    services.collect_sample(order.samples.get(), request=lab_req)
    result = services.enter_result(order.items.get(), request=lab_req, value="500")
    assert list(services.unacknowledged_critical(org, doctor=doctor)) == [result]
    services.acknowledge_critical(result, request=_req(rf, doctor.user, org, branch))
    assert not services.unacknowledged_critical(org, doctor=doctor).exists()


def test_code39_barcode():
    svg = code39_svg("S261016-0001")
    assert svg.startswith("<svg") and svg.count("<rect") == 14 * 5
    with pytest.raises(ValueError):
        code39_svg("a_b")


# --- Views -------------------------------------------------------------------------------------


def test_desk_flow_through_views(client, tech, patient, catalogue, cash_req):
    client.force_login(tech)
    resp = client.post(
        reverse("lab:order-create"),
        {"patient": patient.pk, "tests": [catalogue["hb"].pk], "priority": "urgent"},
    )
    order = LabOrder.objects.get()
    assert resp.status_code == 302 and resp.url == reverse("lab:order-detail", args=[order.pk])
    assert client.get(reverse("lab:worklist")).status_code == 200
    sample = order.samples.get()
    client.post(reverse("lab:sample-collect", args=[sample.pk]))
    label = client.get(reverse("lab:sample-label", args=[sample.pk]))
    assert sample.accession in label.content.decode()
    item = order.items.get()
    client.post(reverse("lab:order-start", args=[order.pk]))
    client.post(reverse("lab:results", args=[order.pk]), {f"value_{item.pk}": "6.5"})
    item.refresh_from_db()
    assert item.status == "result_entered"
    assert item.results.get().flag == "critical_low"
    client.post(reverse("lab:verify", args=[order.pk]), {"items": [item.pk], "release": "1"})
    order.refresh_from_db()
    assert order.status == Status.RELEASED
    report = client.get(reverse("lab:report", args=[order.pk]))
    body = report.content.decode()
    assert "Haemoglobin" in body and "Critical low" in body and "Preliminary" not in body
    upload = SimpleUploadedFile("cbc.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    client.post(reverse("lab:attachment-upload", args=[item.pk]), {"file": upload})
    att = LabAttachment.objects.get()
    assert client.get(reverse("lab:attachment", args=[att.pk])).status_code == 200


def test_doctor_orders_from_consultation(client, doctor, patient, catalogue, rf, org, branch):
    from apps.encounters import services as enc_services

    enc, _ = enc_services.start(request=_req(rf, doctor.user, org, branch), patient=patient)
    client.force_login(doctor.user)
    page = client.get(reverse("encounters:detail", args=[enc.pk]))
    assert 'id="sec-lab"' in page.content.decode() and "Haemoglobin" in page.content.decode()
    client.post(
        reverse("lab:encounter-order", args=[enc.pk]),
        {"tests": [catalogue["hb"].pk, catalogue["glu"].pk], "priority": "routine"},
    )
    order = LabOrder.objects.get()
    assert order.encounter == enc and order.doctor == doctor and order.source == "doctor"
    # clinicians cannot open unreleased reports
    assert client.get(reverse("lab:report", args=[order.pk])).status_code == 404
    assert client.get(reverse("lab:order-detail", args=[order.pk])).status_code == 200


def test_permissions(client, make_user, patient, catalogue, lab_req):
    order = services.create_order(request=lab_req, patient=patient, tests=[catalogue["hb"]])
    client.force_login(make_user("cashier"))
    assert client.get(reverse("lab:worklist")).status_code == 403
    assert client.get(reverse("lab:order-detail", args=[order.pk])).status_code == 403
    client.force_login(make_user("reception"))
    assert client.get(reverse("lab:catalog")).status_code == 403


def test_catalogue_views(client, tech, org):
    client.force_login(tech)
    client.post(reverse("lab:catalog"), {"name": "Serology"})
    cat = LabTestCategory.objects.get(name="Serology")
    resp = client.post(
        reverse("lab:test-create"),
        {
            "code": "crp", "name": "CRP", "category": cat.pk, "sample_type": "serum",
            "price": "800", "turnaround_hours": "6", "result_type": "numeric", "unit": "mg/L",
            "decimals": "1", "options": "", "normal_value": "", "method": "",
            "ranges-TOTAL_FORMS": "2", "ranges-INITIAL_FORMS": "0",
            "ranges-MIN_NUM_FORMS": "0", "ranges-MAX_NUM_FORMS": "1000",
            "ranges-0-gender": "any", "ranges-0-age_min_years": "0", "ranges-0-high": "5",
            "ranges-1-gender": "any", "ranges-1-age_min_years": "0",
        },
    )  # fmt: skip
    assert resp.status_code == 302
    test = LabTest.objects.get(code="CRP")
    assert test.ranges.count() == 1 and test.ranges.get().high == 5
    assert LabResult.objects.count() == 0 and LabSample.objects.count() == 0
