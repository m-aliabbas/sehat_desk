# apps/organizations — Hospital, branches and preferences

Settings → Hospital, Branches and Preferences. PROJECT_NOTES §9, §18.

## Files
- `models.py` — `Organization` (prefixes for MRN/receipts, print header/footer), `Branch`
  (code, `display_key` for the waiting-room screen), `OrganizationPreferences` +
  `preferences_for(org)` (module switches, slot length, near-expiry days, reorder default).
- `views.py` — settings home, hospital form, preferences, branch list/create/edit/toggle.

## Rules
- Every business record belongs to exactly one organization.
- Preference fields named `feature_<key>` drive `apps/core/features.py`.

## Permissions
`admin.organization.manage`, `admin.branches.manage`.

## Tests
`tests/test_settings.py`, `tests/test_preferences.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
