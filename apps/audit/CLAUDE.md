# apps/audit — Audit trail

Append-only audit events and the admin viewer. PRD §19. PROJECT_NOTES §14.

## Files
- `services.py` — `record(request, "area.action", obj, changes=..., summary=...)` (never raises),
  `record_ai_acceptance(...)` for clinician-accepted AI drafts.
- `catalog.py` — readable labels for action codes (optional; unknown codes still display).
- `signals.py` — sign-in/out and similar auth events. `views.py` — read-only viewer + CSV export.

## Rules
- No add/edit/delete screen exists; the model and database refuse changes.
- Never put clinical text or patient identifiers in `summary`/`changes`; use field names and
  record numbers.
- Add a label to `catalog.py` when you introduce a new action code users will filter on.

## Permissions
`admin.audit.view`.

## Tests
`tests/test_audit.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
