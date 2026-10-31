"""Pharmacists can now give discounts at the till, under the limit in Settings → Billing.
sync_permissions() only grants brand-new codes, so existing hospitals get it here."""

from django.db import migrations


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    permission = Permission.objects.filter(code="pharmacy.discount").first()
    if permission is None:
        return
    for role in Role.objects.filter(system_key="pharmacy"):
        role.permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_reception_vitals"),
    ]

    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
