"""Settings → Preferences (Phase 25): module switches and hospital-wide defaults."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.organizations.models import preferences_for
from apps.pharmacy.tests.test_inventory import pharmacist, product, req  # noqa: F401 (fixtures)


def _save(client, **changes):
    data = {
        "feature_lab": "on",
        "feature_pharmacy": "on",
        "feature_ipd": "on",
        "default_slot_minutes": 15,
        "default_follow_up_days": 7,
        "display_refresh_seconds": 5,
        "display_show_doctor": "on",
        "near_expiry_days": 90,
        "default_reorder_level": 0,
    }
    data.update(changes)
    data = {k: v for k, v in data.items() if v is not None}
    return client.post(reverse("organizations:preferences"), data)


def _fresh(org):
    org.refresh_from_db()
    if hasattr(org, "_sd_preferences"):
        del org._sd_preferences
    return preferences_for(org)


def test_preferences_need_admin(client, make_user):
    client.force_login(make_user("reception"))
    assert client.get(reverse("organizations:preferences")).status_code == 403


def test_saving_preferences_is_audited(admin_client, org):
    assert _save(admin_client, near_expiry_days=60).status_code == 302
    assert _fresh(org).near_expiry_days == 60
    entry = AuditLog.objects.filter(action="organization.preferences").get()
    assert entry.changes["near_expiry_days"] == [90, 60]


def test_out_of_range_values_are_refused(admin_client, org):
    response = _save(admin_client, display_refresh_seconds=1)
    assert response.status_code == 200
    assert _fresh(org).display_refresh_seconds == 5


def test_switching_lab_off_blocks_pages_and_hides_navigation(admin_client, org):
    assert admin_client.get(reverse("lab:worklist")).status_code == 200
    _save(admin_client, feature_lab=None)
    response = admin_client.get(reverse("lab:worklist"))
    assert response.status_code == 404
    assert b"turned off" in response.content
    assert admin_client.get(reverse("reports:lab-dashboard")).status_code == 404
    nav = admin_client.get(reverse("core:dashboard")).content
    assert reverse("lab:worklist").encode() not in nav
    _save(admin_client)
    assert admin_client.get(reverse("lab:worklist")).status_code == 200


def test_switching_ipd_off_blocks_ipd_and_nursing(admin_client, org):
    _save(admin_client, feature_ipd=None)
    assert admin_client.get(reverse("ipd:board")).status_code == 404
    assert admin_client.get(reverse("nursing:workspace")).status_code == 404


def test_switching_pharmacy_off(admin_client, org):
    _save(admin_client, feature_pharmacy=None)
    assert admin_client.get(reverse("pharmacy:pos")).status_code == 404
    assert admin_client.get(reverse("pharmacy:dashboard")).status_code == 404


def test_new_doctor_starts_from_defaults(admin_client, org):
    _save(admin_client, default_slot_minutes=20, default_follow_up_days=14)
    html = admin_client.get(reverse("masterdata:doctor-create")).content.decode()
    assert 'name="slot_minutes" value="20"' in html
    assert 'name="follow_up_days" value="14"' in html


def test_new_product_starts_from_default_reorder_level(admin_client, org):
    _save(admin_client, default_reorder_level=30)
    html = admin_client.get(reverse("pharmacy:product-create")).content.decode()
    assert 'name="reorder_level" value="30"' in html


def test_expiry_alert_window_follows_setting(org, branch, req, product):
    from apps.pharmacy import stock
    from apps.pharmacy.tests.test_inventory import _receive

    _receive(req, product, "B1", 60, 10)
    prefs = preferences_for(org)
    for days, expected in ((30, 0), (90, 1)):
        prefs.near_expiry_days = days
        prefs.save()
        branch.organization._sd_preferences = prefs
        assert stock.near_expiry(branch).count() == expected


def test_display_uses_refresh_and_doctor_name_settings(client, admin_client, org, branch):
    branch.regenerate_display_key()
    _save(admin_client, display_refresh_seconds=15, display_show_doctor=None)
    html = client.get(reverse("queue:display", args=[branch.display_key])).content.decode()
    assert 'hx-trigger="every 15s"' in html


@pytest.mark.parametrize("cap, amount, ok", [("10", "100", True), ("10", "101", False)])
def test_discount_cap(rf, admin_user, org, branch, patient, cap, amount, ok):
    from apps.accounts.services import effective_permissions
    from apps.billing import services as billing
    from apps.billing.services import BillingError

    settings_row = billing.settings_for(org)
    settings_row.max_discount_percent = Decimal(cap)
    settings_row.save()
    req = rf.post("/")
    req.user, req.organization, req.branch = admin_user, org, branch
    req.sd_perms = effective_permissions(admin_user, branch)
    invoice = billing.create_invoice(request=req, patient=patient)
    item = billing.add_item(invoice, request=req, description="X-ray", unit_price=Decimal("1000"))
    if ok:
        billing.set_line_discount(item, request=req, amount=amount, reason="staff")
    else:
        with pytest.raises(BillingError, match="above 10%"):
            billing.set_line_discount(item, request=req, amount=amount, reason="staff")
