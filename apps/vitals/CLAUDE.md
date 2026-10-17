# apps/vitals — Vital signs

PRD §6.2. Phase 9. Vitals station, entry form, corrections, trends.

## Files
- `services.py` — `record(request, patient, data, token=, encounter=)`, `mark_error`, `assess`,
  `summary`, `latest`, `latest_today`, `trends`, `fahrenheit_to_celsius`.
- `models.py` — `VitalSign` (`MEASURES`, BMI computed, °C/kg/cm stored).

## Rules
- Readings are never edited or deleted; "entered in error" with a reason, then a new set.
- Flags draw attention only (colour + word, never colour alone); children are not flagged on
  adult ranges.

## Permissions
`vitals.record`.

## Tests
`tests/test_vitals.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
