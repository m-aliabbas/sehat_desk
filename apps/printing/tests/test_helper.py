"""The print helper against a real server: page in, printer commands or PDF out."""

import pytest
from PIL import Image

from apps.printing import services
from apps.printing.models import PrintJob, PrintStation

pypdfium2 = pytest.importorskip("pypdfium2")
from print_helper import agent, printers, render  # noqa: E402

PAGE = (
    "<!doctype html><html><head><style>body{margin:0}.s{width:72mm;margin:auto;"
    "font:bold 40px Arial}</style></head><body><div class=s>M-005</div></body></html>"
)


def test_escpos_is_the_image_then_a_cut():
    image = Image.new("L", (576, 300), 255)
    image.paste(0, (0, 0, 8, 1))  # first 8 dots of the first line black
    data = render.escpos(image)
    assert data.startswith(b"\x1b@\x1dv0\x00" + bytes((72, 0, 255, 0)) + b"\xff\x00")
    assert data.count(b"\x1dv0\x00") == 2  # 300 lines need a second band
    assert data.endswith(b"\x1dV\x42\x40")


def test_cups_sizes_are_read_in_millimetres():
    options = "ColorModel/Color: Gray\nPageSize/Media Size: *A4 A5 w226h3276 Weird\n"
    assert printers.parse_cups_sizes(options) == [[210, 297], [148, 210], [80, 1156]]


def test_a_printer_that_will_not_answer_has_no_sizes(monkeypatch):
    def broken(printer):
        raise OSError("driver")

    monkeypatch.setattr(printers, "paper_sizes", broken)
    assert printers.sizes_of(["Old"]) == {"Old": []}


@pytest.mark.django_db(transaction=True)
def test_helper_waits_then_joins_and_rejoins_after_removal(live_server, org, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(agent.printers, "list_printers", lambda: ["POS-80"])
    monkeypatch.setattr(agent.printers, "sizes_of", lambda names: {"POS-80": [[80, 3276]]})
    config = {"server": live_server.url}
    said = []

    def rounds(count):
        left = [count]

        def stop():
            left[0] -= 1
            return left[0] < 0

        return stop

    agent.loop(config, said.append, rounds(1))
    assert "allow new PCs" in said[-1] and "token" not in config

    services.set_enrollment(org, is_open=True)
    agent.loop(config, said.append, rounds(1))
    station = PrintStation.objects.get(organization=org)
    assert config["token"] and station.route_thermal == "POS-80"

    services.set_enrollment(org, is_open=False)
    station.delete()
    agent.loop(config, said.append, rounds(1))
    assert "token" not in agent.load_config() and "removed" in said[-1]


needs_browser = pytest.mark.skipif(not render.find_browser(), reason="needs Edge or Chrome")


@needs_browser
def test_thermal_page_becomes_a_strip_that_ends_with_the_content(tmp_path):
    kind, raw = render.prepare("thermal", PAGE, tmp_path)
    assert kind == "raw"
    strip = render.roll_image(tmp_path / "job.pdf")
    assert strip.width == render.DOTS_WIDE and strip.height < 150  # not 1200 mm of paper
    assert raw.read_bytes().endswith(b"\x1dV\x42\x40")


@needs_browser
def test_label_keeps_the_sticker_size(tmp_path):
    html = PAGE.replace("</head>", "<style>@page{size:38.1mm 25.4mm;margin:0}</style></head>")
    kind, pdf = render.prepare("label", html, tmp_path)
    document = pypdfium2.PdfDocument(str(pdf))
    assert kind == "pdf" and [round(v) for v in document[0].get_size()] == [108, 72]
    document.close()


@needs_browser
@pytest.mark.django_db(transaction=True)
def test_helper_collects_prints_and_reports(live_server, org, admin_user, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "config_path", lambda: tmp_path / "config.json")
    services.set_enrollment(org, is_open=True)
    config = agent.connect(live_server.url)
    station = PrintStation.objects.get(organization=org)
    agent.hello(config, ["XP-80"])
    station.refresh_from_db()
    assert station.printers == ["XP-80"] and services.is_online(station)
    PrintStation.objects.filter(pk=station.pk).update(route_thermal="XP-80")
    station.refresh_from_db()
    job = services.enqueue_test(station, "thermal", admin_user)

    sent = []

    def fake_send(printer, kind, path):
        sent.append((printer, kind, path.read_bytes()))

    assert agent.run_one(config, fake_send) is True
    assert sent[0][:2] == ("XP-80", "raw") and sent[0][2].endswith(b"\x1dV\x42\x40")
    job = PrintJob.objects.get(pk=job.pk)
    assert job.status == "done" and job.html == ""
    assert agent.run_one(config, fake_send) is False


@needs_browser
@pytest.mark.django_db(transaction=True)
def test_failed_printer_reaches_the_person_printing(
    live_server, org, admin_user, monkeypatch, tmp_path
):
    monkeypatch.setattr(agent, "config_path", lambda: tmp_path / "config.json")
    services.set_enrollment(org, is_open=True)
    config = agent.connect(live_server.url)
    station = PrintStation.objects.get(organization=org)
    PrintStation.objects.filter(pk=station.pk).update(route_a4="Laser")
    station.refresh_from_db()
    job = services.enqueue_test(station, "a4", admin_user)

    def broken(printer, kind, path):
        raise OSError("offline")

    agent.run_one(config, broken)
    job = PrintJob.objects.get(pk=job.pk)
    assert job.status == "failed" and job.error == "Could not print on this printer."
