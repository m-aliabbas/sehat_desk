from decimal import Decimal

import pytest

from apps.accounts.services import effective_permissions
from apps.billing import services as billing
from apps.organizations.models import Branch, Organization
from apps.patients.services import register_patient


def _req(rf, user, org, branch):
    req = rf.post("/")
    req.user, req.organization, req.branch = user, org, branch
    req.sd_perms = effective_permissions(user, branch)
    return req


def _keys(response):
    return [g["key"] for g in response.context["groups"]]


def test_reception_finds_patient_by_name_mrn_and_phone(client, make_user, patient):
    client.force_login(make_user("reception"))
    for q in ["ayesha", patient.mrn, "0300 1234567"]:
        response = client.get("/search/", {"q": q})
        assert response.status_code == 200
        hits = response.context["groups"][0]["hits"]
        assert hits[0].title == "Ayesha Khan", q


def test_results_follow_permissions(rf, client, make_user, org, branch, admin_user, patient):
    inv = billing.create_invoice(request=_req(rf, admin_user, org, branch), patient=patient)
    billing.add_item(inv, request=_req(rf, admin_user, org, branch), description="X",
                     unit_price=Decimal("10"))  # fmt: skip

    client.force_login(make_user("cashier"))  # billing.view + patient.view
    assert _keys(client.get("/search/", {"q": inv.number})) == ["invoices"]

    client.force_login(make_user("doctor"))  # no billing.view: the invoice is never searched
    assert _keys(client.get("/search/", {"q": inv.number})) == []
    assert "invoices" not in {p.key for p in client.get("/search/").context["providers"]}

    client.force_login(make_user("reception", first_name="Sana"))  # no admin.users.view
    assert "staff" not in _keys(client.get("/search/", {"q": "sana"}))
    client.force_login(admin_user)
    assert "staff" in _keys(client.get("/search/", {"q": "sana"}))


def test_other_organization_is_never_found(client, make_user, admin_user):
    other = Organization.objects.create(name="Other Hospital")
    other_branch = Branch.objects.create(organization=other, name="Main", code="M", is_main=True)
    register_patient(
        organization=other, branch=other_branch, user=admin_user,
        data={"full_name": "Zubaida Hidden", "gender": "female"},
    )  # fmt: skip
    client.force_login(make_user("reception"))
    assert client.get("/search/", {"q": "zubaida"}).context["groups"] == []


def test_branch_scoped_records_stay_in_their_branch(rf, client, make_user, org, branch, branch2,
                                                    admin_user, patient):  # fmt: skip
    inv = billing.create_invoice(request=_req(rf, admin_user, org, branch2), patient=patient)
    client.force_login(make_user("cashier"))  # current branch: main
    assert _keys(client.get("/search/", {"q": inv.number})) == []


def test_suggest_fragment_and_recent_searches(client, make_user, patient):
    client.force_login(make_user("reception"))
    response = client.get("/search/suggest/", {"q": "ay"})
    assert b"Ayesha Khan" in response.content and b"See all results" in response.content
    assert b"at least 2 characters" in client.get("/search/suggest/", {"q": "a"}).content

    client.get("/search/", {"q": "ayesha"})
    recent = client.get("/search/suggest/", {"q": ""})
    assert b"Recent searches" in recent.content and b"ayesha" in recent.content
    client.post("/search/recent/clear/", {"next": "/search/"})
    assert b"Recent searches" not in client.get("/search/suggest/", {"q": ""}).content


def test_type_filter_and_topbar(client, make_user, patient):
    client.force_login(make_user("reception"))
    response = client.get("/search/", {"q": "ayesha", "type": "patients"})
    assert _keys(response) == ["patients"]
    page = client.get("/").content
    assert b'action="/search/"' in page and b"Ctrl+K" in page


@pytest.mark.parametrize("q", ["", "a"])
def test_short_queries_return_nothing(client, make_user, patient, q):
    client.force_login(make_user("reception"))
    assert client.get("/search/", {"q": q}).context["groups"] == []
