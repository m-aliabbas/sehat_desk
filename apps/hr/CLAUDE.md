# apps/hr — Staff attendance, pay and doctor commission

Phases 34–35: attendance, pay profiles, doctor commission, payroll and staff ledgers.

## Files
- `attendance.py` — `check_in`, `check_out`, `mark`, `month_sheet`, `is_locked`, `working_days`.
- `commission.py` — `sync_invoice(invoice)` earns/reverses commission, `rate_for`, `unsettled`.
- `payroll.py` — `create_run`, `recalculate`, `set_adjustments`, `finalize`, `pay_staff`,
  `post_opening`, `balance`.
- `signals.py` — invoice saves call `commission.sync_invoice` (billing does not import hr).
- `models.py` — `AttendanceRecord`, `PayProfile`, `CommissionEarning` (append-only),
  `PayrollRun`, `Payslip`, `StaffLedgerEntry`.

## Rules
- Commission rows are append-only: refunds/cancellations post negative adjustments.
- Commission needs a fully paid invoice and `InvoiceItem.performed_by` with an active pay profile.
- Absence deductions count only days explicitly marked absent.
- Finalized payroll is locked; payments go to the staff ledger.

## Permissions
`hr.self` (own attendance/earnings), `hr.attendance.manage`, plus finance codes for pay screens.

## Tests
`tests/test_hr.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
