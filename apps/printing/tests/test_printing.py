import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.printing import services
from apps.printing.detect import kind_of
from apps.printing.models import PrintEnrollment, PrintJob, PrintStation

pytestmark = pytest.mark.django_db
PAGE = "<html>Patient page</html>"


A4 = [210, 297]
ROLL = [80, 3276]
STICKER = [38, 25]


def _join(client, ip="127.0.0.1", name="Reception"):
    return client.post(
        "/print/agent/join/",
        json.dumps({"name": name}),
        content_type="application/json",
        REMOTE_ADDR=ip,
    )


def _connect(client, org, ip="127.0.0.1", printers=("Printer One", "Printer Two"), sizes=None):
    services.set_enrollment(org, is_open=True)
    answer = _join(client, ip).json()
    station = PrintStation.objects.get(name=answer["name"])
    services.set_enrollment(org, is_open=False)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {answer['token']}", "REMOTE_ADDR": ip}
    client.post(
        "/print/agent/hello/",
        json.dumps({"printers": list(printers), "sizes": sizes or {}}),
        content_type="application/json",
        **headers,
    )
    station.refresh_from_db()
    return station, headers


def _send(client, paper="thermal", **extra):
    return client.post(
        "/print/jobs/",
        json.dumps({"paper": paper, "html": PAGE}),
        content_type="application/json",
        **extra,
    ).json()


def test_pc_joins_only_while_allowed_and_only_from_the_lan(client, org):
    assert _join(client).status_code == 403
    services.set_enrollment(org, is_open=True)
    assert _join(client, ip="8.8.8.8").status_code == 403
    joined = _join(client, ip="192.168.1.20")
    assert joined.status_code == 200
    station = PrintStation.objects.get(organization=org, name="Reception")
    assert station.token_hash and joined.json()["token"] not in station.token_hash
    assert _join(client, ip="192.168.1.21").json()["name"] == "Reception (2)"
    services.set_enrollment(org, is_open=False)
    assert _join(client, ip="192.168.1.22").status_code == 403


def test_enrollment_ends_by_itself(client, org):
    services.set_enrollment(org, is_open=True)
    PrintEnrollment.objects.update(open_until=timezone.now())
    assert _join(client).status_code == 403
    assert services.enrollment_open_until(org) is None


def test_two_hospitals_open_at_once_joins_neither(client, org):
    from apps.organizations.models import Organization

    services.set_enrollment(org, is_open=True)
    services.set_enrollment(Organization.objects.create(name="Other"), is_open=True)
    assert _join(client).status_code == 403


def test_printers_are_assigned_by_their_paper_sizes(client, org):
    station, _ = _connect(
        client,
        org,
        printers=("Microsoft Print to PDF", "Front Desk", "Office", "Small"),
        sizes={
            "Microsoft Print to PDF": [A4],
            "Front Desk": [ROLL],
            "Office": [A4, [76, 127]],
            "Small": [STICKER],
        },
    )
    assert station.route_thermal == "Front Desk"
    assert station.route_a4 == "Office"
    assert station.route_label == "Small"
    assert station.printer_for("a5") == "Office"  # A5 follows the A4 printer


@pytest.mark.parametrize(
    "name, sizes, kind",
    [
        ("POS-80C", [], "thermal"),
        ("Zebra ZD220", [A4], "label"),
        ("Xprinter XP-370B", [], "label"),
        ("HP LaserJet", [A4, [76, 127]], "office"),
        ("Unknown", [], ""),
        ("Unknown", [[101, 152]], ""),
        ("Save as PDF", [A4], ""),
    ],
)
def test_kind_of_a_printer(name, sizes, kind):
    assert kind_of(name, sizes) == kind


def test_two_matching_printers_ask_instead_of_guessing(admin_client, client, org):
    station, _ = _connect(
        client, org, printers=("Laser A", "Laser B"), sizes={"Laser A": [A4], "Laser B": [A4]}
    )
    assert station.route_a4 == "" and services.needs_choice(station, "a4")
    assert "Choose a printer" in admin_client.get("/settings/printers/").content.decode()
    admin_client.post(f"/settings/printers/{station.pk}/routes/", {"route_a4": "Laser B"})
    station.refresh_from_db()
    assert station.route_a4 == "Laser B" and station.printer_for("a5") == "Laser B"


def test_a_choice_by_hand_survives_detection_until_its_printer_goes(admin_client, client, org):
    sizes = {"Laser": [A4], "POS-80": [ROLL]}
    station, headers = _connect(client, org, printers=("Laser", "POS-80"), sizes=sizes)
    url = f"/settings/printers/{station.pk}/routes/"
    admin_client.post(url, {"route_thermal": ""})  # this PC keeps the print window for receipts
    station.refresh_from_db()
    assert station.route_thermal == "" and station.manual_routes == ["thermal"]
    services.touch(station, "127.0.0.1", ["Laser", "POS-80", "New"], {**sizes, "New": []})
    assert PrintStation.objects.get(pk=station.pk).route_thermal == ""

    admin_client.post(url, {"route_thermal": "__auto__"})
    station.refresh_from_db()
    assert station.route_thermal == "POS-80" and station.manual_routes == []


