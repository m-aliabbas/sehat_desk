"""Hospital admin's short sidebar needs these codes. sync_permissions() only grants brand-new
codes, so existing hospitals get them here. Custom roles and other grants are untouched."""

from django.db import migrations

CODES = ("queue.manage", "pharmacy.inventory.manage", "lab.catalog.manage")


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    permissions = list(Permission.objects.filter(code__in=CODES))
    for role in Role.objects.filter(system_key="hospital_admin"):
        role.permissions.add(*permissions)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
