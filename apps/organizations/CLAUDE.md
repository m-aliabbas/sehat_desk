# apps/organizations — Hospital, branches and preferences

Settings → Hospital, Branches and Preferences. PROJECT_NOTES §9, §18.

## Files
- `models.py` — `Organization` (prefixes for MRN/receipts, print header/footer), `Branch`
  (code, `display_key` for the waiting-room screen), `OrganizationPreferences` +
  `preferences_for(org)` (module switches, slot length, near-expiry days, reorder default).
- `views.py` — settings home, module switches (shown on `core:menu`), hospital form, preferences, branch list/create/edit/toggle.

## Fields and tax
`field_rules.py`: per-hospital hidden/optional/required rules (default optional) and default tax rates,
screen `field-rules`. Add a key to `FIELDS` and enforce it in the service or form that saves the field.

## Print layouts
`PrintLayout` (one per organization + document), `print_layout_for`, screen `print-layouts`. Templates
use `{% print_layout "invoice" as L %}{% include "print/_layout_css.html" %}`.
The prescription layout also takes top and bottom letterhead images (`header_image`, `footer_image`), printed to the paper's edges. The top image replaces the hospital block unless `image_keep_clinic_text`; the doctor's block always prints. They are stored under `organization-logos/` because `config/urls.py` serves only that media folder without a permission check.

## Rules
- Every business record belongs to exactly one organization.
- Preference fields named `feature_<key>` drive `apps/core/features.py`.

## Permissions
`admin.organization.manage`, `admin.branches.manage`.

## Tests
`tests/test_settings.py`, `tests/test_preferences.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