def test_a_new_printer_does_not_move_a_paper_that_works(client, org):
    station, _ = _connect(client, org, printers=("Laser",), sizes={"Laser": [A4]})
    services.touch(station, "127.0.0.1", ["Laser", "Colour"], {"Laser": [A4], "Colour": [A4]})
    assert PrintStation.objects.get(pk=station.pk).route_a4 == "Laser"


def test_admin_opens_and_closes_new_pcs(admin_client, org):
    admin_client.post("/settings/printers/", {"enrollment": "open"})
    assert services.enrollment_open_until(org)
    admin_client.post("/settings/printers/", {"enrollment": "close"})
    assert services.enrollment_open_until(org) is None


def test_no_helper_or_no_route_falls_back_to_the_print_window(admin_client, client, org):
    assert _send(admin_client) == {"queued": False}
    station, _ = _connect(client, org)
    assert _send(admin_client) == {"queued": False}  # online, nothing chosen for this paper
    assert not PrintJob.objects.exists()


def test_each_paper_goes_to_its_printer_and_the_page_is_wiped(admin_client, client, org):
    station, headers = _connect(client, org)
    PrintStation.objects.filter(pk=station.pk).update(route_thermal="XP-80", route_a4="Laser")
    first = _send(admin_client)
    second = _send(admin_client, paper="a4")
    assert first["printer"] == "XP-80" and second["printer"] == "Laser"

    got = client.get("/print/agent/next/", **headers).json()["job"]
    assert got["html"] == PAGE and got["printer"] == "XP-80"
    job = PrintJob.objects.get(pk=first["id"])
    assert job.html == "" and job.status == "sending"

    client.post(
        f"/print/agent/jobs/{got['id']}/",
        json.dumps({"ok": True}),
        content_type="application/json",
        **headers,
    )
    assert admin_client.get(f"/print/jobs/{got['id']}/").json()["status"] == "done"
    assert client.get("/print/agent/next/", **headers).json()["job"]["printer"] == "Laser"
    assert client.get("/print/agent/next/", **headers).json() == {"job": None}


def test_browser_only_uses_the_helper_on_its_own_pc(admin_client, client, org):
    station, _ = _connect(client, org, ip="192.168.1.20")
    PrintStation.objects.filter(pk=station.pk).update(route_thermal="XP-80")
    assert _send(admin_client, REMOTE_ADDR="192.168.1.99") == {"queued": False}
    assert _send(admin_client, REMOTE_ADDR="192.168.1.20")["queued"] is True


def test_late_helper_does_not_print_old_jobs(admin_client, client, org):
    station, headers = _connect(client, org)
    PrintStation.objects.filter(pk=station.pk).update(route_thermal="XP-80")
    sent = _send(admin_client)
    PrintJob.objects.filter(pk=sent["id"]).update(
        created_at=timezone.now() - services.COLLECT_WINDOW - timedelta(seconds=1)
    )
    assert client.get("/print/agent/next/", **headers).json() == {"job": None}
    job = PrintJob.objects.get(pk=sent["id"])
    assert job.status == "expired" and job.html == ""


def test_agent_needs_its_token(client):
    assert client.get("/print/agent/next/").status_code == 401
    assert client.get("/print/agent/next/", HTTP_AUTHORIZATION="Bearer x").status_code == 401


def test_removing_a_pc_cuts_off_its_token(admin_client, client, org):
    station, headers = _connect(client, org)
    admin_client.post(f"/settings/printers/{station.pk}/delete/")
    assert client.get("/print/agent/next/", **headers).status_code == 401


def test_settings_screen_saves_only_real_printers(admin_client, client, org):
    station, _ = _connect(client, org, printers=("XP-80", "Laser"))
    url = f"/settings/printers/{station.pk}/routes/"
    admin_client.post(url, {"route_thermal": "XP-80", "route_a4": "Not a printer"})
    station.refresh_from_db()
    assert station.route_thermal == "XP-80" and station.route_a4 == ""
    html = admin_client.get("/settings/printers/").content.decode()
    assert "Reception" in html and "XP-80" in html


def test_other_hospital_cannot_see_or_change_a_station(admin_client, org, db):
    from apps.organizations.models import Organization

    other = Organization.objects.create(name="Other")
    station = PrintStation.objects.create(organization=other, name="Theirs")
    assert "Theirs" not in admin_client.get("/settings/printers/").content.decode()
    assert admin_client.post(f"/settings/printers/{station.pk}/delete/").status_code == 404
