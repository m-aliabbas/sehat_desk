from django.urls import reverse


def test_login_shows_the_hospital_name_when_set(client, org, admin_user):
    html = client.get(reverse("accounts:login"), follow=True).content.decode()
    assert org.name in html
    assert "by vibe.exe" in html


def test_login_before_setup_shows_the_product_name(client, db):
    html = client.get(reverse("accounts:login"), follow=True).content.decode()
    assert "SehatDesk" in html


def test_home_link_shows_the_hospital_name(admin_client, org):
    html = admin_client.get(reverse("core:menu")).content.decode()
    brand = html.split('aria-label="Main navigation"')[1].split("</a>")[0]
    assert org.name in brand
    assert "by vibe.exe" in brand


def test_print_credit_on_invoice_footer_and_prescription_templates():
    from django.template.loader import get_template

    for name in ("print/_footer.html", "print/_thermal_footer.html", "prescriptions/print.html"):
        assert "print/_credit.html" in get_template(name).template.source
    assert "vibeexe.com" in get_template("print/_credit.html").template.source
