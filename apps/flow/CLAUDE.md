# apps/flow — Patient flow (where an OPD patient is, which counter takes each fee)

A state machine (django-fsm-2) on top of the queue token, plus the fees each visit owes and the counter that
takes them. The hospital changes the route on Settings → Patient flow; nothing about counters is hard-coded.
PROJECT_NOTES §11.

## Files
- `choices.py` — `CounterLocation` (RECEPTION, PHARMACY, LAB, CENTRAL_CASH), `VisitState`. No model imports, so
  `masterdata.Service.billed_at` uses it too.
- `models.py` — `HospitalFlowSetting` (the hospital's row has no branch; a branch may have its own row:
  `consultation_fee_billed_at`, `lab_tests_billed_at`, `doctor_must_click_next`, `doctor_generates_digital_rx`,
  `rx_print_at`, `pharmacy_handles_manual_rx`; `reception_prints_blank_rx` reads Preferences), `PatientVisit` (one
  per token, protected `state` FSMField and its transitions with their conditions), `PatientVisitStep` (when it
  entered each state), `PatientBillItem` (a fee, its `billed_at` counter, `is_paid`, `collected_by_counter`, the
  invoice and line that carry it, and the `procedure` or `lab_order` it came from).
- `services.py` — settings (`flow_settings_for(org, branch)`, `settings_row_for_edit`, `save_settings`,
  `use_hospital_rules`, `fee_counter`), reception (`open_visit`, `reception_pay_link`, `held_counts`), fees
  (`add_fee`, `add_charge`, `procedure_to_pharmacy`, `bill_lab_at_pharmacy`, `track_invoiced_fee`, `drop_unbilled`),
  billing (`unbilled`, `collect_lines`, `counter_invoice`, `pharmacy_dues`, `bill_pharmacy_dues`, `sync_invoice`),
  routing (`route_after_doctor/_lab/_pharmacy`), doctor (`on_token_status`, `doctor_clicked_next`), printing,
  `pull_by_token`, lab (`lab_guard`, `unpaid_lab_lines`), `send_on`, `close_stale_visits`, screens (`board`,
  `counter_queue`, `attach_lab_dues`, `token_payments`, `flow_times`), `normalize_token` (A-012 = a12 = A012 = "a 12").
- `signals.py` — token saved (open visit, follow its status), invoice saved (fees follow it), lab order status,
  pharmacy sale completed.
- `management/commands/close_flow_day.py` — closes visits left open on earlier days.
- Templates: `flow/board.html`, `flow/settings.html`, `flow/_counter_queue.html` (pharmacy Prescriptions and lab
  worklist "Waiting here"), `flow/_encounter_fees.html` (consultation Fees tab).
- `templatetags/flow.py` — `{% pending_print %}`.
- `views.py` — `flow:board` (today's visits by state, Bill at counter, Done here, Add fee, take by token),
  `flow:token` (Payments by token), `flow:settings`.
- `flow/_token_lookup.html` (box) + `_token_payments.html` (result, htmx) + `token.html` (page): on the board, the
  pharmacy Prescriptions page, the lab worklist and the doctor workspace.

## Rules
- `state` is protected: only transition methods move it, only services call them. Re-read a visit with
  `PatientVisit.objects.get` (or `services._lock`), never `refresh_from_db`. `_save` logs a `PatientVisitStep`.
- TOKEN_ISSUED → IN_QUEUE needs every RECEPTION fee paid (`reception_paid`); emergencies skip it. A refund
  before the doctor (`hold_again`) puts the token back on hold. `queue.waiting` leaves held tokens out, `queue.call`
  refuses them; the desk shows "Pay first" with Collect fee, the doctor's queue counts them. Check-in bills the fee.
- After the doctor: pending lab → LAB_QUEUE (PHARMACY_QUEUE first when its tests are paid at the pharmacy); else
  medicine or a pharmacy fee → PHARMACY_QUEUE; else PAYMENT_DUE if anything is unpaid, else COMPLETED. After the
  pharmacy, pending lab → LAB_QUEUE. PAYMENT_DUE settles when the last fee is paid.
- Doctor: by default a closed token (sign, Complete, Checked · Next) sends the patient on (`auto_next`). With
  `doctor_must_click_next`, only Checked · Next or Sign & next (`doctor_clicked_next`) does; signing alone leaves
  the patient WITH_DOCTOR for pharmacy or lab to take by token.
- Fail-safe `pull_by_token`: pharmacy (`pharmacy.sale`) or lab (`lab.sample.collect`) takes an IN_QUEUE or
  WITH_DOCTOR visit by token; the token closes (`queue.close_elsewhere`); audit `flow.pull`.
- Fees are not money. Money is the invoice; `sync_invoice` marks fees paid when it is paid (not refunded), and
  frees them if it is cancelled. Pharmacy fees join the till's invoice (`pos.totals`, `pos.checkout` →
  `bill_pharmacy_dues`); the procedure or lab order then points at that invoice. Other counters use
  `counter_invoice` + the normal pay screen.
- Procedures (`procedures.order`) and lab orders (`lab.create_order`/`edit_order`) written in a consultation:
  billed at the pharmacy when the service says so (lab: `lab_tests_billed_at`); otherwise invoiced as before and
  tracked on the visit. Cancelling removes fees not yet billed (`drop_unbilled`).
- Lab guard: collect, process and result entry refuse while any lab fee of the visit is unpaid. Urgent/STAT may
  collect with a reason (audit `lab.unpaid_override`), then are not asked again. Emergencies and lab-desk orders
  (no consultation) keep the lab's own rules.
- Adding a fee needs `flow.charge.add` (reception, doctor, cashier by default).
- Lab orders billed on their own invoice carry no fee row: `attach_lab_dues` adds their unpaid invoices to the board
  (with a pay link) and to "Still to pay" on the counter lists (Collect, or "Pay at cash" without the permission).
- Payments by token (`token_payments`): today's issued invoices of the token's patient in this branch, plus fees on no
  invoice yet. Counters and doctors (`consultation.create`); a doctor who is only a doctor sees their own patients.
- A cancelled visit owes nothing: when its fee invoice is voided (`appointments.release_fee`), `sync_invoice` deletes
  its fee rows instead of freeing them.
- Day end: `close_stale_visits` (first board view of a day, or `close_flow_day`) closes open visits from earlier
  days (`closed_at_day_end`); unpaid fees stay on record. Report `patient-flow-times` (apps/reports, clinical).
- Prescription printing (`rx_print_at`: none, doctor, pharmacy, both): `print_at_doctor` when the doctor signs,
  `print_at_pharmacy` when the pharmacy opens or attaches it, once (`Prescription.pharmacy_printed_at`). The Rx id
  waits in the session; `{% pending_print %}` (`layouts/app.html`, till cart) prints it in a hidden frame.

## Permissions
Board: any of `queue.manage`, `pharmacy.sale`, `lab.sample.collect`, `billing.payment.collect`. Payments by token: the
same or `consultation.create`. Billing a counter
needs `billing.payment.collect`; adding a fee `flow.charge.add`. Settings: `admin.organization.manage`.

## Tests
`tests/test_flow.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
