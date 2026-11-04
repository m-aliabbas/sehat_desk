# apps/flow — Patient flow (where an OPD patient is, which counter takes each fee)

A state machine (django-fsm-2) on top of the queue token, plus the fees each visit owes and the counter that
takes them. The hospital changes the route on Settings → Patient flow; nothing about counters is hard-coded.
PROJECT_NOTES §11.

## Files
- `choices.py` — `CounterLocation` (RECEPTION, PHARMACY, LAB, CENTRAL_CASH), `VisitState`. No model imports, so
  `masterdata.Service.billed_at` uses it too.
- `models.py` — `HospitalFlowSetting` (one row per organization, not a global singleton:
  `consultation_fee_billed_at`, `doctor_must_click_next`, `doctor_generates_digital_rx`,
  `pharmacy_handles_manual_rx`; `reception_prints_blank_rx` reads Preferences), `PatientVisit` (one per token,
  protected `state` FSMField and its transitions with their conditions), `PatientBillItem` (a fee, its
  `billed_at` counter, `is_paid`, `collected_by_counter`, the invoice and line that carry it).
- `services.py` — `flow_settings_for`, `save_settings`, `fee_counter`, `open_visit`, `add_charge`, `unbilled`,
  `collect_lines`, `counter_invoice`, `pharmacy_dues`, `bill_pharmacy_dues`, `sync_invoice`,
  `route_after_doctor`, `route_after_lab`, `pull_by_token`, `lab_guard`, `unpaid_lab_lines`, `send_on`, `board`.
- `signals.py` — token saved (open visit, follow its status), invoice saved (fees follow it), prescription sent
  and lab order created (auto move when the doctor need not click), lab order status, pharmacy sale completed.
- `templatetags/flow.py` — `{% pending_print %}`.
- `views.py` — `flow:board` (today's visits by state, Bill at counter, Done here, Add fee, take by token),
  `flow:settings`.

## Rules
- `state` is protected: only transition methods move it, only services call them. Re-read a visit with
  `PatientVisit.objects.get` (or `services._lock`), never `refresh_from_db`.
- TOKEN_ISSUED → IN_QUEUE needs every RECEPTION fee paid (`reception_paid`); emergencies skip it.
  `queue.waiting` leaves held tokens out and `queue.call` refuses them.
- After the doctor (`route_after_doctor`): pending lab order → LAB_QUEUE; else digital Rx, an unbilled
  pharmacy fee, or a paper Rx where `pharmacy_handles_manual_rx` → PHARMACY_QUEUE; else COMPLETED.
  No consultation note = paper Rx; a note without medicines is paper only where doctors do not write Rx here.
- `doctor_must_click_next` on: only the doctor's close (Checked · Next, Complete, signing) moves on. Off: an Rx
  sent or a test ordered moves the patient at once (`auto_next`) and closes the doctor's token.
- Fail-safe `pull_by_token`: pharmacy (`pharmacy.sale`) or lab (`lab.sample.collect`) takes an IN_QUEUE or
  WITH_DOCTOR visit by token; the token closes (`queue.close_elsewhere`); audit `flow.pull`.
- Fees are not money. Money is the invoice; `sync_invoice` marks fees paid when it is paid or refunded, and
  frees them if it is cancelled. Pharmacy fees join the till's own invoice at checkout (`pos.totals` shows them,
  `pos.checkout` calls `bill_pharmacy_dues`). Other counters use `counter_invoice` + the normal pay screen.
- Lab guard: `lab.services` collect, process and result entry call `lab_guard`; any unpaid lab fee of the visit
  (kind lab or billed at the lab, any counter) or outstanding lab invoice blocks it. Emergencies and orders with
  no visit keep the lab's own rules.

- Prescription printing (`rx_print_at`: none, doctor, pharmacy, both): `print_at_doctor` when the doctor signs
  (encounter sign, Checked · Next), `print_at_pharmacy` when the pharmacy opens it (`rx-detail`) or attaches it at
  the till, once per prescription (`Prescription.pharmacy_printed_at`). Both put the Rx id in the session; the
  `{% pending_print %}` tag (`templatetags/flow.py`, in `layouts/app.html` and the till cart) pops it and loads
  `prescriptions:print?print=1` in a hidden frame, which prints on that PC's printer (apps/printing).

## Permissions
Board: any of `queue.manage`, `pharmacy.sale`, `lab.sample.collect`, `billing.payment.collect`. Billing a counter
needs `billing.payment.collect`. Settings: `admin.organization.manage`.

## Tests
`tests/test_flow.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
