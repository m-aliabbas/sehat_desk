---
name: sehatdesk-model-change
description: Change SehatDesk models safely - choose the right base class, migrations, PostgreSQL portability, transactional money/stock writes, versioned clinical records, and the knock-on updates (demo data, reports, backups, search). Use when adding or altering models or fields.
---

# Change a model

1. **Base class** (`apps/core/models.py`, `apps/core/scoping.py`):
   - Org-wide data (patients, catalogues, staff records) → `OrgScopedModel`.
   - Per-branch data (visits, stock, invoices, beds) → `BranchScopedModel`.
   - Child rows of a scoped parent may use `TimeStampedModel` and reach scope through the parent.
   - Soft delete → `ArchivableModel`; list views call `.active()`.
2. **Record rules:**
   - Clinical data: never overwritten. Use a version table (encounters, lab results) or
     `entered_in_error` + reason + who/when (vitals, allergies, nursing, rounds).
   - Money and stock: amounts change only in services under `transaction.atomic` with
     `core.db.locked(obj)`; keep an append-only ledger row (StockTransaction, Payment, Refund,
     CommissionEarning, VendorLedgerEntry) rather than editing a total.
   - Human-facing numbers via `next_number(org, "key")`.
   - Add `UniqueConstraint`s for real invariants (one open bed occupancy, one MRN per org) so the
     database is the last line of defence.
3. **Portability:** ORM only; no SQLite-only SQL or functions. Decimal for money
   (`masterdata.models.money`). Time with `timezone.now()` / `timezone.localdate()`, never
   `datetime.now()`. Indexes on `(organization, ...)` for list filters.
4. **Migrations:** `python manage.py makemigrations <app>`; read the file; data migrations must be
   reversible or say why not. Then `makemigrations --check --dry-run` is clean.
5. **Knock-on updates — check each:**
   - Services and forms that create the model; admin/settings screens.
   - `apps/core/management/commands/seed_demo.py` and `apps/core/demo_data.py` if the demo flow
     touches it (run `apps/core/tests/test_seed_demo.py`).
   - Reports (`apps/reports/definitions/`), KPIs, finance statements/segments for new money kinds.
   - Global search providers if it should be searchable.
   - Backups need nothing for DB tables; new file storage must be added to `apps/core/backup.py`
     and `docs/BACKUP_AND_RECOVERY.md`.
   - PHI: no patient fields in `__str__` used by logs; logs pass through the redaction filter.
6. Tests for each new rule, then skills `sehatdesk-checks` and `sehatdesk-docs-sync`.
