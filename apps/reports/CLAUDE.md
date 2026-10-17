# apps/reports — Reports and dashboards

Phases 20–21. PROJECT_NOTES §14, §20.

## Files
- `engine.py` — `Filters`, `ReportResult` and the `@report(...)` registry decorator.
- `definitions/` — reports by area: `clinical.py`, `financial.py`, `ipd.py`, `lab.py`,
  `pharmacy.py`. Add a report by decorating a function in the right file.
- `exports.py` — CSV, XLSX (openpyxl), PDF (WeasyPrint with print fallback).
- `kpis.py` — `admin_kpis`, `trend`, `lab_kpis`, `pharmacy_kpis` for the dashboards.
- `views.py` (hub, report pages, exports), `views_dashboards.py` (lab and pharmacy dashboards).

## Rules
- Reports query through scoped querysets and declare their permission; money reports need
  `reports.financial.view`.
- Keep dashboards cheap: aggregate queries, no per-row loops (`test_query_scaling.py`).

## Permissions
`reports.view`, `reports.financial.view`.

## Tests
`tests/test_reports.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
