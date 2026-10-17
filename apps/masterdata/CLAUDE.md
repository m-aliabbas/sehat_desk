# apps/masterdata — Departments, doctors and services

Settings → Departments, Doctors (schedules, leave), Services. PROJECT_NOTES §10.

## Files
- `models.py` — `Department` (token prefix), `Specialty`, `DoctorProfile` (fees, slot length,
  follow-up window), `DoctorSchedule` (per branch + weekday, optional break), `DoctorLeave`,
  `ServiceCategory`, `Service` (`is_procedure`, `price_editable`, `price_for(branch)`),
  `ServiceBranchPrice`.
- `views.py`, `forms.py` — the settings screens.

## Rules
- Org-wide catalogues; schedules and service prices may differ per branch.
- A doctor is a staff `User` with a `DoctorProfile`; access still comes from roles.
- Services marked `is_procedure` are the procedures catalogue (apps/procedures).

## Permissions
`admin.masterdata.manage`.

## Tests
`tests/test_masterdata.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
