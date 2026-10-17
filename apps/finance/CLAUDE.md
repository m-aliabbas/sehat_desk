# apps/finance — Expenses, ledgers and profit & loss

Phase 36. Admin finance dashboard, P&L, the cash ledger, expenses and supplier payments.

## Files
- `services.py` — `record_expense`, `void_expense`, `pay_supplier`, `ensure_categories`.
- `statements.py` — read-only: `income_rows`, `pnl_rows`, `statement`, `monthly_trend`,
  `cash_entries`, `ledger`, `outstanding`, `doctor_shares`.
- `segments.py` — maps invoice line kinds and expenses to business segments.
- `models.py` — `Expense` (+ categories), never edited or deleted (void with reason).

## Rules
- Income, refunds, deposits, staff pay and supplier payments stay in their own apps;
  `statements.py` reads them. Do not copy money into finance tables.
- Income is accrual by invoice issue date; drafts and cancelled invoices excluded.
- Writes are transactional and double-submit safe (`idempotency_key`).
- A new invoice line kind must be mapped in `segments.py`.

## Permissions
`finance.view`, `finance.expense.manage`.

## Tests
`tests/test_finance.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
