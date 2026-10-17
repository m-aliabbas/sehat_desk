---
name: sehatdesk-demo-data
description: Load, inspect or extend the SehatDesk demo hospital (manage.py seed_demo). Use when setting up staging or a sales demo, when a new workflow should appear in demo data, or when seed_demo tests fail after a model or service change.
---

# Demo data (`seed_demo`)

Code: `apps/core/management/commands/seed_demo.py` (flow) and `apps/core/demo_data.py`
(catalogue: hospital, branches, departments, doctors, staff, services, lab tests and panels,
50 products, 50 invented patients, clinical cases, admissions). Notes: PROJECT_NOTES §22.

## Load it
- Fresh data folder only:
  `SEHATDESK_DATA_DIR=<empty dir> python manage.py migrate` then `... python manage.py seed_demo`.
  For staging use `SEHATDESK_ENV_FILE=deploy/staging.env` instead.
- On a development database that already has a hospital: `--alongside`. Never on a customer machine.
- `--password` sets the shared demo password; otherwise it is random. Logins are written to
  `<data dir>/DEMO_LOGIN.txt`, never printed. Do not paste the password into chat.

## How it works
- Every record is created through the real services (booking, tokens, vitals, consultations,
  prescriptions, lab, billing, POS, purchasing, IPD) with a request built by `self.req(user, branch)`.
- `_clock_patched` moves `timezone.now` (and model fields defaulting to it) to each step's demo time,
  so history spreads over ~2 weeks. Today's steps are clamped to the real time.
- Everything runs in one transaction: a failure leaves nothing behind.

## Extend it
1. Add catalogue rows to `demo_data.py` (invented names only; CNICs are 13 digits, phones 11).
2. Add a step method in `seed_demo.py`, call it from `_build()` in time order, using services only.
3. If counts change, update `_report()` and `apps/core/tests/test_seed_demo.py`.
4. Run `python -m pytest apps/core/tests/test_seed_demo.py -q`, then a real run on a scratch
   data dir and spot-check the screens.

## When it breaks after another change
The failing step names the service that refused. Fix the demo step to satisfy the new rule (new
required field, permission, status precondition); do not weaken the service.
