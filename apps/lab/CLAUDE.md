# apps/lab — Laboratory

Test catalogue, orders, samples, results, verification and reports. PRD §8. PROJECT_NOTES §12.

## Files
- `services.py` — `create_order` (issues the lab invoice), `cancel_order`, `collect_sample`,
  `reject_sample` (opens a recollection), `start_processing`, `enter_result`, `verify`, `release`,
  `amend` (new version), `acknowledge_critical`, `unacknowledged_critical`, `compute_flag`.
- `models.py` — `LabTestCategory`, `LabTest` (result types, `range_for(patient)`),
  `LabReferenceRange`, `LabPanel`, `LabOrder`, `LabOrderItem`, `LabSample`, `LabResult`.
- `signals.py` — invoice saves refresh order status. `barcode.py` — Code 39 SVG for tube labels (sticker size from `core/labels.py`, `?paper=`).

- `lab:tests` — read-only list of tests and packages with prices, for reception, doctors and the lab.

## Rules
- `edit_order`: priority and notes change until the order closes. Tests and doctor change only before payment, discount and collection; the unpaid invoice is voided and reissued, and removed tests stay as cancelled items.
- Reference ranges are chosen by sex and age (`range_for`). A test may instead have named bands (`band`, `band_normal`), e.g.
  HbA1c Normal, Prediabetes, Diabetes (`bands_for`, `services.band_for`). A band runs from its low to the next band's low. The
  result keeps a copy of `reference_text` and `band`, so a report never changes when the catalogue does.
- `lab.catalog.manage` alone (default hospital admin) opens the catalogue only: the worklist
  redirects there, and orders, results and labels stay refused.
- Order status is derived (`refresh_status`) from items, samples and invoice; never set it.
- Collect only when paid, unless urgent/STAT or a reason is recorded. An OPD visit (`apps/flow`) is stricter:
  `_flow_guard` refuses collect, processing and result entry while any lab fee of the visit is unpaid,
  whichever counter takes it; emergencies excepted.
- Results are clinical records: editable until verified, then only `amend` (new version + reason).
- Critical flags must be acknowledged by a clinician.
- The worklist search also takes a tube's accession, so scanning a label finds its order.
- A doctor who is only a doctor (`_own_doctor`) gets `lab/my_patients.html` instead of the worklist:
  the orders they asked for, reports first. Orders, reports, attachments and acknowledging are limited
  on the backend to orders they asked for or patients they treat (`limit_to_own_patients`); others 404.

## Permissions
`lab.order.create`, `lab.sample.collect`, `lab.result.enter`, `lab.result.verify`,
`lab.catalog.manage`. Module switch: `lab` in `apps/core/features.py`.

## Tests
`tests/test_lab.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
