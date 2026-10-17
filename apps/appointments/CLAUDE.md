# apps/appointments — Appointment booking

Doctor slots, booking, rescheduling and appointment status. PRD §4. PROJECT_NOTES §10.

## Files
- `services.py` — `available_slots(doctor, branch, date)`, `book(...)`, `reschedule`, `cancel`,
  `confirm`, `check_in`, `mark_no_show`, `suggest_visit_type`, `default_fee`, `schedule_for`.
- `models.py` — `Appointment` with `Status`, `VisitType`, `Source` and the allowed `TRANSITIONS`.

## Rules
- Status changes only through services (`_transition` checks `TRANSITIONS`).
- Slots come from `masterdata.DoctorSchedule` (per branch, per weekday, with break) and
  `DoctorLeave`. New/follow-up bookings must hit a free slot; walk-in and emergency skip slot checks.
- Booking takes a per-organization lock (`_lock_bookings`) so two desks cannot take one slot.
- No booking in the past; one active appointment per patient per doctor per day.
- Downstream: `queue.issue_token` moves it to in_queue; encounters move it to with_doctor/completed.

## Permissions
`appointment.view`, `appointment.manage`.

## Tests
`tests/test_appointments.py`.

## Keep this file current
Update it in the same change when you add or rename an entry point, change a status flow, a money/stock/clinical rule, a permission this app checks, or a cross-app link listed here. Shared conventions: `apps/CLAUDE.md`.
