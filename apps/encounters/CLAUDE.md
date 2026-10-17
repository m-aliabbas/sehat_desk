# apps/encounters — OPD consultations

The consultation note, diagnoses, and the doctor's workspace. PRD §6. PROJECT_NOTES §12.

## Files
- `services.py` — `start(request, patient, token=, appointment=)`, `save_note` (autosave, revision
  check), `complete` (signs a version), `reopen` (reason, new version later), `add_diagnosis`,
  `update_diagnosis`, `remove_diagnosis`, `follow_ups`, `ensure_diagnosis_codes`, `doctor_for`,
  `can_write`.
- `models.py` — `NOTE_FIELDS`, `Encounter` (draft/completed), `EncounterVersion` (signed copy),
  `DiagnosisCode`, `EncounterDiagnosis`.
- `diagnosis_codes.py` — starter ICD-10 list loaded per organization.

## Rules
- Only the encounter's own doctor writes or signs it (`consultation.create`/`consultation.sign`).
- Completing copies the note, diagnoses and prescription into an `EncounterVersion` that is never
  edited. Reopening keeps old versions; refused once the pharmacy has dispensed.
- Completing needs a chief complaint and a diagnosis; follow-up date not in the past.
- Starting from a token moves the token to in_consultation and the appointment to with_doctor.
- Audit note saves by field name only, never the text.
- AI suggestions are drafts; saving one needs the doctor's action and `audit.record_ai_acceptance`.

## Connected
prescriptions (signed with the note), lab (orders from the workspace), procedures, vitals, queue.

## Permissions
`consultation.view`, `consultation.create`, `consultation.sign`, `consultation.reopen`.

## Tests
`tests/test_encounters.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
