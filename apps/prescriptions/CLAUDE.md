# apps/prescriptions — Prescriptions and the medicine master

PRD §6.3. Prescription lines written inside a consultation; Settings → Medicines.

## Files
- `services.py` — `add_item` (allergy check, suggested quantity), `remove_item`, `set_advice`,
  `copy_items`, `save_favorite`, `send_to_pharmacy`, `sign_for`/`reopen_for` (called by
  encounters), `allergy_conflicts`, `prescription_for`.
- `models.py` — `Medicine` (shared master; pharmacy products point at it), `Prescription`
  (draft/signed + `dispense_status`), `PrescriptionItem`, `FavoritePrescription`,
  `suggest_quantity`.

## Rules
- Only the encounter's doctor, on an open draft, with `prescription.create`.
- An allergy conflict needs an override reason.
- Signed with the encounter; sent to pharmacy only after signing. Dispensing status is set by
  `apps/pharmacy/pos.py`.

## Permissions
`prescription.create`.

## Tests
`tests/test_prescriptions.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
