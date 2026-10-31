from django.db import migrations
from django.db.models import OuterRef, Subquery


def forwards(apps, schema_editor):
    """Older ledger rows take their batch's sale price: the nearest record of what it was."""
    txn = apps.get_model("pharmacy", "StockTransaction")
    batch = apps.get_model("pharmacy", "InventoryBatch")
    txn.objects.update(
        unit_price=Subquery(batch.objects.filter(pk=OuterRef("batch_id")).values("sale_price")[:1])
    )


class Migration(migrations.Migration):
    dependencies = [("pharmacy", "0010_uom_prices_lots_ledger")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
