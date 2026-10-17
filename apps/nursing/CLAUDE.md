# apps/nursing — Nursing care for inpatients

Phase 17: nurse assignments, notes, vitals schedule, intake/output, MAR and handover.

## Files
- `services.py` — `assign`, `unassign`, `add_note`, `set_vitals_schedule`, `vitals_due_at`,
  `record_intake_output`, `fluid_balance`, `mar_for_day`, `chart_dose`, `give_handover`,
  `acknowledge_handover`, `due_tasks`, `mark_error`.
- `views.py` — nursing workspace; each action is audited against the admission.

## Rules
- Everything is a clinical record: no edits or deletes; "entered in error" with a reason.
- Writes need `nursing.notes` (assignments: `ipd.manage`) and refuse closed admissions.
- Medication schedule times come from `ipd.MedicationOrder.SCHEDULE`.

## Permissions
`nursing.notes`, `ipd.manage`. Module switch: `ipd`.

## Tests
`tests/test_nursing.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
