"""Audit log hardening (Phase 23): filter indexes and database triggers that refuse UPDATE and
DELETE on audit rows, so even a direct SQL change or a mistaken bulk delete cannot alter history.
Supported on SQLite (now) and PostgreSQL (later); other backends rely on the model guards.
"""

from django.db import migrations, models

SQLITE = [
    """CREATE TRIGGER IF NOT EXISTS audit_auditlog_no_update BEFORE UPDATE ON audit_auditlog
       BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS audit_auditlog_no_delete BEFORE DELETE ON audit_auditlog
       BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;""",
]
SQLITE_DROP = [
    "DROP TRIGGER IF EXISTS audit_auditlog_no_update;",
    "DROP TRIGGER IF EXISTS audit_auditlog_no_delete;",
]
POSTGRES = [
    """CREATE OR REPLACE FUNCTION audit_auditlog_append_only() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'audit log is append-only'; END; $$ LANGUAGE plpgsql;""",
    """CREATE TRIGGER audit_auditlog_no_change BEFORE UPDATE OR DELETE ON audit_auditlog
       FOR EACH ROW EXECUTE FUNCTION audit_auditlog_append_only();""",
    """CREATE TRIGGER audit_auditlog_no_truncate BEFORE TRUNCATE ON audit_auditlog
       FOR EACH STATEMENT EXECUTE FUNCTION audit_auditlog_append_only();""",
]
POSTGRES_DROP = [
    "DROP TRIGGER IF EXISTS audit_auditlog_no_change ON audit_auditlog;",
    "DROP TRIGGER IF EXISTS audit_auditlog_no_truncate ON audit_auditlog;",
    "DROP FUNCTION IF EXISTS audit_auditlog_append_only();",
]


def _run(statements):
    def apply(apps, schema_editor):
        vendor = schema_editor.connection.vendor
        for sql in statements.get(vendor, []):
            schema_editor.execute(sql)

    return apply


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["organization", "actor", "-created_at"],
                name="audit_audit_organiz_actor_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["organization", "entity", "record_id"],
                name="audit_audit_organiz_entity_idx",
            ),
        ),
        migrations.RunPython(
            _run({"sqlite": SQLITE, "postgresql": POSTGRES}),
            _run({"sqlite": SQLITE_DROP, "postgresql": POSTGRES_DROP}),
        ),
    ]
