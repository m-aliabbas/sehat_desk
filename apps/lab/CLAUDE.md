# apps/lab — Laboratory

Test catalogue, orders, samples, results, verification and reports. PRD §8. PROJECT_NOTES §12.

## Files
- `services.py` — `create_order` (issues the lab invoice), `cancel_order`, `collect_sample`,
  `reject_sample` (opens a recollection), `start_processing`, `enter_result`, `verify`, `release`,
  `amend` (new version), `acknowledge_critical`, `unacknowledged_critical`, `compute_flag`.
- `models.py` — `LabTestCategory`, `LabTest` (result types, `range_for(patient)`),
  `LabReferenceRange`, `LabPanel`, `LabOrder`, `LabOrderItem`, `LabSample`, `LabResult`.
- `signals.py` — invoice saves refresh order status. `barcode.py` — Code 39 SVG for tube labels.

## Rules
- Order status is derived (`refresh_status`) from items, samples and invoice; never set it.
- Collect only when paid, unless urgent/STAT or a reason is recorded.
- Results are clinical records: editable until verified, then only `amend` (new version + reason).
- Critical flags must be acknowledged by a clinician.

## Permissions
`lab.order.create`, `lab.sample.collect`, `lab.result.enter`, `lab.result.verify`,
`lab.catalog.manage`. Module switch: `lab` in `apps/core/features.py`.

## Tests
`tests/test_lab.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
