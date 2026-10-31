"""Reception can now give discounts and refunds, under the limit in Settings → Billing.
sync_permissions() only grants brand-new codes, so existing hospitals get it here."""

from django.db import migrations


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    permissions = list(Permission.objects.filter(code__in=("billing.discount", "billing.refund")))
    for role in Role.objects.filter(system_key="reception"):
        role.permissions.add(*permissions)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_pharmacist_discount"),
    ]

    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
