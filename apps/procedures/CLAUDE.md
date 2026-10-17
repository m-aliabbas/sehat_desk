# apps/procedures — Billable procedures

Phase 33: ECG, EEG, ECHO, ultrasound, operations, dental work added to a patient.

## Files
- `services.py` — `order` (copies list price, optional price override with reason, issues an
  invoice line of kind "procedure"), `complete(findings)`, `cancel(reason)`.
- `models.py` — `PatientProcedure`. Catalogue = `masterdata.Service` with `is_procedure=True`.

## Rules
- Price override needs `procedure.price.override` and a reason; the list price stays on record.
- Only ordered (not done) procedures can be cancelled, with a reason. Its invoice is voided
  (`billing.void_unpaid`); if it has payments, refund them in Billing first.

## Permissions
`procedure.view`, `procedure.order`, `procedure.perform`, `procedure.price.override`.

## Tests
`tests/test_procedures.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
