# apps/ipd — Inpatient department

Wards, rooms, beds, admissions, transfers, the running bill and discharge. PRD §10. Phase 16.

## Files
- `services.py` — `add_beds`, `set_bed_status`, `admit`, `transfer`, `cancel_admission`,
  `record_deposit`, `add_charge`, `billing_summary`, `finalize_bill`, `return_deposit_balance`,
  `add_round`, `mark_round_error`, `order_medication`, `stop_medication`, discharge
  (`save_discharge_draft`, `plan_discharge`, `release`, `amend_summary`), `ward_overview`.
- `models.py` — Branch → `Ward` → `Room` (daily_rate) → `Bed`; `Admission`, `BedOccupancy`
  (occupancy ledger), `BedTransfer`, `AdmissionDeposit`, `DoctorRound`, `MedicationOrder`
  (`SCHEDULE` per frequency), `DischargeSummary`.

## Rules
- A bed holds one patient: partial unique constraints on open occupancy plus a row lock.
- Admission opens a draft invoice that collects bed, service and procedure charges until the
  final bill. Deposits are money: transactional and idempotent.
- Rounds and medication orders are clinical: corrected by "entered in error", never edited.
- Discharge with an unpaid bill needs `ipd.discharge.override`.
- Audit entries name the action and numbers, never clinical text.

## Connected
billing (stay invoice), nursing (MAR, notes on the admission), lab (pending orders checklist).

## Permissions
`ipd.manage`, `ipd.facility.manage`, `ipd.discharge.override`, `billing.payment.collect` (deposits).

## Tests
`tests/test_ipd.py` (`tests/helpers.py` builds wards and beds).

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
