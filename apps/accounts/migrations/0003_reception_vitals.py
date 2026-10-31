"""Reception can now take vitals on the front-desk visit screen. sync_permissions() only grants
brand-new codes, so existing hospitals get it here. Custom roles are untouched."""

from django.db import migrations


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    permission = Permission.objects.filter(code="vitals.record").first()
    if permission is None:
        return
    for role in Role.objects.filter(system_key="reception"):
        role.permissions.add(permission)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_hospital_admin_sidebar_access"),
    ]

    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
