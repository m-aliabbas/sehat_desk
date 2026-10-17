# apps/patients — Patients

Registration, search, duplicate detection, demographics and allergies. PRD §5. PROJECT_NOTES §10.

## Files
- `services.py` — `register_patient` (assigns MRN), `find_duplicates`, `search_patients`,
  `add_identifier`, `format_mrn`.
- `identity.py` — `normalize_phone`, `normalize_cnic`, `format_phone`, `is_valid_phone`.
- `models.py` — `Patient` (org-wide, one MRN in every branch), `PatientIdentifier`, `Allergy`,
  `PatientAlert`.

## Rules
- Store phone and CNIC normalized; search and duplicate checks depend on it.
- Opening a record writes a `patient.view` audit event; demographic edits store field diffs.
- Allergies are never deleted; mark "entered in error" with a reason.

## Permissions
`patient.view`, `patient.create`, `patient.update`.

## Tests
`tests/test_patients.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
