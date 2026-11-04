# apps/reports — Reports and dashboards

Phases 20–21. PROJECT_NOTES §14, §20.

## Files
- `engine.py` — `Filters`, `ReportResult` and the `@report(...)` registry decorator.
- `definitions/` — reports by area: `clinical.py` (incl. `patient-flow-times` from `apps/flow`), `daily.py` (daily sales, purchases, both side by
  side), `financial.py`, `ipd.py`, `lab.py`, `pharmacy.py`, `accounts.py` (khata, customers,
  salespeople, doctors, salaries, P&L). Add a report by decorating a function in the right file.
- `exports.py` — CSV, XLSX (openpyxl), PDF (WeasyPrint with print fallback).
- `kpis.py` — `admin_kpis`, `trend`, `lab_kpis`, `pharmacy_kpis` for the dashboards.
- `views.py` (hub, report pages, exports), `views_dashboards.py` (lab and pharmacy dashboards).

## Rules
- Filters a report can declare: date, branch, department, doctor, vendor, product, cashier, patient.
- Reports query through scoped querysets and declare their permission; money reports need
  `reports.financial.view`. Exception: supplier books (ledger, balances, invoices, payments, returns,
  daily purchases) also open with `pharmacy.inventory.manage`, who already see them on the supplier page.
- Daily sales are billed amounts, paid or not; money received is `daily-collection`. Pharmacy returns
  come from `PharmacySaleReturn`, other refunds from `Refund`, so neither counts twice.
- Keep dashboards cheap: aggregate queries, no per-row loops (`test_query_scaling.py`).

## Permissions
`reports.view`, `reports.financial.view`.

## Tests
`tests/test_reports.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
