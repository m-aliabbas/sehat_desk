# apps/search — Global search

Phase 22: one query across patients, invoices, appointments, lab orders, medicines and staff.

## Files
- `services.py` — `Provider` + `PROVIDERS` list, `providers_for(request)`, `search(request, q)`,
  `remember`, `recent`.
- `views.py` — results page, top-bar suggestions, recent searches.

## Rules
- Each provider declares the permissions that may see it and searches only `scoped()` querysets.
- Add a provider for a new searchable record type rather than special-casing the view.

## Tests
`tests/test_search.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
