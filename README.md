# SehatDesk

A hospital management system for clinics and small hospitals in Pakistan. It runs on one
computer in the hospital and staff use it from any browser on the hospital network at
**http://sehatdesk.local**. No internet connection is needed.

Built with Django, HTMX, Alpine.js and Tailwind. SQLite by default, PostgreSQL supported.

## What it does

| Area | Features |
|---|---|
| Reception | Patient registration with duplicate detection, appointments, walk-ins, token queue, waiting-room display |
| OPD | Doctor workspace, consultation notes with signed versions, ICD-10 diagnoses, vitals, prescriptions |
| Laboratory | Test catalogue and panels, orders, sample barcodes, result entry, verification, critical alerts, reports |
| Pharmacy | Products and batches (first-expiry-first), point of sale, prescription dispensing, suppliers, purchases, returns |
| Inpatients | Wards, rooms and beds, admissions, transfers, deposits, ward rounds, medication orders, discharge |
| Nursing | Assignments, notes, medication charting, intake/output, shift handover |
| Billing | Invoices, payments (cash, card, Easypaisa, JazzCash...), discounts, refunds, day closing |
| Procedures | ECG, ECHO, ultrasound, operations and dental work billed per patient |
| Staff and finance | Attendance, payroll, doctor commission, expenses, ledgers, profit and loss |
| Administration | Branches, roles and permissions, reports with CSV/Excel/PDF export, dashboards, global search, audit trail, backups |

Every screen checks permissions and keeps each hospital's and branch's data separate on the
server. Money and stock changes run in database transactions. Clinical records are corrected
by adding a new version, never by overwriting.

## Quick start (development)

Requires the `agent_dev311` conda environment (Python 3.11).

```bash
conda activate agent_dev311
```

```bash
pip install -r requirements-dev.txt
```

```bash
python manage.py migrate
```

```bash
python manage.py runserver
```

Open http://localhost:8000. On an empty database you are sent to `/setup/` to create the
hospital and the first admin account.

### Try it with demo data

`seed_demo` builds a demo hospital with two branches, 8 doctors, staff for every role,
50 invented patients and about two weeks of activity. Load it into an empty data folder:

```bash
SEHATDESK_DATA_DIR=./data-demo python manage.py migrate
```

```bash
SEHATDESK_DATA_DIR=./data-demo python manage.py seed_demo
```

```bash
SEHATDESK_DATA_DIR=./data-demo python manage.py runserver
```

The demo logins (`demo.admin`, `demo.dr.ayesha`, `demo.reception1`...) and their password are
written to `data-demo/DEMO_LOGIN.txt`. Never load demo data on a hospital's real database.

## Checks

```bash
python -m ruff check .
```

```bash
python -m ruff format --check .
```

```bash
python -m pytest -q
```

CI runs these on Windows, macOS and Linux, and also against PostgreSQL. After adding Tailwind
classes, rebuild the stylesheet with `python manage.py tailwind build` and commit
`static/css/app.css`.

## Running for real

- `serve.py` serves the app with Waitress (pure Python, Windows and macOS) and takes automatic
  backups.
- Staging and production settings come from env files; templates are in `deploy/`.
- `docker compose up --build` runs PostgreSQL and the staging configuration on
  http://localhost:8001 (for demos and hosted servers).
- Hospitals will get Windows and macOS installers with a desktop shortcut (planned, Phase 30).
- Backups: `python manage.py backup` and `python manage.py restore`. See
  [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md).

## Project layout

```
apps/            one Django app per module (patients, billing, lab, pharmacy, ipd, ...)
config/          settings and URLs
templates/       Django templates per app, shared layouts and components
static/          Tailwind source, built CSS, htmx and Alpine (vendored, no CDN)
deploy/          staging and production env templates
docs/            project notes and backup procedures
ui_design_reference/   design kit: screens, tokens, page checklist
```

## Documentation

- [PRD.md](PRD.md): product requirements
- [TODO_TASKS.md](TODO_TASKS.md): build checklist by phase
- [docs/PROJECT_NOTES.md](docs/PROJECT_NOTES.md): architecture, deployment and network decisions
- [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md): backup and restore
- [CLAUDE.md](CLAUDE.md): working rules, plus a `CLAUDE.md` in each app folder and skills in
  `.claude/skills/` for AI-assisted development

## Status

Done: phases 0–14, 16–29, 31 and 33–36, including all core hospital workflows, reports,
security hardening, tests, performance and backups.

Not started yet:
- AI assistant (Phase 15). AI output will only ever be a draft for a clinician to accept.
- Installers and deployment (Phase 30).
- Mobile companion app (Phase 32).
