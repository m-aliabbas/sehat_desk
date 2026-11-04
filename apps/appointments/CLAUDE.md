# apps/appointments — Appointment booking

Doctor slots, booking, rescheduling and appointment status. PRD §4. PROJECT_NOTES §10.

## Files
- `services.py` — `available_slots(doctor, branch, date)`, `book(...)`, `reschedule`, `cancel`,
  `confirm`, `check_in`, `mark_no_show`, `suggest_visit_type`, `default_fee`, `schedule_for`.
- `visits.py` — the front-desk visit (the fee invoice only when the doctor's fee is paid at reception,
  `flow.services.fee_counter`): `register_visit`, `arrive`, `collect_fee`. The form
  (`_visit_form.html`, `visit_context`) sits on the reception desk and posts to `visit-new`; `papers`
  prints the blank Rx, token and fee copies. A future date on the same form is a pre-booking.
- `models.py` — `Appointment` with `Status`, `VisitType`, `Source` and the allowed `TRANSITIONS`.

## Rules
- Status changes only through services (`_transition` checks `TRANSITIONS`).
- Slots come from `masterdata.DoctorSchedule` (per branch, per weekday, with break) and
  `DoctorLeave`. New/follow-up bookings must hit a free slot; walk-in and emergency skip slot checks.
- Booking takes a per-organization lock (`_lock_bookings`) so two desks cannot take one slot.
- No booking in the past; one active appointment per patient per doctor per day.
- Downstream: `queue.issue_token` moves it to in_queue; encounters move it to with_doctor/completed.

- Time on a visit is automatic (`next_free_slot`, optional shift); today with no slot left becomes a
  walk-in. Future dates get no token or invoice until `arrive`, unless the fee is paid in advance.
- A doctor can have several clinics a day (`DoctorSchedule.shift`); use `schedules_for`.
- `register_visit` is all-or-nothing, including any new patient made on the same screen.
- Emergency (`emergency=True`, F4 on the desk form): today, now, no slot or leave check, visit type emergency, `E-` token first in the queue. Phone and CNIC are optional, the fee may stay unpaid, and the papers still print by themselves.

## Permissions
`appointment.view`, `appointment.manage`.

## Tests
`tests/test_appointments.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
